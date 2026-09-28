"""Resolve the ``report_coordinator`` model role into a ready ``LLMReportCoordinator``.

Mirrors ``writer_factory.py`` deliberately: kept apart from ``coordinator.py`` so
the coordinator itself has no database/Flask dependency, and model selection
uses the same persisted catalog (report > empresa > global scope) as every other
role. Nothing here names a provider or a physical model: whichever model an
admin assigns to ``report_coordinator`` in Administración > Modelos de IA >
Componentes is what runs. Unlike the writer (mandatory), an unassigned or
misconfigured ``report_coordinator`` must never block a report: callers (see
``coordination.CoordinationRunner``) treat every error raised here as a safe,
contained failure that falls back to the ``fixed`` behaviour.
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

from .coordinator import COORDINATOR_ROLE, CoordinatorConfigurationError, LLMReportCoordinator


def resolve_report_coordinator(config: dict[str, Any], report_id: int, *, runtime=None) -> LLMReportCoordinator:
    """Preflight and build the coordinator for ``report_id`` (synchronous: touches the DB).

    Raises ``CoordinatorConfigurationError`` when the role is unassigned or its
    model cannot run, and lets ``ai_billing.BillingLimitExceeded`` propagate
    untouched (exactly like ``resolve_report_writer``) -- callers are expected to
    contain both, since the coordinator is optional and its own failures must
    never block report generation.
    """
    report = db.session.get(Report, report_id)
    if report is None:
        raise CoordinatorConfigurationError(f"Report no encontrado: {report_id}")
    try:
        settings = build_runtime_settings(dict(config))
        billing_context = ai_billing.resolve_report_billing_context(report)
        resolver = model_catalog.build_catalog_resolver(
            settings, report_id=report.id, empresa_id=billing_context.empresa_id, config=dict(config),
        )
        component = resolver.component(COORDINATOR_ROLE, report_id=report.id, empresa_id=billing_context.empresa_id)
    except (TypeError, ValueError, ai_billing.BillingConfigurationError) as exc:
        raise CoordinatorConfigurationError(str(exc)) from exc
    model = component.model
    if component.strategy != "model" or model is None:
        raise CoordinatorConfigurationError(
            "El rol report_coordinator no tiene un modelo asignado. Asignalo en Administración > Modelos de "
            "IA > Componentes (opcional: sin asignar, la estrategia coordinated se comporta como fixed).")
    if not model.api_key:
        raise CoordinatorConfigurationError("El modelo del rol report_coordinator no tiene credencial configurada.")
    try:
        profile = PROFILES.get(model.family_key) if model.family_key else None
        if profile is not None:
            profile.validate(model)
        ai_billing.validate_pricing_coverage(model)
    except (ai_billing.BillingConfigurationError, ValueError) as exc:
        raise CoordinatorConfigurationError(f"El modelo report_coordinator no puede ejecutarse: {exc}") from exc
    ai_billing.enforce_limit_for_report(report)
    runtime = runtime or LiteLLMRuntime(cost_resolver=ai_billing.generation_cost_details)
    return LLMReportCoordinator(runtime, model, cache_scope=report_cache_scope(billing_context.empresa_id, report.id))
