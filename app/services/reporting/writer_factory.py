"""Resolve the ``report_writer`` model role into a ready ``LLMReportWriter``.

Kept apart from ``writer.py`` so the writer itself has no database/Flask
dependency. Model selection uses the persisted catalog (report > empresa > global
scope); nothing here names a provider or a physical model.
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

from .writer import (
    WRITER_ROLE, LLMReportWriter, ReportWriterConfigurationError,
)


def resolve_report_writer(config: dict[str, Any], report_id: int, *, runtime=None) -> LLMReportWriter:
    """Preflight and build the writer for ``report_id`` (synchronous: touches the DB).

    Raises ``ReportWriterConfigurationError`` when the role is unassigned or its model
    cannot run, and lets ``ai_billing.BillingLimitExceeded`` propagate untouched.
    """
    report = db.session.get(Report, report_id)
    if report is None:
        raise ReportWriterConfigurationError(f"Report no encontrado: {report_id}")
    try:
        settings = build_runtime_settings(dict(config))
        billing_context = ai_billing.resolve_report_billing_context(report)
        resolver = model_catalog.build_catalog_resolver(
            settings, report_id=report.id, empresa_id=billing_context.empresa_id, config=dict(config),
        )
        component = resolver.component(WRITER_ROLE, report_id=report.id, empresa_id=billing_context.empresa_id)
    except (TypeError, ValueError, ai_billing.BillingConfigurationError) as exc:
        raise ReportWriterConfigurationError(str(exc)) from exc
    model = component.model
    if component.strategy != "model" or model is None:
        raise ReportWriterConfigurationError(
            "El rol report_writer no tiene un modelo asignado. Asignalo en Administración > Modelos de IA > Componentes.")
    if not model.api_key:
        raise ReportWriterConfigurationError("El modelo del rol report_writer no tiene credencial configurada.")
    try:
        profile = PROFILES.get(model.family_key) if model.family_key else None
        if profile is not None:
            profile.validate(model)
        ai_billing.validate_pricing_coverage(model)
    except (ai_billing.BillingConfigurationError, ValueError) as exc:
        raise ReportWriterConfigurationError(f"El modelo report_writer no puede ejecutarse: {exc}") from exc
    ai_billing.enforce_limit_for_report(report)
    runtime = runtime or LiteLLMRuntime(cost_resolver=ai_billing.generation_cost_details)
    return LLMReportWriter(runtime, model, cache_scope=report_cache_scope(billing_context.empresa_id, report.id))
