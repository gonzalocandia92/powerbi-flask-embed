"""Resolve the ``report_writer`` model role into a ready writer.

Kept apart from ``writer.py`` / ``structured_writer.py`` so the writers themselves have no database/Flask
dependency. Model selection uses the persisted catalog (report > empresa > global scope); nothing here names a
provider or a physical model. Both writers (legacy 1.1 and structured 1.2) use the SAME ``report_writer`` role,
the same preflight and the same billing gate; only the class they instantiate differs.
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

from .structured_writer import LLMStructuredReportWriter
from .structured_writer_v13 import LLMStructuredReportWriterV13
from .structured_writer_v131 import LLMStructuredReportWriterV131
from .writer import (
    WRITER_ROLE, LLMReportWriter, ReportWriterConfigurationError,
)


def _resolve_writer_model(config: dict[str, Any], report_id: int):
    """Preflight of the ``report_writer`` role: ``(report, model, empresa_id)`` or a configuration error."""
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
    return report, model, billing_context.empresa_id


def resolve_report_writer(config: dict[str, Any], report_id: int, *, runtime=None) -> LLMReportWriter:
    """Preflight and build the LEGACY writer (``FinalReport`` 1.1) for ``report_id`` (synchronous: touches the DB).

    Raises ``ReportWriterConfigurationError`` when the role is unassigned or its model cannot run, and lets
    ``ai_billing.BillingLimitExceeded`` propagate untouched.
    """
    report, model, empresa_id = _resolve_writer_model(config, report_id)
    runtime = runtime or LiteLLMRuntime(cost_resolver=ai_billing.generation_cost_details)
    return LLMReportWriter(runtime, model, cache_scope=report_cache_scope(empresa_id, report.id))


def resolve_structured_report_writer(config: dict[str, Any], report_id: int, *,
                                     runtime=None) -> LLMStructuredReportWriter:
    """Same preflight, building the 1.2 STRUCTURED writer (kept for runs / tests that still target 1.2)."""
    report, model, empresa_id = _resolve_writer_model(config, report_id)
    runtime = runtime or LiteLLMRuntime(cost_resolver=ai_billing.generation_cost_details)
    return LLMStructuredReportWriter(runtime, model, cache_scope=report_cache_scope(empresa_id, report.id))


def resolve_structured_report_writer_v13(config: dict[str, Any], report_id: int, *,
                                         runtime=None) -> LLMStructuredReportWriterV13:
    """Same preflight and role, building the 1.3 (data story) STRUCTURED writer."""
    report, model, empresa_id = _resolve_writer_model(config, report_id)
    runtime = runtime or LiteLLMRuntime(cost_resolver=ai_billing.generation_cost_details)
    return LLMStructuredReportWriterV13(runtime, model, cache_scope=report_cache_scope(empresa_id, report.id))


def resolve_structured_report_writer_v131(config: dict[str, Any], report_id: int, *,
                                          runtime=None) -> LLMStructuredReportWriterV131:
    """Same preflight and role, building the 1.3.1 (typed evidence references) STRUCTURED writer."""
    report, model, empresa_id = _resolve_writer_model(config, report_id)
    runtime = runtime or LiteLLMRuntime(cost_resolver=ai_billing.generation_cost_details)
    return LLMStructuredReportWriterV131(runtime, model, cache_scope=report_cache_scope(empresa_id, report.id))
