"""Native Power BI helpers for the chat flow."""
import json
import logging
import os
from collections import defaultdict
from typing import Any, Dict, List

import msal
import requests

LOG = logging.getLogger(__name__)
SCOPES = ["https://analysis.windows.net/powerbi/api/.default"]


def _get_access_token(credentials: Dict[str, str]) -> str:
    """Combine the DB tenant with read-only environment credentials."""
    tenant_id = credentials["TENANT_ID"]

    client_id = os.getenv("POWERBI_TOOL_CLIENT_ID") or os.getenv("CLIENT_ID_MCP")
    client_secret = os.getenv("POWERBI_TOOL_CLIENT_SECRET") or os.getenv("CLIENT_SECRET_MCP")

    if not client_id or not client_secret:
        raise RuntimeError("Faltan POWERBI_TOOL_CLIENT_ID o POWERBI_TOOL_CLIENT_SECRET en el entorno (.env)")

    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(scopes=SCOPES)

    if not isinstance(result, dict):
        raise RuntimeError(f"Error MSAL: {result}")

    access_token = result.get("access_token")
    if access_token:
        return str(access_token)

    raise RuntimeError(f"Error MSAL: {result.get('error_description', result)}")


def get_tables_and_measures_description(dataset_id: str, credentials: Dict[str, str]) -> List[Dict[str, Any]]:
    """Fetch structured table and measure chunks from a semantic model."""
    try:
        tables_res = execute_dax_query_local(dataset_id, "EVALUATE INFO.VIEW.TABLES()", credentials)
        if tables_res.startswith("Error"):
            raise RuntimeError(tables_res)
        t_rows = json.loads(tables_res)

        table_descriptions = {}
        for table_row in t_rows:
            table_name = table_row.get("[Name]")
            table_description = table_row.get("[Description]")

            if not table_name or table_name.startswith("DateTable") or table_name.startswith("LocalDate"):
                continue
            table_descriptions[table_name] = table_description or ""

        columns_res = execute_dax_query_local(dataset_id, "EVALUATE INFO.VIEW.COLUMNS()", credentials)
        if columns_res.startswith("Error"):
            raise RuntimeError(columns_res)
        c_rows = json.loads(columns_res)

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

        measures_res = execute_dax_query_local(dataset_id, "EVALUATE INFO.VIEW.MEASURES()", credentials)
        if measures_res.startswith("Error"):
            raise RuntimeError(measures_res)
        m_rows = json.loads(measures_res)

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


def execute_dax_query_local(dataset_id: str, dax_query: str, credentials: Dict[str, str]) -> str:
    """Execute DAX directly against the Power BI API."""
    if not dax_query or not str(dax_query).strip():
        return "Error: dax_query vacio"

    try:
        token = _get_access_token(credentials)
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
                "Verifica que exista y que el Service Principal tenga acceso."
            )

        response.raise_for_status()
        data = response.json()

        rows = []
        for result in data.get("results", []):
            for table in result.get("tables", []):
                rows.extend(table.get("rows", []))

        return json.dumps(rows, ensure_ascii=False)
    except Exception as exc:
        LOG.exception("Error ejecutando DAX localmente")
        return f"Error tecnico ejecutando DAX: {str(exc)}"
