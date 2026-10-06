"""Single place that wires a ``ReportPipeline`` to the AI usage ledger.

Two pipelines coexist, chosen HERE and nowhere else (no version checks in routes, worker, executor, writers or
renderers):

* ``build_report_pipeline``            legacy: ReportDraft -> writer -> ``FinalReport`` 1.1 -> ``html-v1``.
                                       Used by the synchronous ``/generate`` debug/comparison path.
* ``build_structured_report_pipeline`` structured: ReportDraft + frozen structure -> composer -> structured writer
                                       -> ``FinalReport`` 1.3 by default (-> ``html-v3``; V1.5 data story), or 1.2
                                       (-> ``html-v2``) when asked. ``build_run_pipeline`` (persisted ``ReportRun``)
                                       uses it with the CURRENT schema. The schema -> (writer, composer, renderer)
                                       association lives in ``_STRUCTURED_FLOWS`` below and nowhere else.

Both bill identically (same ledger recorders). ``writer`` / ``analytics_engine`` can be injected; when omitted they
are resolved from ``config`` (this is the worker's preflight).
"""
from __future__ import annotations

import asyncio

from app.services import ai_billing
from app.services.analytics import build_analytics_engine

from .analysis_execution import build_analysis_execution, resolve_analysis_concurrency
from .contracts import ReportDefinition
from .db_scope import isolated_db_scope
from .coordination import CoordinationRunner
from .coordinator import CoordinatorBillingLimitError
from .coordinator_factory import resolve_report_coordinator
from .composer import ReportComposer
from .html_renderer import HtmlReportRenderer
from .html_renderer_v2 import HtmlReportRendererV2
from .html_renderer_v3 import HtmlReportRendererV3
from .generator import ReportGenerator
from .pipeline import ReportPipeline
from .usage import record_coordinator_usage, record_extra_analysis_usage, record_section_usage, record_writer_usage
from .versions import (
    CURRENT_FINAL_REPORT_SCHEMA, FINAL_REPORT_SCHEMA_1_2, FINAL_REPORT_SCHEMA_1_3, FINAL_REPORT_SCHEMA_1_3_1,
)
from .fallback_report import FallbackReportBuilder
from .writer_factory import (
    resolve_report_writer, resolve_structured_report_writer, resolve_structured_report_writer_v13,
    resolve_structured_report_writer_v131,
)
from .writing import StructuredWriting


def build_report_pipeline(config: dict, definition: ReportDefinition, *, writer=None,
                          analytics_engine=None, render_html: bool = True) -> ReportPipeline:
    """Legacy pipeline (``FinalReport`` 1.1 / ``html-v1``)."""
    # Resolve the writer BEFORE spending analytical tokens: an unassigned or
    # unpriced report_writer role would otherwise waste a full analysis.
    writer = writer if writer is not None else resolve_report_writer(config, definition.report_id)
    return _assemble(config, definition, writer=writer, writing=None,
                     renderer=HtmlReportRenderer() if render_html else None, analytics_engine=analytics_engine)


class _StructuredFlow:
    """What a structured FinalReport schema needs: its writer resolver, whether its composer carries structured
    evidence (ComposedReportInput v2), and its renderer."""

    def __init__(self, resolve_writer, structured_evidence: bool, renderer, fallback=None):
        self.resolve_writer, self.structured_evidence, self.renderer = resolve_writer, structured_evidence, renderer
        # Builder of the deterministic report used when the writer fails its contract twice (None = no fallback).
        self.fallback = fallback


_STRUCTURED_FLOWS = {
    # Resolvers are looked up at call time (module globals), so they stay patchable in tests.
    FINAL_REPORT_SCHEMA_1_2: _StructuredFlow(
        lambda config, report_id: resolve_structured_report_writer(config, report_id), False, HtmlReportRendererV2),
    FINAL_REPORT_SCHEMA_1_3: _StructuredFlow(
        lambda config, report_id: resolve_structured_report_writer_v13(config, report_id), True, HtmlReportRendererV3),
    FINAL_REPORT_SCHEMA_1_3_1: _StructuredFlow(
        lambda config, report_id: resolve_structured_report_writer_v131(config, report_id), True,
        HtmlReportRendererV3, fallback=FallbackReportBuilder),
}


