"""Writing strategies: the single seam between ``ReportPipeline`` and the two ways of producing a report.

    LegacyWriting      ReportDraft                      -> writer          -> FinalReport 1.1   (``/generate``)
    StructuredWriting  ReportDraft + FrozenStructure    -> ReportComposer  -> ComposedReportInput
                                                        -> structured writer -> FinalReport 1.2 (persisted runs)

The pipeline calls ``strategy.write(draft, structure, on_attempt=...)`` and never branches on a version; which
strategy a pipeline has is decided once, by ``pipeline_factory``. Composition is deterministic and costs no
tokens (no usage event); only the writer calls the model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from .composed_report import ComposedReportInput
from .composer import CompositionError, ReportComposer
from .contracts import ReportDraft
from .structure_contracts import FrozenStructure
from .structured_writer import StructuredReportWriter
from .fallback_report import FallbackReportBuilder, FallbackUnavailableError
from .writer import (
    ReportWriter, ReportWriterError, ReportWriterExecutionError, ReportWriterInvalidOutputError, WriterAttemptHook,
)

MODE_WRITER = "writer"
MODE_FALLBACK = "fallback"


@dataclass
class WritingOutcome:
    report: BaseModel                       # FinalReport (1.1) or FinalReportV12; carries its own schema_version
    composed: ComposedReportInput | None = None   # None in the legacy flow
    mode: str = MODE_WRITER                        # "writer" | "fallback" (a repaired writer output is still "writer")
    # Set only with mode == "fallback": the editorial failure the deterministic fallback covered.
    writer_error: ReportWriterError | None = None


class WritingStrategy(Protocol):
    async def write(self, draft: ReportDraft, structure: FrozenStructure | None, *,
                    on_attempt: WriterAttemptHook | None = None) -> WritingOutcome: ...


class LegacyWriting:
    def __init__(self, writer: ReportWriter):
        self.writer = writer

    async def write(self, draft, structure=None, *, on_attempt=None) -> WritingOutcome:
        return WritingOutcome(report=await self.writer.write(draft, on_attempt=on_attempt))


class StructuredWriting:
    def __init__(self, writer: StructuredReportWriter, composer: ReportComposer | None = None,
                 fallback: FallbackReportBuilder | None = None):
        self.writer = writer
        self.composer = composer or ReportComposer()
        # Deterministic, model-free safety net for an INVALID editorial output only (see ``write``).
        self.fallback = fallback

    async def write(self, draft, structure=None, *, on_attempt=None) -> WritingOutcome:
        try:
            composed = self.composer.compose(draft, structure)
        except CompositionError as exc:
            # Never "fixed" by a model; surfaces as a writer failure so the persisted analyses are kept.
            raise ReportWriterExecutionError(str(exc), code=exc.code) from exc
        try:
            report = await self.writer.write(composed, report_run_id=draft.report_run_id, on_attempt=on_attempt)
        except ReportWriterInvalidOutputError as failure:
            # The writer (and its one repair) could not satisfy the contract. Provider/config failures, a failed
            # composition or a report with no successful evidence are NOT editorial failures and never get here.
            if self.fallback is None:
                raise
            try:
                report = self.fallback.build(composed)
            except FallbackUnavailableError:
                raise failure from None
            return WritingOutcome(report=report, composed=composed, mode=MODE_FALLBACK, writer_error=failure)
        return WritingOutcome(report=report, composed=composed)
