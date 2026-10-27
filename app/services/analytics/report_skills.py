"""Effective analytics skills of a Report, resolved with the engine's own scope rules.

Consumers (admin screens, validators) ask for "the skills available for this
report" without knowing how the scope is built: Report -> dataset + empresa ->
SkillScopeContext -> effective skills (report > dataset > empresa > global).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import requests

from app.models import AnalyticsSkill, Report
from app.services import ai_billing
from app.services.skill_catalog import SkillScopeContext, list_effective_skills, unknown_skill_keys
from app.utils.powerbi import get_current_dataset_id


@dataclass(frozen=True)
class ReportSkillCatalog:
    skills: list[AnalyticsSkill] = field(default_factory=list)
    # False when the dataset could not be resolved, so dataset-scoped skills may be missing.
    dataset_resolved: bool = True

    def to_payload(self) -> dict:
        return {
            "dataset_resolved": self.dataset_resolved,
            "skills": [{
                "skill_key": skill.skill_key, "title": skill.title,
                "domain_key": skill.domain_key, "scope": skill.scope,
            } for skill in self.skills],
        }

    def missing(self, skill_keys: Iterable[str]) -> list[str]:
        """Requested keys that are not effective for this report."""
        return unknown_skill_keys(skill_keys, self.skills)


def report_skill_scope(report: Report) -> tuple[SkillScopeContext, bool]:
    """Scope context for a Report, plus whether its dataset could be resolved."""
    dataset_resolved = True
    try:
        dataset_id = get_current_dataset_id(report)
    except (requests.RequestException, RuntimeError, KeyError):
        dataset_id, dataset_resolved = None, False
    try:
        empresa_id = ai_billing.resolve_report_billing_context(report).empresa_id
    except ai_billing.BillingConfigurationError:
        empresa_id = None
    return SkillScopeContext(dataset_id=dataset_id, empresa_id=empresa_id, report_id=report.id), dataset_resolved


def list_effective_skills_for_report(report: Report) -> ReportSkillCatalog:
    scope, dataset_resolved = report_skill_scope(report)
    return ReportSkillCatalog(skills=list_effective_skills(scope), dataset_resolved=dataset_resolved)
