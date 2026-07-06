"""Native Power BI helpers for the chat flow."""
import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

import requests

LOG = logging.getLogger(__name__)
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
MAX_DAX_RESULT_ROWS_FOR_LLM = 30
MAX_POWERBI_ERROR_BODY_CHARS = 3_000


def _preview_response_body(response: requests.Response) -> str:
    body = (response.text or "").strip()
    if len(body) <= MAX_POWERBI_ERROR_BODY_CHARS:
        return body

    omitted_chars = len(body) - MAX_POWERBI_ERROR_BODY_CHARS
    return (
        f"{body[:MAX_POWERBI_ERROR_BODY_CHARS]}\n"
        f"[Power BI error body truncado: se omitieron {omitted_chars} caracteres.]"
    )


def _get_access_token(credentials: Dict[str, str]) -> str:
    """Obtain a delegated Power BI token using the report's stored user."""
    required = ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET", "USER", "PASS")
    missing = [key for key in required if not str(credentials.get(key) or "").strip()]
    if missing:
        raise RuntimeError(f"Faltan credenciales Power BI del reporte: {', '.join(missing)}")

    token_url = f"https://login.microsoftonline.com/{credentials['TENANT_ID']}/oauth2/v2.0/token"
    payload = {
        "grant_type": "password",
        "client_id": credentials["CLIENT_ID"],
        "client_secret": credentials["CLIENT_SECRET"],
        "scope": POWERBI_SCOPE,
        "username": credentials["USER"],
        "password": credentials["PASS"],
    }

    response = requests.post(token_url, data=payload, timeout=30)
    if not response.ok:
        LOG.error(
            "Azure AD token request failed for Power BI chat user - status=%s body=%r",
            response.status_code,
            response.text,
        )
    response.raise_for_status()

    token_data = response.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError(f"Azure AD no devolvio access_token: {token_data}")

    return str(access_token)


def _load_dax_rows(result_json: str) -> List[Dict[str, Any]]:
    """Parse executeQueries JSON output into a row list."""
    parsed = json.loads(result_json)
    if isinstance(parsed, list):
        rows = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("rows"), list):
        rows = parsed["rows"]
    else:
        raise RuntimeError(f"Power BI devolvio un formato inesperado para filas DAX: {type(parsed).__name__}")

    malformed = [row for row in rows if not isinstance(row, dict)]
    if malformed:
        raise RuntimeError("Power BI devolvio filas DAX con formato inesperado")

    return rows


def get_tables_and_measures_description(dataset_id: str, credentials: Dict[str, str]) -> List[Dict[str, Any]]:
    """Fetch structured table and measure chunks from a semantic model."""
    try:
        tables_res = execute_dax_query_local(
            dataset_id,
            "EVALUATE INFO.VIEW.TABLES()",
            credentials,
            max_rows=None,
        )
        if tables_res.startswith("Error"):
            raise RuntimeError(tables_res)
        t_rows = _load_dax_rows(tables_res)

        table_descriptions = {}
        for table_row in t_rows:
            table_name = table_row.get("[Name]")
            table_description = table_row.get("[Description]")

            if not table_name or table_name.startswith("DateTable") or table_name.startswith("LocalDate"):
                continue
            table_descriptions[table_name] = table_description or ""

        columns_res = execute_dax_query_local(
            dataset_id,
            "EVALUATE INFO.VIEW.COLUMNS()",
            credentials,
            max_rows=None,
        )
        if columns_res.startswith("Error"):
            raise RuntimeError(columns_res)
        c_rows = _load_dax_rows(columns_res)

        tables_dict = defaultdict(list)
        for column_row in c_rows:
            table_name = column_row.get("[Table]")
            column_name = column_row.get("[Name]")
            column_type = column_row.get("[DataType]")

            if not table_name or table_name.startswith("DateTable") or table_name.startswith("LocalDate"):
                continue
            if column_name and column_name.startswith("RowNumber-"):
                continue

            tables_dict[table_name].append(f"{column_name} ({column_type})")

        measures_res = execute_dax_query_local(
            dataset_id,
            "EVALUATE INFO.VIEW.MEASURES()",
            credentials,
            max_rows=None,
        )
        if measures_res.startswith("Error"):
            raise RuntimeError(measures_res)
        m_rows = _load_dax_rows(measures_res)

        documents: List[Dict[str, Any]] = []
        for table_name, columns in tables_dict.items():
            description = table_descriptions.get(table_name, "")
            columns_text = ", ".join(columns)
            documents.append(
                {
                    "item_type": "table",
                    "item_name": table_name,
                    "content_text": f"Tabla: {table_name}. Descripcion: {description}. Columnas: {columns_text}",
                    "description": description,
                    "columns": list(columns),
                }
            )

        for measure_row in m_rows:
            name = measure_row.get("[Name]")
            description = measure_row.get("[Description]")
            if not name:
                continue

            documents.append(
                {
                    "item_type": "measure",
                    "item_name": name,
                    "content_text": f"Medida: {name}. Descripcion: {description or ''}.",
                    "description": description or "",
                }
            )

        return documents
    except Exception as exc:
        LOG.exception("Error obteniendo el esquema del dataset")
        raise RuntimeError(
            f"Error obteniendo el esquema: {exc}\nIntenta explorar los datos usando consultas DAX basicas."
        ) from exc


def execute_dax_query_local(
    dataset_id: str,
    dax_query: str,
    credentials: Dict[str, str],
    max_rows: Optional[int] = MAX_DAX_RESULT_ROWS_FOR_LLM,
) -> str:
    """Execute DAX directly against the Power BI API."""
    if not dax_query or not str(dax_query).strip():
        return "Error: dax_query vacio"

    try:
        token = _get_access_token(credentials)
        workspace_id = str(credentials.get("WORKSPACE_ID") or "").strip()
        if workspace_id:
            url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
        else:
            url = f"https://api.powerbi.com/v1.0/myorg/datasets/{dataset_id}/executeQueries"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "queries": [{"query": dax_query}],
            "serializerSettings": {"includeNulls": True},
        }

        response = requests.post(url, headers=headers, json=payload, timeout=30)

        if response.status_code == 404:
            return (
                f"Error: No se encontro el Dataset ID '{dataset_id}'. "
                "Verifica que exista y que el usuario Power BI del reporte tenga acceso."
            )

        if not response.ok:
            body_preview = _preview_response_body(response)
            LOG.error(
                "Power BI executeQueries failed - status=%s reason=%s body=%r",
                response.status_code,
                response.reason,
                body_preview,
            )
            detail = f" Body: {body_preview}" if body_preview else ""
            return f"Error tecnico ejecutando DAX: Power BI {response.status_code} {response.reason}.{detail}"

        response.raise_for_status()
        data = response.json()

        rows = []
        for result in data.get("results", []):
            for table in result.get("tables", []):
                rows.extend(table.get("rows", []))

        if max_rows is not None and len(rows) > max_rows:
            truncated_rows = rows[:max_rows]
            truncated_payload = {
                "warning": (
                    f"La consulta devolvio {len(rows)} filas. "
                    f"Mostrando solo las primeras {max_rows}. "
                    "Agrega filtros o TOPN en DAX si necesitas un resultado mas acotado."
                ),
                "truncated": True,
                "total_rows": len(rows),
                "returned_rows": len(truncated_rows),
                "rows": truncated_rows,
            }
            return json.dumps(truncated_payload, ensure_ascii=False)

        return json.dumps(rows, ensure_ascii=False)
    except Exception as exc:
        LOG.exception("Error ejecutando DAX localmente")
        return f"Error tecnico ejecutando DAX: {str(exc)}"
