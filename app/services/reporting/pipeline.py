"""ReportPipeline: ReportDefinition -> ReportDraft -> [coordination] -> FinalReport -> HTML.

Composition only. ``ReportGenerator`` still owns analysis, an optional
``CoordinationRunner`` owns the bounded coordinator round (see
``coordination.py``), the writer owns redaction, the renderer owns
presentación; each is replaceable. A writer failure never invalidates the
analytical draft, and neither does a coordinator failure: both are contained
and the result always carries the draft that was actually written.

``strategy`` lives on ``ReportDefinition`` (``coordination_enabled``); this
pipeline only ever calls the coordinator when both that flag is set AND a
``coordination_runner`` was supplied. A "fixed" run (the V1 default) never even
constructs a coordinator, so its configuration/pricing can never block it.
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from .contracts import ReportDefinition, ReportDraft
from .coordination import CoordinationOutcome, CoordinationRunner
from .final_report import FinalReport
from .generator import ReportGenerator
from .writer import ReportWriter, ReportWriterError, WriterAttempt

LOG = logging.getLogger(__name__)

WriterUsageRecorder = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class FinalReportRenderer(Protocol):
    def render(self, report: FinalReport) -> str: ...


@dataclass
class ReportPipelineResult:
    draft: ReportDraft
    final_report: FinalReport | None = None
    html: str | None = None
    writer_error: ReportWriterError | None = None
    render_error: str | None = None
    # JSON-safe per-attempt summaries (provider, model, tokens, latency, cost, repair).
    writer_attempts: list[dict[str, Any]] = field(default_factory=list)
    # Attempts whose ledger event could not be persisted (kept visible for admins).
    usage_record_failures: int = 0
    # None in "fixed" mode (or when no CoordinationRunner was supplied); otherwise
    # the full coordination round, for the admin debug panel.
    coordination: CoordinationOutcome | None = None

    @property
    def writer_ok(self) -> bool:
        return self.final_report is not None


class ReportPipeline:
    def __init__(self, generator: ReportGenerator, writer: ReportWriter, renderer: FinalReportRenderer,
                 *, coordination_runner: CoordinationRunner | None = None,
                 record_writer_usage: WriterUsageRecorder | None = None):
        self.generator = generator
        self.writer = writer
        self.renderer = renderer
        self.coordination_runner = coordination_runner
        self.record_writer_usage = record_writer_usage

    async def run(self, definition: ReportDefinition) -> ReportPipelineResult:
        # Analytical errors (billing, configuración, ...) propagate exactly as before.
        draft = await self.generator.generate(definition)
        return await self.write(draft, definition)

    async def write(self, draft: ReportDraft, definition: ReportDefinition | None = None) -> ReportPipelineResult:
        """Coordination (if configured) + writing + rendering for an existing draft."""
        result = ReportPipelineResult(draft=draft)

        if self.coordination_runner is not None and definition is not None and definition.coordination_enabled:
            result.coordination = await self.coordination_runner.run(draft, definition)
            draft = result.coordination.draft
            result.draft = draft

        async def on_attempt(attempt: WriterAttempt) -> None:
            result.writer_attempts.append(attempt.summary())
            if self.record_writer_usage is None:
                return
            try:
                recorded = self.record_writer_usage(draft.report_run_id, attempt.usage_event)
                if inspect.isawaitable(recorded):
                    await recorded
            except Exception:
                # Never lose the draft/report over a ledger failure; surface it instead.
                result.usage_record_failures += 1
                LOG.exception("Report writer usage could not be recorded run=%s", draft.report_run_id)

        try:
            result.final_report = await self.writer.write(draft, on_attempt=on_attempt)
        except ReportWriterError as exc:
            result.writer_error = exc
            return result
        try:
            result.html = self.renderer.render(result.final_report)
        except Exception:
            LOG.exception("Report HTML rendering failed run=%s", draft.report_run_id)
            result.render_error = "report_render_failed"
        return result
