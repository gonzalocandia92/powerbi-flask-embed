"""Temporary API-key MCP surface retained during the OAuth migration window."""

import contextvars
import json
import os

from fastmcp import FastMCP

from .auth.api_keys import get_client_context
from .powerbi.rest_api import execute_dax_query
from .powerbi.xmla_tom import get_semantic_model_schema_tom


legacy_mcp = FastMCP("PowerBI_Aklara_Legacy")
legacy_api_key_var = contextvars.ContextVar(
    "legacy_api_key", default=os.getenv("MCP_API_KEY")
)


def _client():
    api_key = legacy_api_key_var.get()
    client = get_client_context(api_key) if api_key else None
    if client is None:
        raise PermissionError("Invalid legacy API key")
    return client


@legacy_mcp.tool()
async def get_powerbi_schema() -> str:
    client = _client()
    return get_semantic_model_schema_tom(client.workspace_name, client.dataset_name)


@legacy_mcp.tool()
async def execute_dax(query: str) -> str:
    client = _client()
    result = await execute_dax_query(client.dataset_id, query)
    return json.dumps(result, ensure_ascii=False, indent=2)
