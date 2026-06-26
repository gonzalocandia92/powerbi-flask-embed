"""Helpers for resolving persisted agent prompt instructions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from app.models import AgentPromptConfig, Report


PROMPT_SCOPE_GLOBAL = "global"
PROMPT_SCOPE_EMPRESA = "empresa"
PROMPT_SCOPE_REPORT = "report"


@dataclass(frozen=True)
class ResolvedPromptInstruction:
    scope_type: str
    scope_id: Optional[str]
    title: str
    instructions: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def resolve_report_prompt_empresa_id(report: Optional[Report]) -> Optional[int]:
    """Resolve the empresa scope using the same precedence as AI billing."""
    if report is None:
        return None

    empresa_id = getattr(report, "empresa_facturadora_id", None)
    if empresa_id:
        return int(empresa_id)

    associated_companies = list(getattr(report, "empresas", []) or [])
    if len(associated_companies) == 1:
        return int(associated_companies[0].id)

    return None


def _active_prompt_config(
    *,
    scope_type: str,
    scope_id: Optional[str],
    as_of: Optional[datetime] = None,
) -> Optional[AgentPromptConfig]:
    reference_time = as_of or utcnow()
    query = AgentPromptConfig.query.filter(
        AgentPromptConfig.scope_type == scope_type,
        AgentPromptConfig.is_active.is_(True),
        AgentPromptConfig.starts_at.is_(None) | (AgentPromptConfig.starts_at <= reference_time),
        AgentPromptConfig.ends_at.is_(None) | (AgentPromptConfig.ends_at >= reference_time),
    )
    if scope_id is None:
        query = query.filter(AgentPromptConfig.scope_id.is_(None))
    else:
        query = query.filter(AgentPromptConfig.scope_id == scope_id)
    return query.order_by(AgentPromptConfig.id.desc()).first()


def resolve_agent_prompt_instructions(
    report: Optional[Report],
    *,
    as_of: Optional[datetime] = None,
) -> List[ResolvedPromptInstruction]:
    """Return active prompt instructions ordered from broadest to most specific."""
    resolved: List[ResolvedPromptInstruction] = []

    global_config = _active_prompt_config(
        scope_type=PROMPT_SCOPE_GLOBAL,
        scope_id=None,
        as_of=as_of,
    )
    if global_config is not None:
        resolved.append(_to_instruction(global_config))

    empresa_id = resolve_report_prompt_empresa_id(report)
    if empresa_id is not None:
        empresa_config = _active_prompt_config(
            scope_type=PROMPT_SCOPE_EMPRESA,
            scope_id=str(empresa_id),
            as_of=as_of,
        )
        if empresa_config is not None:
            resolved.append(_to_instruction(empresa_config))

    report_id = getattr(report, "id", None) if report is not None else None
    if report_id is not None:
        report_config = _active_prompt_config(
            scope_type=PROMPT_SCOPE_REPORT,
            scope_id=str(report_id),
            as_of=as_of,
        )
        if report_config is not None:
            resolved.append(_to_instruction(report_config))

    return resolved


def _to_instruction(config: AgentPromptConfig) -> ResolvedPromptInstruction:
    return ResolvedPromptInstruction(
        scope_type=config.scope_type,
        scope_id=config.scope_id,
        title=config.title,
        instructions=config.instructions,
    )