def build_structured_report_pipeline(config: dict, definition: ReportDefinition, *, writer=None,
                                     analytics_engine=None, render_html: bool = False,
                                     analysis_concurrency: int = 1,
                                     report_schema: str = CURRENT_FINAL_REPORT_SCHEMA) -> ReportPipeline:
    """Structured pipeline (``FinalReport`` ``report_schema``: 1.3 / ``html-v3`` by default, or 1.2 / ``html-v2``).
    The structure arrives frozen on ``definition``.

    ``analysis_concurrency`` is how many independent questions may run at once (1 = sequential).
    """
    try:
        flow = _STRUCTURED_FLOWS[report_schema]
    except KeyError:
        raise ValueError(f"No structured pipeline for FinalReport schema {report_schema!r}") from None
    writer = writer if writer is not None else flow.resolve_writer(config, definition.report_id)
    return _assemble(config, definition, writer=writer,
                     writing=StructuredWriting(
                         writer, ReportComposer(structured_evidence=flow.structured_evidence),
                         fallback=flow.fallback() if flow.fallback else None),
                     renderer=flow.renderer() if render_html else None, analytics_engine=analytics_engine,
                     analysis_concurrency=analysis_concurrency)


def _assemble(config: dict, definition: ReportDefinition, *, writer, writing, renderer,
              analytics_engine=None, analysis_concurrency: int = 1) -> ReportPipeline:
    analytics_engine = analytics_engine if analytics_engine is not None else build_analytics_engine(config)
    report_id = definition.report_id

    async def record_usage(section):
        await asyncio.to_thread(record_section_usage, report_id, section)

    async def record_writer(report_run_id, event):
        await asyncio.to_thread(record_writer_usage, report_id, report_run_id, event)

    async def record_coordinator(report_run_id, event):
        await asyncio.to_thread(record_coordinator_usage, report_id, report_run_id, event)

    async def record_extra(section):
        await asyncio.to_thread(record_extra_analysis_usage, report_id, section)

    def resolve_coordinator():
        # Lazy on purpose: only called by CoordinationRunner.run(), and only when
        # ``coordination_enabled`` is true, so a "fixed" run never touches the
        # report_coordinator role/config/pricing (V1.1 spec).
        try:
            return resolve_report_coordinator(config, report_id)
        except ai_billing.BillingLimitExceeded as exc:
            raise CoordinatorBillingLimitError(str(exc)) from exc

    coordination_runner = CoordinationRunner(
        resolve_coordinator, analytics_engine,
        record_coordinator_usage=record_coordinator, record_extra_usage=record_extra,
    )
    execution = build_analysis_execution(analysis_concurrency)
    generator = ReportGenerator(
        analytics_engine, record_usage=record_usage, execution=execution,
        # Concurrent questions each get their own DB session (the sequential path is left untouched).
        analysis_scope=isolated_db_scope if execution.concurrency > 1 else None,
    )
    return ReportPipeline(
        generator,
        writer, renderer, coordination_runner=coordination_runner,
        record_writer_usage=record_writer, writing=writing,
    )


def build_run_pipeline(config: dict, definition: ReportDefinition) -> ReportPipeline:
    """Pipeline for persisted runs: structured (CURRENT schema, 1.3); stops at the FinalReport, ``ReportRunExecutor`` renders and
    stores the HTML artifact itself through ``ArtifactService`` (default renderer of the report's schema).

    Only persisted runs use controlled concurrency (``REPORT_ANALYSIS_CONCURRENCY``, server config);
    the legacy ``/generate`` path stays sequential on purpose as the baseline."""
    return build_structured_report_pipeline(
        config, definition, render_html=False, analysis_concurrency=resolve_analysis_concurrency(config))
