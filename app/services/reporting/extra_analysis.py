"""Executes coordinator-requested analyses through the existing ``AnalyticsExecutor``.

This module owns exactly one responsibility: turn a ``RequestedAnalysis`` into a
``ReportSection``. It never generates DAX, never talks to Power BI directly and
never lets the coordinator choose a model, tier or skill -- it reuses the same
``AnalyticsExecutor`` and the same ``report_id``/model/tier configured on the
``ReportDefinition`` that ``ReportGenerator`` already uses, with dynamic skill
routing (no pinned skills yet for coordinator-originated questions, per V1.1
scope) and ``execution_source="report"``.
"""
from __future__ import annotations

from app.services.analytics import AnalyticsExecutor, AnalyticsRequest

from .analytics_bridge import section_from_exception, section_from_result
from .contracts import ReportDefinition, ReportSection
from .coordinator_contracts import RequestedAnalysis


async def run_analysis(
    requested_analysis: RequestedAnalysis,
    *,
    definition: ReportDefinition,
    analytics: AnalyticsExecutor,
    report_run_id: str,
    section_key: str,
) -> ReportSection:
    """Execute one coordinator-requested analysis and return it as a ``ReportSection``.

    Any failure -- including a global ``AnalyticsError`` (billing/config/model) --
    is contained here as a ``had_error=True`` section: an optional extra analysis
    must never abort the pipeline or invalidate the mandatory sections already in
    the draft (see ``CoordinationRunner``, which relies on this contract).
    """
    title = requested_analysis.question if len(requested_analysis.question) <= 100 \
        else requested_analysis.question[:99] + "…"
    request = AnalyticsRequest(
        report_id=definition.report_id,
        question=requested_analysis.question,
        history=[],
        source="report",
        execution_id=f"report:{report_run_id}:{section_key}",
        model_key=definition.analysis_model_key,
        service_tier=definition.analysis_service_tier,
        required_skill_keys=[],
        trace_context={
            "report_run_id": report_run_id,
            "report_stage": "extra_analysis",
            "report_section_key": section_key,
        },
    )
    try:
        result = await analytics.execute(request)
    except Exception as exc:  # noqa: BLE001 -- contained by design, see docstring (includes AnalyticsError)
        return section_from_exception(
            key=section_key, title=title, question=requested_analysis.question, exc=exc,
            report_run_id=report_run_id, origin="coordinator", purpose=requested_analysis.purpose,
            related_section_keys=tuple(requested_analysis.related_section_keys),
        )
    return section_from_result(
        key=section_key, title=title, question=requested_analysis.question, result=result,
        report_run_id=report_run_id, origin="coordinator", purpose=requested_analysis.purpose,
        related_section_keys=tuple(requested_analysis.related_section_keys),
    )
