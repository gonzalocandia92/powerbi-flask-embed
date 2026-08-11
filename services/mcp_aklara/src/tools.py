"""OAuth-protected MCP tools with human-readable model selectors."""

import asyncio
import json
from time import perf_counter

from fastmcp import FastMCP
from fastmcp.server.auth import require_scopes
from fastmcp.server.dependencies import get_access_token
from mcp.types import ToolAnnotations

from .auth.broker import (
    audit_result,
    get_relevant_schema as broker_get_relevant_schema,
    list_access,
    list_analytics_skills as broker_list_analytics_skills,
    resolve_access,
    select_analytics_skills as broker_select_analytics_skills,
)
from .powerbi.rest_api import execute_dax_query
from .powerbi.xmla_tom import create_measure as create_measure_tom
from .powerbi.xmla_tom import get_semantic_model_schema_tom
from .powerbi.xmla_tom import update_measure as update_measure_tom


SERVER_INSTRUCTIONS = """
Este servidor permite consultar y modificar modelos semanticos de Power BI.

Flujo preferido para preguntas analiticas y operaciones DAX:
1. Si el modelo no esta identificado, usa list_semantic_models.
2. Llama primero a select_analytics_skills con la pregunta original del usuario.
3. Si vas a construir, revisar o modificar DAX, llama despues a get_relevant_schema
   para recuperar las medidas y tablas relacionadas. Pasa las claves devueltas por
   select_analytics_skills cuando existan skills seleccionadas.
4. Construye DAX usando las instrucciones seleccionadas y objetos confirmados en
   el schema; nunca inventes medidas, tablas ni columnas.
5. Usa execute_dax unicamente despues de completar los pasos anteriores.
6. Usa get_powerbi_schema cuando get_relevant_schema recomiende fallback, falle el
   indice, necesites una inspeccion exhaustiva o el usuario pida el schema completo.
7. Antes de create_measure o update_measure, selecciona skills y consulta primero
   el schema relevante; usa el schema completo como fallback.

list_analytics_skills es opcional y sirve para explorar el catalogo; no hace falta
llamarla antes de select_analytics_skills. No uses tools analiticas para preguntas
sobre las tools, funciones o configuracion del propio conector MCP.
""".strip()


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
    server = FastMCP(
        "PowerBI_Aklara",
        auth=auth_provider,
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.tool(
        auth=require_scopes("mcp:models:list"),
        description=(
            "Paso 0 opcional: lista los modelos semanticos disponibles. Usala solo "
            "cuando el usuario no haya identificado claramente el modelo."
        ),
        annotations=ToolAnnotations(
            title="Listar modelos semanticos",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def list_semantic_models() -> str:
        access_list = await list_access(_user_token())
        return json.dumps(
            {"semantic_models": _public_models(access_list)},
            ensure_ascii=False,
            indent=2,
        )

    @server.tool(
        auth=require_scopes("mcp:models:read"),
        description=(
            "Exploracion opcional: lista capacidades analiticas del modelo. No es "
            "un requisito previo para select_analytics_skills y no debe usarse para "
            "listar tools, funciones u operaciones del conector MCP."
        ),
        annotations=ToolAnnotations(
            title="Listar skills analiticas",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def list_analytics_skills(
        model_name: str,
        company_name: str | None = None,
        domain_key: str | None = None,
    ) -> str:
        token = _user_token()
        grant_public_id = await _resolve_model_selector(token, model_name, company_name)
        result = await broker_list_analytics_skills(
            token, grant_public_id, domain_key=domain_key
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool(
        auth=require_scopes("mcp:models:read"),
        description=(
            "Paso 1 requerido para preguntas analiticas: selecciona las skills usando "
            "la pregunta original antes de consultar el schema, construir DAX o llamar "
            "a execute_dax. No usar para preguntas sobre el conector ni sus tools."
        ),
        annotations=ToolAnnotations(
            title="Seleccionar skills analiticas",
            readOnlyHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def select_analytics_skills(
        model_name: str,
        question: str,
        company_name: str | None = None,
        candidate_skill_keys: list[str] | None = None,
    ) -> str:
        token = _user_token()
        grant_public_id = await _resolve_model_selector(token, model_name, company_name)
        result = await broker_select_analytics_skills(
            token,
            grant_public_id,
            question,
            candidate_skill_keys=candidate_skill_keys,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool(
        auth=require_scopes("mcp:models:read"),
        description=(
            "Paso 2 preferido para construir o revisar DAX: recupera mediante embeddings "
            "solo las tablas y medidas relacionadas con la pregunta. Llamala despues de "
            "select_analytics_skills y pasa selected_skill_keys cuando haya seleccion. "
            "Si recomienda fallback o falla el indice, usa get_powerbi_schema."
        ),
        annotations=ToolAnnotations(
            title="Obtener schema relevante",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def get_relevant_schema(
        model_name: str,
        question: str,
        company_name: str | None = None,
        selected_skill_keys: list[str] | None = None,
    ) -> str:
        token = _user_token()
        grant_public_id = await _resolve_model_selector(token, model_name, company_name)
        result = await broker_get_relevant_schema(
            token,
            grant_public_id,
            question,
            selected_skill_keys=selected_skill_keys,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool(
        auth=require_scopes("mcp:models:read"),
        description=(
            "Fallback de schema completo: devuelve todo el modelo cuando "
            "get_relevant_schema no esta disponible, recomienda fallback, faltan objetos, "
            "se necesita una inspeccion exhaustiva o el usuario lo solicita expresamente."
        ),
        annotations=ToolAnnotations(
            title="Obtener schema completo de Power BI",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def get_powerbi_schema(
        model_name: str, company_name: str | None = None
    ) -> str:
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

    @server.tool(
        auth=require_scopes("mcp:models:query"),
        description=(
            "Paso final: ejecuta una consulta DAX de solo lectura. Antes debes llamar "
            "a select_analytics_skills con la pregunta original y confirmar los objetos "
            "con get_relevant_schema. Usa get_powerbi_schema si hubo fallback. Nunca "
            "inventes objetos del modelo."
        ),
        annotations=ToolAnnotations(
            title="Ejecutar consulta DAX",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def execute_dax(
        model_name: str, query: str, company_name: str | None = None
    ) -> str:
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

    @server.tool(
        auth=require_scopes("mcp:models:write"),
        description=(
            "Crea una medida. Antes debes llamar a select_analytics_skills con el "
            "cambio solicitado y luego a get_relevant_schema para confirmar la tabla y "
            "los objetos usados. Usa get_powerbi_schema si se recomienda fallback."
        ),
        annotations=ToolAnnotations(
            title="Crear medida",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def create_measure(
        model_name: str,
        table_name: str,
        measure_name: str,
        dax_expression: str,
        description: str = "",
        company_name: str | None = None,
    ) -> str:
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

    @server.tool(
        auth=require_scopes("mcp:models:write"),
        description=(
            "Actualiza y reemplaza la definicion de una medida existente. Antes debes "
            "llamar a select_analytics_skills y get_relevant_schema para confirmar la "
            "medida y sus objetos. Usa get_powerbi_schema si se recomienda fallback."
        ),
        annotations=ToolAnnotations(
            title="Actualizar medida",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def update_measure(
        model_name: str,
        table_name: str,
        measure_name: str,
        dax_expression: str,
        description: str = "",
        company_name: str | None = None,
    ) -> str:
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
