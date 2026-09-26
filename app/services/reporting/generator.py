"""Sequential, independent KLARA analyses for one report draft."""
from __future__ import annotations

import inspect
import uuid
from typing import Awaitable, Callable

from app.services.analytics import AnalyticsError, AnalyticsExecutor, AnalyticsRequest
from app.services.semantic_notes import normalize_semantic_notes

from .contracts import ReportDefinition, ReportDraft, ReportSection


UsageRecorder = Callable[[ReportSection], Awaitable[None] | None]


def _skill_routing_summary(result, requested_keys) -> dict:
    """Extract an admin-facing, non-sensitive summary of how skills were chosen."""
    route = result.route_metadata_json if isinstance(result.route_metadata_json, dict) else {}
    mode = route.get("skill_route_source") or ("pinned" if requested_keys else "dynamic")
    summary = {
        "mode": mode,
        "skills": [key for key in route.get("resolved_skill_keys") or [] if isinstance(key, str)],
        "pinned_skill_keys": [key for key in route.get("pinned_skill_keys") or [] if isinstance(key, str)],
    }
    if mode == "dynamic":
        strategy = (result.execution_metadata or {}).get("skill_selector_strategy")
        summary["selector"] = strategy if isinstance(strategy, str) else None
    unavailable = [
        {"skill_key": str(item.get("skill_key")), "reason": str(item.get("reason"))}
        for item in route.get("unavailable_pinned_skills") or [] if isinstance(item, dict)
    ]
    if unavailable:
        summary["unavailable"] = unavailable
    return summary


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
                section = ReportSection(
                    key=question.key, title=question.title, question=question.question,
                    answer="", had_error=True, error_message=str(exc),
                    failure_reason="execution_failed", report_run_id=report_run_id,
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
                    report_run_id=report_run_id,
                    semantic_notes=normalize_semantic_notes(result.semantic_notes),
                    skill_routing=_skill_routing_summary(result, question.required_skill_keys),
                )
            draft.sections.append(section)
            if self.record_usage and section.ai_usage_events:
                recorded = self.record_usage(section)
                if inspect.isawaitable(recorded):
                    await recorded
        return draft
