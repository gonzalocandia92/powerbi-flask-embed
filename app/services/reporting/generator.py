"""Sequential, independent KLARA analyses for one report draft."""
from __future__ import annotations

import inspect
import uuid
from typing import Awaitable, Callable

from app.services.analytics import AnalyticsError, AnalyticsExecutor, AnalyticsRequest

from .analytics_bridge import section_from_exception, section_from_result
from .contracts import ReportDefinition, ReportDraft, ReportSection


UsageRecorder = Callable[[ReportSection], Awaitable[None] | None]


class ReportGenerator:
    def __init__(self, analytics: AnalyticsExecutor, *, record_usage: UsageRecorder | None = None):
        self.analytics = analytics
        self.record_usage = record_usage

    async def generate(self, definition: ReportDefinition) -> ReportDraft:
        # One identity per complete generation; later stages (writing, extra
        # analyses, coordination) reuse it and only vary the stage/section key.
        report_run_id = uuid.uuid4().hex
        draft = ReportDraft(
            report_run_id=report_run_id,
            report_id=definition.report_id,
            name=definition.name,
            analysis_model_key=definition.analysis_model_key,
            analysis_service_tier=definition.analysis_service_tier,
        )
        for question in sorted(definition.questions, key=lambda item: item.order):
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
                section = section_from_exception(
                    key=question.key, title=question.title, question=question.question,
                    exc=exc, report_run_id=report_run_id,
                )
            else:
                section = section_from_result(
                    key=question.key, title=question.title, question=question.question,
                    result=result, report_run_id=report_run_id,
                    requested_skill_keys=list(question.required_skill_keys),
                )
            draft.sections.append(section)
            if self.record_usage and section.ai_usage_events:
                recorded = self.record_usage(section)
                if inspect.isawaitable(recorded):
                    await recorded
        return draft
