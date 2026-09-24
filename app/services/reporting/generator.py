"""Sequential, independent KLARA analyses for one report draft."""
from __future__ import annotations

import inspect
from typing import Awaitable, Callable

from app.services.analytics import AnalyticsError, AnalyticsExecutor, AnalyticsRequest

from .contracts import ReportDefinition, ReportDraft, ReportSection


UsageRecorder = Callable[[ReportSection], Awaitable[None] | None]


class ReportGenerator:
    def __init__(self, analytics: AnalyticsExecutor, *, record_usage: UsageRecorder | None = None):
        self.analytics = analytics
        self.record_usage = record_usage

    async def generate(self, definition: ReportDefinition) -> ReportDraft:
        draft = ReportDraft(
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
                execution_id=f"report:{definition.report_id}:{question.key}",
                model_key=definition.analysis_model_key,
                service_tier=definition.analysis_service_tier,
                trace_context={"report_stage": "analysis", "report_section_key": question.key},
            )
            try:
                result = await self.analytics.execute(request)
            except AnalyticsError:
                # Preflight, credentials, billing and model configuration are global.
                raise
            except Exception as exc:
                section = ReportSection(
                    key=question.key, title=question.title, question=question.question,
                    answer="", had_error=True, error_message=str(exc),
                    failure_reason="execution_failed",
                )
            else:
                actual_tier = next((metadata.get("actual_service_tier")
                                    for event in reversed(result.ai_usage_events)
                                    if (metadata := event.get("metadata_json") or {}).get("component") == "main_agent"
                                    and metadata.get("actual_service_tier")), None)
                section = ReportSection(
                    key=question.key, title=question.title, question=question.question,
                    answer=result.answer, had_error=result.had_error,
                    error_message=result.error_message, failure_reason=result.failure_reason,
                    recovered_errors=list(result.recovered_errors), dax_query=result.dax_query,
                    tools_called=list(result.tools_called), model_key=result.model_key,
                    model=result.model, provider=result.provider,
                    service_tier=result.service_tier,
                    actual_service_tier=actual_tier,
                    input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                    latency_by_component_ms=dict(result.latency_by_component_ms),
                    ai_usage_events=list(result.ai_usage_events), trace_id=result.trace_id,
                )
            draft.sections.append(section)
            if self.record_usage and section.ai_usage_events:
                recorded = self.record_usage(section)
                if inspect.isawaitable(recorded):
                    await recorded
        return draft
