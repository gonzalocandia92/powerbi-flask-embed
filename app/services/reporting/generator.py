"""Independent KLARA analyses for one report draft.

``ReportGenerator`` owns WHAT one question does (build the request, run the ``AnalyticsExecutor``,
contain its failures, record usage, report progress). HOW MANY questions run at once is not its
business: it delegates scheduling to an ``AnalysisExecutionStrategy`` (``analysis_execution``).
The draft always lists sections in the definition's order, never in completion order.
"""
from __future__ import annotations

import inspect
from contextlib import AbstractContextManager, nullcontext
from typing import Awaitable, Callable, Iterable

from app.services.analytics import AnalyticsError, AnalyticsExecutor, AnalyticsRequest

from .analysis_execution import AnalysisExecutionStrategy, AnalysisStats, SequentialAnalysisExecution
from .analytics_bridge import section_from_exception, section_from_result
from .contracts import ReportDefinition, ReportDraft, ReportQuestion, ReportSection
from .progress import ReportProgress, STAGE_ANALYSIS, notify_checkpoint, notify_section, notify_stage


UsageRecorder = Callable[[ReportSection], Awaitable[None] | None]
# Factory of a context manager entered around ONE question's analysis + usage recording
# (the factory uses it to give each concurrent question its own DB session; default: nothing).
AnalysisScope = Callable[[], AbstractContextManager]


class ReportGenerator:
    def __init__(self, analytics: AnalyticsExecutor, *, record_usage: UsageRecorder | None = None,
                 execution: AnalysisExecutionStrategy | None = None,
                 analysis_scope: AnalysisScope | None = None):
        self.analytics = analytics
        self.record_usage = record_usage
        self.execution = execution if execution is not None else SequentialAnalysisExecution()
        self.analysis_scope = analysis_scope
        # Operational metadata of the last ``generate`` (never part of the draft/report).
        self.last_analysis_stats: AnalysisStats | None = None

    async def generate(
        self, definition: ReportDefinition, *, report_run_id: str,
        progress: ReportProgress | None = None,
        completed_sections: Iterable[ReportSection] = (),
    ) -> ReportDraft:
        """Run every question of ``definition`` that is not already done.

        ``report_run_id`` is the identity of the whole generation and is owned by
        the caller (the persisted ``ReportRun``); later stages (writing, extra
        analyses, coordination) reuse it and only vary the stage/section key.
        ``completed_sections`` are already-persisted results that are kept as-is and
        not executed again (no repeated model calls / cost); only the pending questions
        reach the execution strategy.

        This is a barrier: it returns once EVERY initial question has finished (successfully
        or not), with ``sections`` in definition order whatever the completion order was.
        """
        await notify_stage(progress, STAGE_ANALYSIS)
        done = {section.key: section for section in completed_sections}
        draft = ReportDraft(
            report_run_id=report_run_id,
            report_id=definition.report_id,
            name=definition.name,
            analysis_model_key=definition.analysis_model_key,
            analysis_service_tier=definition.analysis_service_tier,
        )
        ordered = sorted(definition.questions, key=lambda item: item.order)
        pending = [question for question in ordered if question.key not in done]
        stats = self.last_analysis_stats = AnalysisStats()

        async def run_one(question: ReportQuestion) -> ReportSection:
            with self._scope():
                section = await self._analyse(definition, question, report_run_id)
                usage_error = await self._record_usage(section)
            # Progress persistence runs in the run's own context, outside the per-question scope.
            notify_error = None
            try:
                await notify_section(progress, section)
            except Exception as exc:  # noqa: BLE001 - cancellation / lost lease; usage is already recorded
                notify_error = exc
            if notify_error is not None or usage_error is not None:
                raise notify_error or usage_error
            return section

        fresh = await self.execution.execute(
            pending, run_one, checkpoint=lambda: notify_checkpoint(progress), stats=stats,
        ) if pending else []
        finished = {**done, **{question.key: section for question, section in zip(pending, fresh)}}
        draft.sections = [finished[question.key] for question in ordered]
        return draft

    def _scope(self) -> AbstractContextManager:
        return self.analysis_scope() if self.analysis_scope is not None else nullcontext()

    async def _analyse(self, definition: ReportDefinition, question: ReportQuestion,
                       report_run_id: str) -> ReportSection:
        # A fresh request per question: no history, no shared mutable state between questions.
        request = AnalyticsRequest(
            report_id=definition.report_id,
            question=question.question,
            history=[],
            source="report",
            execution_id=f"report:{report_run_id}:{question.key}",
            model_key=definition.analysis_model_key,
            service_tier=definition.analysis_service_tier,
            required_skill_keys=list(question.required_skill_keys),
            trace_context={
                "report_run_id": report_run_id,
                "report_stage": "analysis",
                "report_section_key": question.key,
            },
        )
        try:
            result = await self.analytics.execute(request)
        except AnalyticsError:
            # Preflight, credentials, billing and model configuration are global.
            raise
        except Exception as exc:
            return section_from_exception(
                key=question.key, title=question.title, question=question.question,
                exc=exc, report_run_id=report_run_id,
            )
        return section_from_result(
            key=question.key, title=question.title, question=question.question,
            result=result, report_run_id=report_run_id,
            requested_skill_keys=list(question.required_skill_keys),
        )

    async def _record_usage(self, section: ReportSection) -> Exception | None:
        """Ledger write for the section; a failure is returned (raised later) so it never loses the section."""
        if not (self.record_usage and section.ai_usage_events):
            return None
        try:
            recorded = self.record_usage(section)
            if inspect.isawaitable(recorded):
                await recorded
        except Exception as exc:  # noqa: BLE001
            return exc
        return None
