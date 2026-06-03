"""Native Power BI helpers for the chat flow."""
import os
import json
import logging
from collections import defaultdict
from typing import Dict, List

import msal
import requests

LOG = logging.getLogger(__name__)
SCOPES = ["https://analysis.windows.net/powerbi/api/.default"]


def _get_access_token(credentials: Dict[str, str]) -> str:
    """Combina el tenant de la DB con credenciales de solo lectura del entorno."""
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


def get_tables_and_measures_description(dataset_id: str, credentials: Dict[str, str]) -> List[str]:
    """
    Obtiene documentos planos de tablas y medidas del modelo semántico.
    """
    try:
        tables_res = execute_dax_query_local(dataset_id, "EVALUATE INFO.VIEW.TABLES()", credentials)
        if tables_res.startswith("Error"):
            raise RuntimeError(tables_res)
        t_rows = json.loads(tables_res)

        table_descriptions = {}
        for t in t_rows:
            t_name = t.get("[Name]")
            t_description = t.get("[Description]")

            if not t_name or t_name.startswith("DateTable") or t_name.startswith("LocalDate"):
                continue
            table_descriptions[t_name] = t_description or ""

        columns_res = execute_dax_query_local(dataset_id, "EVALUATE INFO.VIEW.COLUMNS()", credentials)
        if columns_res.startswith("Error"):
            raise RuntimeError(columns_res)
        c_rows = json.loads(columns_res)

        tables_dict = defaultdict(list)
        for c in c_rows:
            t_name = c.get("[Table]")
            c_name = c.get("[Name]")
            c_type = c.get("[DataType]")

            if not t_name or t_name.startswith("DateTable") or t_name.startswith("LocalDate"):
                continue
            if c_name and c_name.startswith("RowNumber-"):
                continue

            tables_dict[t_name].append(f"{c_name} ({c_type})")

        measures_res = execute_dax_query_local(dataset_id, "EVALUATE INFO.VIEW.MEASURES()", credentials)
        if measures_res.startswith("Error"):
            raise RuntimeError(measures_res)
        m_rows = json.loads(measures_res)

        documentos: List[str] = []
        for table_name, columns in tables_dict.items():
            descripcion = table_descriptions.get(table_name, "")
            columnas = ", ".join(columns)
            documentos.append(f"Tabla: {table_name}. Descripción: {descripcion}. Columnas: {columnas}")

        for m in m_rows:
            name = m.get("[Name]")
            descr = m.get("[Description]")

            if not name:
                continue

            documentos.append(f"Medida: {name}. Descripcion: {descr or ''}.")

        return documentos
    except Exception as e:
        LOG.exception("Error obteniendo el esquema del dataset")
        raise RuntimeError(
            f"Error obteniendo el esquema: {str(e)}\nIntenta explorar los datos usando consultas DAX básicas."
        ) from e


def execute_dax_query_local(dataset_id: str, dax_query: str, credentials: Dict[str, str]) -> str:
    """Ejecuta DAX directamente contra la API de Power BI."""
    if not dax_query or not str(dax_query).strip():
        return "Error: dax_query vacío"

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
                f"Error: No se encontró el Dataset ID '{dataset_id}'. "
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
        return f"Error técnico ejecutando DAX: {str(exc)}"
