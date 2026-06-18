"""MCP server for Power BI semantic-layer access.
"""
from __future__ import annotations

import json
from collections import defaultdict
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

import msal
import requests
from dotenv import load_dotenv

import sys


def _remove_project_root_from_sys_path() -> None:
    project_root = Path(__file__).resolve().parents[1]
    cleaned = []
    for entry in sys.path:
        if not entry:
            continue
        try:
            if Path(entry).resolve() == project_root:
                continue
        except Exception:
            pass
        cleaned.append(entry)
    sys.path[:] = cleaned


_remove_project_root_from_sys_path()

from mcp.server.fastmcp import FastMCP


LOG = logging.getLogger(__name__)
ROOT_DIR = Path(__file__).resolve().parents[1]

# Load environment from the project root first, then fall back to the current
# working directory. This keeps the server working even when it is launched with
# MCP_SERVER_CWD pointing to the `mcp/` folder.
load_dotenv(ROOT_DIR / ".env")
load_dotenv()

mcp = FastMCP("PowerBI_Semantic_Layer")

MSAL_APP = None
SCOPES = ["https://analysis.windows.net/powerbi/api/.default"]


def _get_env(*names: str, default: str | None = None, required: bool = False) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    if required:
        joined = ", ".join(names)
        raise RuntimeError(f"Missing required environment variable. Expected one of: {joined}")
    return default


def _build_msal_app() -> msal.ConfidentialClientApplication:
    client_id = _get_env("CLIENT_ID_MCP", "CLIENT_ID", required=True)
    client_secret = _get_env("CLIENT_SECRET_MCP", "CLIENT_SECRET", required=True)
    tenant_id = _get_env("TENANT_ID", required=True)

    assert client_id is not None
    assert client_secret is not None
    assert tenant_id is not None

    authority = f"https://login.microsoftonline.com/{tenant_id}"
    return msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret,
    )


def _get_msal_app() -> msal.ConfidentialClientApplication:
    global MSAL_APP
    if MSAL_APP is None:
        MSAL_APP = _build_msal_app()
    return MSAL_APP


def get_access_token() -> str:
    """Obtain a Power BI access token using the service principal."""
    result = _get_msal_app().acquire_token_for_client(scopes=SCOPES)
    access_token = result.get("access_token")
    if access_token:
        return access_token
    raise RuntimeError(
        f"Error de autenticación con Service Principal: {result.get('error_description', result)}"
    )


def _execute_powerbi_query(dataset_id: str, dax_query: str) -> Dict[str, Any]:
    token = get_access_token()
    url = f"https://api.powerbi.com/v1.0/myorg/datasets/{dataset_id}/executeQueries"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "queries": [{"query": dax_query}],
        "serializerSettings": {"includeNulls": True},
    }

    response = requests.post(url, headers=headers, json=payload, timeout=120)
    if response.status_code != 200:
        raise RuntimeError(f"Error en la API de Power BI: {response.status_code} - {response.text}")

    return response.json()


def _extract_rows(api_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    return api_result.get("results", [{}])[0].get("tables", [{}])[0].get("rows", [])


def _clean_table_name(name: str | None) -> bool:
    if not name:
        return False
    return not (name.startswith("DateTable") or name.startswith("LocalDate"))




@mcp.tool()
def get_semantic_model_schema(dataset_id: str) -> str:
    """Obtiene tablas, columnas, medidas y relaciones del modelo semántico.

    La IA debe llamar a esta herramienta antes de generar consultas DAX.
    """
    dax_measures = "EVALUATE INFO.VIEW.MEASURES()"
    dax_columns = "EVALUATE INFO.VIEW.COLUMNS()"
    dax_relationships = "EVALUATE INFO.VIEW.RELATIONSHIPS()"

    try:
        columns_res = _execute_powerbi_query(dataset_id, dax_columns)
        measures_res = _execute_powerbi_query(dataset_id, dax_measures)
        relationships_res = _execute_powerbi_query(dataset_id, dax_relationships)
        LOG.debug("Measures query response received for dataset_id=%s", dataset_id)

        column_rows = _extract_rows(columns_res)
        measure_rows = _extract_rows(measures_res)
        relationship_rows = _extract_rows(relationships_res)

        tables_dict: Dict[str, List[str]] = defaultdict(list)

        for row in column_rows:
            table_name = row.get("[Table]")
            column_name = row.get("[Name]")
            column_type = row.get("[DataType]")

            if not _clean_table_name(table_name):
                continue
            if column_name and str(column_name).startswith("RowNumber-"):
                continue
            if not column_name:
                continue

            tables_dict[str(table_name)].append(f"{column_name} ({column_type})")

        clean_measures: List[str] = []
        for row in measure_rows:
            name = row.get("[Name]")
            expr = row.get("[Expression]")
            descr = row.get("[Description]")

            if not name:
                continue

            parts = [str(name)]
            if descr:
                parts.append(str(descr))
            if expr:
                expr_str = str(expr)
                if len(expr_str) > 300:
                    expr_str = expr_str[:300] + "..."
                parts.append(f"Expresion: {expr_str}")
            clean_measures.append(" | ".join(parts))

        clean_relationships: List[str] = []
        for row in relationship_rows:
            from_table = row.get("[FromTable]")
            to_table = row.get("[ToTable]")

            if not _clean_table_name(from_table) or not _clean_table_name(to_table):
                continue

            from_column = row.get("[FromColumn]")
            to_column = row.get("[ToColumn]")
            clean_relationships.append(f"{from_table}.{from_column} -> {to_table}.{to_column}")

        schema_info = {
            "Tables": dict(tables_dict),
            "Measures": clean_measures,
            "Relationships": clean_relationships,
        }
        return json.dumps(schema_info, indent=2, ensure_ascii=False)
    except Exception as exc:
        LOG.exception("Error obteniendo el esquema para dataset_id=%s", dataset_id)
        return f"Error obteniendo el esquema: {exc}\nIntenta explorar los datos usando consultas DAX básicas."


@mcp.tool()
def execute_dax_query(dataset_id: str, dax_query: str) -> str:
    """Execute a read-only DAX query and return the rows as JSON."""
    if not dax_query or not str(dax_query).strip():
        return "Error ejecutando DAX: dax_query vacío"

    try:
        result = _execute_powerbi_query(dataset_id, dax_query)
        rows = _extract_rows(result)
        return json.dumps(rows, indent=2, ensure_ascii=False)
    except Exception as exc:
        LOG.exception("Error ejecutando DAX para dataset_id=%s", dataset_id)
        return f"Error ejecutando DAX: {exc}"


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
