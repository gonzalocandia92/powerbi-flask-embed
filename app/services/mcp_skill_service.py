"""Public-safe analytics skill catalog and selection for MCP grants."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from flask import current_app

from app import db
from app.models import SchemaEmbedding
from app.services import ai_billing
from app.services.skill_catalog import SkillScopeContext, list_effective_skills
from app.services.skill_router import build_skill_router_settings, resolve_skill_route


class SkillSelectionProviderError(RuntimeError):
    """Raised after a provider failure has been converted to a safe MCP error."""


class SkillReportContextError(RuntimeError):
    """Safe, actionable failure while resolving a functional report context."""

    def __init__(self, code: str, message: str, *, source: str, association_count: int):
        super().__init__(message)
        self.code = code
        self.source = source
        self.association_count = association_count


@dataclass(frozen=True)
class McpSkillScopeResolution:
    context: SkillScopeContext
    source: str
    association_count: int


def report_ids_for_dataset(dataset_id: Optional[str]) -> list[int]:
    """Return the distinct functional reports evidenced by schema embeddings."""

    normalized_dataset_id = str(dataset_id or "").strip()
    if not normalized_dataset_id:
        return []
    rows = (
        db.session.query(SchemaEmbedding.report_id_fk)
        .filter(SchemaEmbedding.dataset_id == normalized_dataset_id)
        .distinct()
        .order_by(SchemaEmbedding.report_id_fk)
        .all()
    )
    return [int(report_id) for report_id, in rows]


def resolve_mcp_skill_scope(config, empresa_id: Optional[int]) -> McpSkillScopeResolution:
    """Resolve MCP scope without treating the credential report as business context."""

    report_ids = report_ids_for_dataset(config.dataset_id)
    explicit_report_id = getattr(config, "skill_report_id_fk", None)
    if explicit_report_id is not None:
        if int(explicit_report_id) not in report_ids:
            raise SkillReportContextError(
                "skill_report_context_invalid",
                "El reporte configurado para skills no corresponde al dataset del modelo.",
                source="invalid",
                association_count=len(report_ids),
            )
        report_id = int(explicit_report_id)
        source = "explicit"
    elif len(report_ids) == 1:
        report_id = report_ids[0]
        source = "dataset_unique"
    elif len(report_ids) > 1:
        raise SkillReportContextError(
            "skill_report_context_ambiguous",
            "El modelo tiene mas de un reporte asociado; configura un reporte de skills explicito.",
            source="ambiguous",
            association_count=len(report_ids),
        )
    else:
        report_id = None
        source = "none"

    return McpSkillScopeResolution(
        context=SkillScopeContext(
            dataset_id=config.dataset_id,
            empresa_id=empresa_id,
            report_id=report_id,
        ),
        source=source,
        association_count=len(report_ids),
    )


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


def _required_schema_items(metadata: dict[str, Any]) -> list[dict[str, str]]:
    raw_items = metadata.get("required_schema_items")
    if not isinstance(raw_items, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("item_type") or "").strip().lower()
        item_name = str(item.get("item_name") or "").strip()
        key = (item_type, item_name.casefold())
        if item_type in {"measure", "table"} and item_name and key not in seen:
            seen.add(key)
            result.append({"item_type": item_type, "item_name": item_name})
    return result


def serialize_skill_card(skill) -> dict[str, Any]:
    return {
        "key": skill.skill_key,
        "domain": skill.domain_key,
        "title": skill.title,
        "description": skill.description or "",
        "when_to_use": skill.routing_text or "",
        "scope": skill.scope,
        "priority": skill.priority or "normal",
    }


def list_grant_skills(
    grant,
    *,
    domain_key: Optional[str] = None,
    resolution: Optional[McpSkillScopeResolution] = None,
) -> dict[str, Any]:
    resolution = resolution or resolve_mcp_skill_scope(grant.config, grant.empresa_id)
    skills = list_effective_skills(resolution.context, domain_key=domain_key)
    return {"skills": [serialize_skill_card(skill) for skill in skills]}


def _persist_usage_events(
    grant, events: Iterable[dict[str, Any]], *, report_id: Optional[int]
) -> None:
    for raw_event in events:
        event = dict(raw_event)
        metadata_json = event.pop("metadata_json", None)
        ai_billing.record_ai_usage_event(
            workspace_id=grant.config.workspace_id_fk,
            report_id=report_id,
            empresa_id=grant.empresa_id,
            billing_scope_type=ai_billing.BILLING_SCOPE_EMPRESA,
            billing_scope_id=str(grant.empresa_id),
            metadata_json=metadata_json,
            **event,
        )


def _serialize_selected_skills(route, *, max_skill_chars: int) -> tuple[list[dict[str, Any]], bool]:
    companion_keys = {
        str(key or "").casefold() for key in route.resolved_companion_skill_keys
    }
    remaining = max(0, int(max_skill_chars or 0))
    truncated_any = False
    serialized: list[dict[str, Any]] = []
    for skill in route.selected_skills:
        content = str(skill.content or "").strip()
        truncated = len(content) > remaining
        if truncated:
            content = content[:remaining].rstrip()
            truncated_any = True
        remaining = max(0, remaining - len(content))
        metadata = skill.metadata if isinstance(skill.metadata, dict) else {}
        serialized.append(
            {
                "key": skill.skill_key,
                "domain": skill.domain_key,
                "role": "companion" if skill.skill_key.casefold() in companion_keys else "primary",
                "content": content,
                "canonical_measures": _string_list(metadata.get("canonical_measures")),
                "required_schema_items": _required_schema_items(metadata),
                "preferred_tables": _string_list(metadata.get("preferred_tables")),
                "allowed_dimensions": _string_list(metadata.get("allowed_dimensions")),
                "constraints": _string_list(metadata.get("constraints")),
            }
        )
    return serialized, truncated_any


async def select_grant_skills(
    grant,
    *,
    question: str,
    candidate_skill_keys: Optional[list[str]] = None,
    resolution: Optional[McpSkillScopeResolution] = None,
) -> dict[str, Any]:
    normalized_question = str(question or "").strip()
    if not normalized_question:
        raise ValueError("question es obligatorio.")
    if candidate_skill_keys is not None and not isinstance(candidate_skill_keys, list):
        raise ValueError("candidate_skill_keys debe ser una lista de strings.")
    if candidate_skill_keys is not None and any(
        not isinstance(key, str) or not key.strip() for key in candidate_skill_keys
    ):
        raise ValueError("candidate_skill_keys solo acepta strings no vacios.")

    resolution = resolution or resolve_mcp_skill_scope(grant.config, grant.empresa_id)
    settings = build_skill_router_settings(dict(current_app.config))
    usage_totals = {"input_tokens": 0, "output_tokens": 0}
    usage_events: list[dict[str, Any]] = []
    route = await resolve_skill_route(
        user_message=normalized_question,
        report_id=resolution.context.report_id,
        empresa_id=grant.empresa_id,
        dataset_id=grant.config.dataset_id,
        settings=settings,
        usage_totals=usage_totals,
        ai_usage_events=usage_events,
        candidate_skill_keys=candidate_skill_keys,
        force_enabled=True,
    )
    _persist_usage_events(
        grant, usage_events, report_id=resolution.context.report_id
    )

    if route.strategy == "router_error":
        raise SkillSelectionProviderError(
            "No se pudo resolver la seleccion de skills en este momento."
        )

    selected, truncated = _serialize_selected_skills(
        route,
        max_skill_chars=settings.max_skill_chars,
    )
    return {
        "selection": {
            "matched": bool(selected),
            "confidence": round(float(route.confidence or 0.0), 4),
        },
        "skills": selected,
        "truncated": truncated,
        "warnings": (
            ["Faltan skills companion requeridas para completar la seleccion."]
            if route.missing_companion_skill_keys
            else []
        ),
    }
