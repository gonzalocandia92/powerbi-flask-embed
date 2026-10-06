"""Resolve the ``structure_planner`` model role into a ready ``LLMStructurePlanner``.

Mirrors ``coordinator_factory.py``: model selection goes through the persisted catalog
(report > empresa > global), nothing here names a provider or a physical model, and every failure is
raised as a ``StructurePlannerError`` so ``StructureCompiler`` degrades to the default structure
instead of breaking the definition flow. Unassigned role => the structure simply isn't interpreted.
"""
from __future__ import annotations

from typing import Any

from app import db
from app.models import Report
from app.services import ai_billing, model_catalog
from app.services.agent_core import build_runtime_settings
from app.services.llm import LiteLLMRuntime
from app.services.llm.contracts import report_cache_scope
from app.services.llm.profiles import PROFILES

from .structure_contracts import (
    StructurePlannerBillingLimitError, StructurePlannerConfigurationError,
)
from .structure_planner import STRUCTURE_PLANNER_ROLE, LLMStructurePlanner


def resolve_structure_planner(config: dict[str, Any], report_id: int, *, runtime=None) -> LLMStructurePlanner:
    report = db.session.get(Report, report_id)
    if report is None:
        raise StructurePlannerConfigurationError(f"Report no encontrado: {report_id}")
    try:
        settings = build_runtime_settings(dict(config))
        billing_context = ai_billing.resolve_report_billing_context(report)
        resolver = model_catalog.build_catalog_resolver(
            settings, report_id=report.id, empresa_id=billing_context.empresa_id, config=dict(config))
        component = resolver.component(STRUCTURE_PLANNER_ROLE, report_id=report.id, empresa_id=billing_context.empresa_id)
    except (TypeError, ValueError, ai_billing.BillingConfigurationError) as exc:
        raise StructurePlannerConfigurationError(str(exc)) from exc
    model = component.model
    if component.strategy != "model" or model is None:
        raise StructurePlannerConfigurationError(
            "El rol structure_planner no tiene un modelo asignado. Asignalo en Administración > Modelos de IA > "
            "Componentes (opcional: sin asignar, se usa la estructura estándar).")
    if not model.api_key:
        raise StructurePlannerConfigurationError("El modelo del rol structure_planner no tiene credencial configurada.")
    try:
        profile = PROFILES.get(model.family_key) if model.family_key else None
        if profile is not None:
            profile.validate(model)
        ai_billing.validate_pricing_coverage(model)
    except (ai_billing.BillingConfigurationError, ValueError) as exc:
        raise StructurePlannerConfigurationError(f"El modelo structure_planner no puede ejecutarse: {exc}") from exc
    try:
        ai_billing.enforce_limit_for_report(report)
    except ai_billing.BillingLimitExceeded as exc:
        raise StructurePlannerBillingLimitError(str(exc)) from exc
    runtime = runtime or LiteLLMRuntime(cost_resolver=ai_billing.generation_cost_details)
    return LLMStructurePlanner(runtime, model, cache_scope=report_cache_scope(billing_context.empresa_id, report.id))
