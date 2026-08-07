"""OAuth-protected MCP tools with human-readable model selectors."""

import asyncio
import json
from time import perf_counter

from fastmcp import FastMCP
from fastmcp.server.auth import require_scopes
from fastmcp.server.dependencies import get_access_token

from .auth.broker import audit_result, list_access, resolve_access
from .powerbi.rest_api import execute_dax_query
from .powerbi.xmla_tom import create_measure as create_measure_tom
from .powerbi.xmla_tom import get_semantic_model_schema_tom
from .powerbi.xmla_tom import update_measure as update_measure_tom


def _public_models(access_list: list[dict]) -> list[dict]:
    """Return only user-facing model metadata; never expose internal identifiers."""
    name_counts: dict[str, int] = {}
    for item in access_list:
        key = item["model"]["name"].strip().casefold()
        name_counts[key] = name_counts.get(key, 0) + 1

    models = []
    for item in access_list:
        name = item["model"]["name"]
        model = {"name": name}
        if name_counts[name.strip().casefold()] > 1:
            model["company"] = item["company"]["name"]
        models.append(model)
    return models


async def _resolve_model_selector(
    token: str, model_name: str, company_name: str | None = None
) -> str:
    """Translate a public, human-readable selector to an internal grant id."""
    normalized_model = model_name.strip().casefold()
    normalized_company = company_name.strip().casefold() if company_name else None
    matches = [
        item
        for item in await list_access(token)
        if item["model"]["name"].strip().casefold() == normalized_model
        and (
            normalized_company is None
            or item["company"]["name"].strip().casefold() == normalized_company
        )
    ]
    if not matches:
        raise ValueError("El modelo semántico indicado no está disponible.")
    if len(matches) > 1:
        raise ValueError(
            "Hay más de un modelo con ese nombre. Indica también company_name."
        )
    return matches[0]["grant_public_id"]


def _user_token() -> str:
    access_token = get_access_token()
    if access_token is None:
        raise PermissionError("OAuth access token missing")
    return access_token.token


async def _audit(token, grant_public_id, operation, started, error=None, row_count=None):
    try:
        details = {
            "operation": operation,
            "status": "failure" if error else "success",
            "duration_ms": round((perf_counter() - started) * 1000),
        }
        if error:
            details["error_code"] = type(error).__name__
        if row_count is not None:
            details["row_count"] = row_count
        await audit_result(token, grant_public_id, **details)
    except Exception:
        pass


def create_mcp(auth_provider) -> FastMCP:
    server = FastMCP("PowerBI_Aklara", auth=auth_provider)

    @server.tool(auth=require_scopes("mcp:models:list"))
    async def list_semantic_models() -> str:
        """Lista los modelos disponibles sin exponer ids ni metadatos internos."""
        access_list = await list_access(_user_token())
        return json.dumps(
            {"semantic_models": _public_models(access_list)},
            ensure_ascii=False,
            indent=2,
        )

    @server.tool(auth=require_scopes("mcp:models:read"))
    async def get_powerbi_schema(
        model_name: str, company_name: str | None = None
    ) -> str:
        """Obtiene el esquema usando el nombre visible del modelo."""
        token, started, error = _user_token(), perf_counter(), None
        grant_public_id = None
        try:
            grant_public_id = await _resolve_model_selector(
                token, model_name, company_name
            )
            context = await resolve_access(
                token, grant_public_id, "mcp.model.schema.read"
            )
            return await asyncio.to_thread(
                get_semantic_model_schema_tom,
                context["workspace_name"],
                context["dataset_name"],
                context["powerbi_access_token"],
            )
        except Exception as exc:
            error = exc
            raise
        finally:
            if grant_public_id:
                await _audit(token, grant_public_id, "schema.read", started, error)

    @server.tool(auth=require_scopes("mcp:models:query"))
    async def execute_dax(
        model_name: str, query: str, company_name: str | None = None
    ) -> str:
        """Ejecuta DAX en el modelo identificado por su nombre visible."""
        token, started, error, row_count = (
            _user_token(),
            perf_counter(),
            None,
            None,
        )
        grant_public_id = None
        try:
            grant_public_id = await _resolve_model_selector(
                token, model_name, company_name
            )
            context = await resolve_access(
                token, grant_public_id, "mcp.model.query.execute"
            )
            result = await execute_dax_query(
                context["dataset_id"], query, context["powerbi_access_token"]
            )
            row_count = sum(
                len(table.get("rows", []))
                for item in result.get("results", [])
                for table in item.get("tables", [])
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as exc:
            error = exc
            raise
        finally:
            if grant_public_id:
                await _audit(
                    token, grant_public_id, "query.execute", started, error, row_count
                )

    @server.tool(auth=require_scopes("mcp:models:write"))
    async def create_measure(
        model_name: str,
        table_name: str,
        measure_name: str,
        dax_expression: str,
        description: str = "",
        company_name: str | None = None,
    ) -> str:
        """Crea una medida en el modelo identificado por su nombre visible."""
        token, started, error = _user_token(), perf_counter(), None
        grant_public_id = None
        try:
            grant_public_id = await _resolve_model_selector(
                token, model_name, company_name
            )
            context = await resolve_access(
                token, grant_public_id, "mcp.model.measure.create"
            )
            return await asyncio.to_thread(
                create_measure_tom,
                context["workspace_name"],
                context["dataset_name"],
                table_name,
                measure_name,
                dax_expression,
                description,
                context["powerbi_access_token"],
            )
        except Exception as exc:
            error = exc
            raise
        finally:
            if grant_public_id:
                await _audit(token, grant_public_id, "measure.create", started, error)

    @server.tool(auth=require_scopes("mcp:models:write"))
    async def update_measure(
        model_name: str,
        table_name: str,
        measure_name: str,
        dax_expression: str,
        description: str = "",
        company_name: str | None = None,
    ) -> str:
        """Actualiza una medida en el modelo identificado por su nombre visible."""
        token, started, error = _user_token(), perf_counter(), None
        grant_public_id = None
        try:
            grant_public_id = await _resolve_model_selector(
                token, model_name, company_name
            )
            context = await resolve_access(
                token, grant_public_id, "mcp.model.measure.update"
            )
            return await asyncio.to_thread(
                update_measure_tom,
                context["workspace_name"],
                context["dataset_name"],
                table_name,
                measure_name,
                dax_expression,
                description,
                context["powerbi_access_token"],
            )
        except Exception as exc:
            error = exc
            raise
        finally:
            if grant_public_id:
                await _audit(token, grant_public_id, "measure.update", started, error)

    return server
