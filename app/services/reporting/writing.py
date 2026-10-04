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
from .writer import ReportWriter, ReportWriterExecutionError, WriterAttemptHook


@dataclass
class WritingOutcome:
    report: BaseModel                       # FinalReport (1.1) or FinalReportV12; carries its own schema_version
    composed: ComposedReportInput | None = None   # None in the legacy flow


class WritingStrategy(Protocol):
    async def write(self, draft: ReportDraft, structure: FrozenStructure | None, *,
                    on_attempt: WriterAttemptHook | None = None) -> WritingOutcome: ...


class LegacyWriting:
    def __init__(self, writer: ReportWriter):
        self.writer = writer

    async def write(self, draft, structure=None, *, on_attempt=None) -> WritingOutcome:
        return WritingOutcome(report=await self.writer.write(draft, on_attempt=on_attempt))


class StructuredWriting:
    def __init__(self, writer: StructuredReportWriter, composer: ReportComposer | None = None):
        self.writer = writer
        self.composer = composer or ReportComposer()

    async def write(self, draft, structure=None, *, on_attempt=None) -> WritingOutcome:
        try:
            composed = self.composer.compose(draft, structure)
        except CompositionError as exc:
            # Never "fixed" by a model; surfaces as a writer failure so the persisted analyses are kept.
            raise ReportWriterExecutionError(str(exc), code=exc.code) from exc
        report = await self.writer.write(composed, report_run_id=draft.report_run_id, on_attempt=on_attempt)
        return WritingOutcome(report=report, composed=composed)
