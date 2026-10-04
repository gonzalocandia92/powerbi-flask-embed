"""JSON-safe, admin-facing views of reporting outcomes (shared by every HTTP surface)."""
from __future__ import annotations

from .contracts import ReportSection
from .writer import ReportWriterInvalidOutputError

PUBLIC_FAILURE_REASONS = frozenset({
    "dax_generation_failed", "dax_query_empty", "dax_execution_exception",
    "tool_round_limit", "unsupported_tool", "semantic_model_unavailable",
    "agent_execution_exception", "pinned_skill_unavailable", "pinned_skill_resolution_failed",
})


def public_failure_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    if reason in PUBLIC_FAILURE_REASONS:
        return reason
    if reason.endswith("_prompt_too_long"):
        return "provider_prompt_too_long"
    return "execution_failed"


def section_payload(section: ReportSection) -> dict:
    return {
        "key": section.key, "title": section.title, "origin": section.origin,
        "answer": "" if section.had_error else section.answer,
        "had_error": section.had_error,
        "failure_reason": (public_failure_reason(section.failure_reason) or "execution_failed")
                          if section.had_error else None,
        "recovered_error_count": len(section.recovered_errors),
        # Reason codes only: recovered error messages may carry DAX or client data.
        "recovered_errors": [
            {"reason": public_failure_reason(item.get("reason")) or "execution_failed"}
            for item in section.recovered_errors if isinstance(item, dict)
        ],
        # Administrative/debug metadata only; the public Markdown never includes it.
        "semantic_notes": [] if section.had_error else list(section.semantic_notes),
        "skill_routing": dict(section.skill_routing),
    }


def planned_sections(snapshot: dict | None) -> list[dict]:
    """Questions a run will execute, from its immutable snapshot, so a UI can show what is still pending."""
    questions = (snapshot or {}).get("questions") or []
    return [{"key": item["key"], "title": item["title"], "order": item["order"]}
            for item in sorted(questions, key=lambda item: item["order"])]


def writer_payload(result) -> dict:
    error = result.writer_error
    return {
        "status": "ok" if result.writer_ok else "failed",
        "error": None if error is None else {
            "code": error.code, "message": str(error),
            "validation_errors": list(error.errors) if isinstance(error, ReportWriterInvalidOutputError) else [],
        },
        "render_error": result.render_error,
        "attempts": list(result.writer_attempts),
        "usage_record_failures": result.usage_record_failures,
    }


def coordination_payload(coordination) -> dict | None:
    """Admin/debug view of one V1.1 coordination round; ``None`` in "fixed" mode.

    Never mixed into ``markdown``/``final_report``/``html``: the coordinator's
    decision, summary and per-analysis metadata are for the admin debug panel only.
    """
    if coordination is None:
        return None
    decision = coordination.decision
    return {
        "ran": coordination.ran,
        "enriched": coordination.enriched,
        "error": coordination.coordinator_error,
        "decision": None if decision is None else {
            "action": decision.action,
            "summary": decision.summary,
            "analyses": [{
                "question": item.question, "purpose": item.purpose,
                "expected_value": item.expected_value,
                "related_section_keys": list(item.related_section_keys),
            } for item in decision.analyses],
        },
        # Provider/model/tokens/latency/cost per LLM call (initial + repair).
        "attempts": list(coordination.attempts),
        "extra_analyses": [{
            "key": section.key, "title": section.title, "question": section.question,
            "purpose": section.purpose, "related_section_keys": list(section.related_section_keys),
            "had_error": section.had_error,
            "failure_reason": (public_failure_reason(section.failure_reason) or "execution_failed")
                              if section.had_error else None,
        } for section in coordination.extra_sections],
        "extra_usage_record_failures": coordination.extra_usage_record_failures,
    }
