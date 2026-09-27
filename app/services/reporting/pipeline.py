"""ReportPipeline: ReportDefinition -> ReportDraft -> FinalReport -> HTML.

Composition only. ``ReportGenerator`` still owns analysis, the writer owns
redaction, the renderer owns presentation; each is replaceable. A writer failure
never invalidates the analytical draft: the result always carries it.
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from .contracts import ReportDefinition, ReportDraft
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

    @property
    def writer_ok(self) -> bool:
        return self.final_report is not None


class ReportPipeline:
    def __init__(self, generator: ReportGenerator, writer: ReportWriter, renderer: FinalReportRenderer,
                 *, record_writer_usage: WriterUsageRecorder | None = None):
        self.generator = generator
        self.writer = writer
        self.renderer = renderer
        self.record_writer_usage = record_writer_usage

    async def run(self, definition: ReportDefinition) -> ReportPipelineResult:
        # Analytical errors (billing, configuration, ...) propagate exactly as before.
        draft = await self.generator.generate(definition)
        return await self.write(draft)

    async def write(self, draft: ReportDraft) -> ReportPipelineResult:
        """Writing + rendering for an existing draft (also the entry point of a future coordinator)."""
        result = ReportPipelineResult(draft=draft)

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
