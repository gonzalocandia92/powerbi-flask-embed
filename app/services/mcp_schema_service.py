"""Public-safe relevant schema retrieval for authorized MCP grants."""
from __future__ import annotations

from typing import Any, Iterable, Optional

from flask import current_app

from app import db
from app.models import Report
from app.services import ai_billing
from app.services.mcp_skill_service import McpSkillScopeResolution, resolve_mcp_skill_scope
from app.services.schema_retrieval_service import (
    DEFAULT_MEASURE_CONTEXT_LIMIT,
    DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS,
    DEFAULT_TABLE_CONTEXT_LIMIT,
    RelevantSchemaProviderError,
    SchemaRetrievalUsage,
    VOYAGE_QUERY_EMBEDDING_MODEL,
    retrieve_relevant_schema,
)
from app.services.skill_catalog import list_effective_skills, unknown_skill_keys


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        normalized = text.casefold()
        if text and normalized not in seen:
            seen.add(normalized)
            result.append(text)
    return result


def _required_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("item_type") or "").strip().lower()
        item_name = str(item.get("item_name") or "").strip()
        key = (item_type, item_name.casefold())
        if item_type in {"table", "measure"} and item_name and key not in seen:
            seen.add(key)
            result.append({"item_type": item_type, "item_name": item_name})
    return result


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        normalized = text.casefold()
        if text and normalized not in seen:
            seen.add(normalized)
            result.append(text)
    return result


def _dedupe_required(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for item in values:
        key = (item["item_type"], item["item_name"].casefold())
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _skill_retrieval_hints(
    resolution: McpSkillScopeResolution,
    selected_skill_keys: Optional[list[str]],
) -> tuple[list[dict[str, str]], list[str], list[str], int]:
    if selected_skill_keys is None:
        return [], [], [], 0
    if not isinstance(selected_skill_keys, list) or any(
        not isinstance(key, str) or not key.strip() for key in selected_skill_keys
    ):
        raise ValueError("selected_skill_keys debe ser una lista de strings no vacios.")

    effective_catalog = list_effective_skills(resolution.context)
    if unknown_skill_keys(selected_skill_keys, effective_catalog):
        raise ValueError(
            "Una o mas selected_skill_keys no estan disponibles para este modelo."
        )
    selected = list_effective_skills(
        resolution.context, skill_keys=selected_skill_keys
    )

    required: list[dict[str, str]] = []
    preferred_measures: list[str] = []
    preferred_tables: list[str] = []
    for skill in selected:
        metadata = skill.metadata_json if isinstance(skill.metadata_json, dict) else {}
        required.extend(_required_items(metadata.get("required_schema_items")))
        preferred_measures.extend(_string_list(metadata.get("canonical_measures")))
        preferred_tables.extend(_string_list(metadata.get("preferred_tables")))
    return (
        _dedupe_required(required),
        _dedupe_strings(preferred_measures),
        _dedupe_strings(preferred_tables),
        len(selected),
    )


def _record_usage(
    grant,
    *,
    report: Optional[Report],
    usage: SchemaRetrievalUsage,
    status: str,
) -> None:
    ai_billing.record_ai_usage_event(
        report=report,
        workspace_id=(
            grant.config.workspace_id_fk
            or (report.workspace_id_fk if report is not None else None)
        ),
        report_id=report.id if report is not None else None,
        empresa_id=grant.empresa_id,
        billing_scope_type=ai_billing.BILLING_SCOPE_EMPRESA,
        billing_scope_id=str(grant.empresa_id),
        provider="voyageai",
        model=VOYAGE_QUERY_EMBEDDING_MODEL,
        event_type="embedding",
        source_type="mcp_schema_retrieval",
        trigger_type="user_request",
        operation_name="voyage-relevant-schema-query-embedding",
        status=status,
        input_tokens=usage.input_tokens,
        output_tokens=0,
        total_tokens=usage.input_tokens,
        metadata_json={
            "input_type": "query",
            "estimated_usage": usage.estimated,
            **(
                {"error_type": "voyage_provider_error"}
                if status == "error"
                else {}
            ),
        },
    )


async def get_grant_relevant_schema(
    grant,
    *,
    question: str,
    selected_skill_keys: Optional[list[str]] = None,
    resolution: Optional[McpSkillScopeResolution] = None,
) -> tuple[dict[str, Any], int]:
    normalized_question = str(question or "").strip()
    if not normalized_question:
        raise ValueError("question es obligatorio.")

    resolution = resolution or resolve_mcp_skill_scope(grant.config, grant.empresa_id)
    required, preferred_measures, preferred_tables, selected_skill_count = (
        _skill_retrieval_hints(resolution, selected_skill_keys)
    )

    report = (
        db.session.get(Report, resolution.context.report_id)
        if resolution.context.report_id is not None
        else None
    )
    table_limit = (
        report.schema_table_context_limit
        if report is not None and report.schema_table_context_limit is not None
        else DEFAULT_TABLE_CONTEXT_LIMIT
    )
    measure_limit = (
        report.schema_measure_context_limit
        if report is not None and report.schema_measure_context_limit is not None
        else DEFAULT_MEASURE_CONTEXT_LIMIT
    )
    try:
        timeout_seconds = int(
            current_app.config.get(
                "CHAT_SCHEMA_CONTEXT_TIMEOUT_SECONDS",
                DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS,
            )
        )
    except (TypeError, ValueError):
        timeout_seconds = DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS

    try:
        result = await retrieve_relevant_schema(
            dataset_id=grant.config.dataset_id,
            question=normalized_question,
            report_id=resolution.context.report_id,
            table_limit=table_limit,
            measure_limit=measure_limit,
            required_schema_items=required,
            preferred_measures=preferred_measures,
            preferred_tables=preferred_tables,
            timeout_seconds=timeout_seconds,
        )
    except RelevantSchemaProviderError as exc:
        _record_usage(
            grant,
            report=report,
            usage=exc.usage,
            status="error",
        )
        raise

    _record_usage(
        grant,
        report=report,
        usage=result.usage,
        status="success",
    )
    fallback_recommended = bool(result.missing_required_items or result.truncated)
    warnings: list[str] = []
    if result.missing_required_items:
        warnings.append(
            "Faltan objetos requeridos en el indice; usa get_powerbi_schema como fallback."
        )
    if result.truncated:
        warnings.append(
            "Los objetos requeridos superan el limite seguro; usa get_powerbi_schema."
        )

    tables = [
        {"type": item.item_type, "name": item.name, "content": item.content}
        for item in result.tables
    ]
    measures = [
        {"type": item.item_type, "name": item.name, "content": item.content}
        for item in result.measures
    ]
    return (
        {
            "schema": {"tables": tables, "measures": measures},
            "retrieval": {
                "matched": bool(tables or measures),
                "table_count": len(tables),
                "measure_count": len(measures),
                "skill_context_applied": selected_skill_count > 0,
                "fallback_recommended": fallback_recommended,
                "truncated": result.truncated,
            },
            "missing_required_items": result.missing_required_items,
            "warnings": warnings,
        },
        selected_skill_count,
    )
