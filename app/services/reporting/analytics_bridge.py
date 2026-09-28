"""Shared ``AnalyticsResult`` -> ``ReportSection`` mapping.

Both ``ReportGenerator`` (definition questions) and ``extra_analysis.run_analysis``
(coordinator-requested questions) go through the same ``AnalyticsExecutor`` and
must expose the same admin-facing metadata; this module is the single place that
knows how to turn an ``AnalyticsResult`` (or a raised exception) into a
``ReportSection`` so neither caller duplicates that logic.
"""
from __future__ import annotations

from typing import Any

from app.services.semantic_notes import normalize_semantic_notes

from .contracts import ReportSection


def skill_routing_summary(result, requested_keys: list[str]) -> dict[str, Any]:
    """Extract an admin-facing, non-sensitive summary of how skills were chosen."""
    route = result.route_metadata_json if isinstance(result.route_metadata_json, dict) else {}
    mode = route.get("skill_route_source") or ("pinned" if requested_keys else "dynamic")
    summary: dict[str, Any] = {
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


def actual_service_tier(result) -> str | None:
    return next((metadata.get("actual_service_tier")
                for event in reversed(result.ai_usage_events)
                if (metadata := event.get("metadata_json") or {}).get("component") == "main_agent"
                and metadata.get("actual_service_tier")), None)


def section_from_result(
    *, key: str, title: str, question: str, result, report_run_id: str,
    requested_skill_keys: list[str] = (),
    origin: str = "definition", purpose: str | None = None,
    related_section_keys: tuple[str, ...] = (),
) -> ReportSection:
    """Build a ``ReportSection`` from a successful ``AnalyticsExecutor.execute`` call."""
    return ReportSection(
        key=key, title=title, question=question,
        answer=result.answer, had_error=result.had_error,
        error_message=result.error_message, failure_reason=result.failure_reason,
        recovered_errors=list(result.recovered_errors), dax_query=result.dax_query,
        tools_called=list(result.tools_called), model_key=result.model_key,
        model=result.model, provider=result.provider,
        service_tier=result.service_tier,
        actual_service_tier=actual_service_tier(result),
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        latency_by_component_ms=dict(result.latency_by_component_ms),
        ai_usage_events=list(result.ai_usage_events), trace_id=result.trace_id,
        report_run_id=report_run_id,
        semantic_notes=normalize_semantic_notes(result.semantic_notes),
        skill_routing=skill_routing_summary(result, list(requested_skill_keys)),
        origin=origin, purpose=purpose, related_section_keys=tuple(related_section_keys),
    )


def section_from_exception(
    *, key: str, title: str, question: str, exc: Exception, report_run_id: str,
    origin: str = "definition", purpose: str | None = None,
    related_section_keys: tuple[str, ...] = (),
) -> ReportSection:
    """Build a ``had_error`` ``ReportSection`` from an unexpected (non-``AnalyticsError``) failure."""
    return ReportSection(
        key=key, title=title, question=question,
        answer="", had_error=True, error_message=str(exc), failure_reason="execution_failed",
        report_run_id=report_run_id, origin=origin, purpose=purpose,
        related_section_keys=tuple(related_section_keys),
    )
