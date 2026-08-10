"""Shared scope resolution for curated analytics skills."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from app import db
from app.models import AnalyticsSkill


@dataclass(frozen=True)
class SkillScopeContext:
    """Authorized scope used to resolve the effective analytics catalog."""

    dataset_id: Optional[str]
    empresa_id: Optional[int]
    report_id: Optional[int] = None


def skill_scope_priority(scope: str) -> int:
    return {"global": 0, "empresa": 1, "dataset": 2, "report": 3}.get(scope, 0)


def skill_scope_filter(context: SkillScopeContext):
    filters = [
        db.and_(
            AnalyticsSkill.report_id_fk.is_(None),
            AnalyticsSkill.empresa_id_fk.is_(None),
            AnalyticsSkill.dataset_id.is_(None),
        )
    ]
    if context.empresa_id is not None:
        filters.append(AnalyticsSkill.empresa_id_fk == int(context.empresa_id))
    if context.dataset_id:
        filters.append(AnalyticsSkill.dataset_id == str(context.dataset_id))
    if context.report_id is not None:
        filters.append(AnalyticsSkill.report_id_fk == int(context.report_id))
    return db.or_(*filters)


def _normalized_keys(values: Optional[Iterable[str]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        key = str(value or "").strip().casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _effective_rank(skill: AnalyticsSkill) -> tuple:
    return (
        skill_scope_priority(skill.scope),
        int(skill.version or 0),
        skill.updated_at or skill.created_at,
        int(skill.id or 0),
    )


def list_effective_skills(
    context: SkillScopeContext,
    *,
    domain_key: Optional[str] = None,
    skill_keys: Optional[Iterable[str]] = None,
) -> list[AnalyticsSkill]:
    """Return one deterministic active skill per key for an authorized scope."""

    query = AnalyticsSkill.query.filter(
        AnalyticsSkill.is_active.is_(True),
        skill_scope_filter(context),
    )
    normalized_domain = str(domain_key or "").strip().casefold()
    if normalized_domain:
        query = query.filter(db.func.lower(AnalyticsSkill.domain_key) == normalized_domain)

    normalized_keys = _normalized_keys(skill_keys)
    if skill_keys is not None:
        if not normalized_keys:
            return []
        query = query.filter(db.func.lower(AnalyticsSkill.skill_key).in_(normalized_keys))

    best_by_key: dict[str, AnalyticsSkill] = {}
    for skill in query.all():
        key = str(skill.skill_key or "").strip().casefold()
        current = best_by_key.get(key)
        if current is None or _effective_rank(skill) > _effective_rank(current):
            best_by_key[key] = skill

    return sorted(
        best_by_key.values(),
        key=lambda skill: (
            str(skill.domain_key or "").casefold(),
            str(skill.title or "").casefold(),
            str(skill.skill_key or "").casefold(),
        ),
    )


def unknown_skill_keys(
    requested_keys: Iterable[str],
    effective_skills: Iterable[AnalyticsSkill],
) -> list[str]:
    available = {str(skill.skill_key or "").strip().casefold() for skill in effective_skills}
    unknown: list[str] = []
    seen: set[str] = set()
    for value in requested_keys:
        display = str(value or "").strip()
        normalized = display.casefold()
        if display and normalized not in available and normalized not in seen:
            seen.add(normalized)
            unknown.append(display)
    return unknown
