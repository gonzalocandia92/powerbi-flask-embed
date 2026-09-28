"""Structured analytical draft; a future writer can consume it without Power BI."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ReportQuestion:
    key: str
    title: str
    question: str
    order: int = 0
    # Skills fixed by the report definition (not suggestions). Empty = automatic
    # routing; otherwise the dynamic selector is skipped for this question.
    required_skill_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        keys = self.required_skill_keys
        if isinstance(keys, str) or not isinstance(keys, (list, tuple)):
            raise ValueError("required_skill_keys must be a list or tuple of skill keys")
        if any(not isinstance(key, str) or not key.strip() for key in keys):
            raise ValueError("required_skill_keys cannot contain empty or non-string values")
        unique: list[str] = []
        for key in (key.strip() for key in keys):
            if key.casefold() not in {item.casefold() for item in unique}:
                unique.append(key)
        object.__setattr__(self, "required_skill_keys", tuple(unique))


@dataclass
class ReportDefinition:
    report_id: int
    name: str
    questions: list[ReportQuestion]
    analysis_model_key: str | None = None
    analysis_service_tier: str | None = None
    # V1.1: "fixed" keeps the exact V1 behaviour (ReportDraft -> ReportWriter,
    # coordinator never instantiated, its config/pricing can never block a run).
    # "coordinated" inserts one bounded ReportCoordinator round between the
    # initial ReportDraft and ReportWriter (see ``coordination.CoordinationRunner``).
    coordination_enabled: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.report_id, bool) or not isinstance(self.report_id, int) or self.report_id <= 0:
            raise ValueError("report_id must be a positive Report.id")
        if not self.name.strip() or not self.questions:
            raise ValueError("name and at least one question are required")
        keys = set()
        for item in self.questions:
            if not item.key.strip() or not item.title.strip() or not item.question.strip():
                raise ValueError("question key, title and text cannot be empty")
            if item.key in keys:
                raise ValueError(f"duplicate question key: {item.key}")
            keys.add(item.key)


@dataclass
class ReportSection:
    key: str
    title: str
    question: str
    answer: str
    had_error: bool = False
    error_message: str | None = None
    failure_reason: str | None = None
    recovered_errors: list[dict[str, Any]] = field(default_factory=list)
    dax_query: str | None = None
    tools_called: list[dict[str, Any]] = field(default_factory=list)
    model_key: str | None = None
    model: str | None = None
    provider: str | None = None
    service_tier: str | None = None
    actual_service_tier: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_by_component_ms: dict[str, Any] = field(default_factory=dict)
    ai_usage_events: list[dict[str, Any]] = field(default_factory=list)
    trace_id: str | None = None
    # Identity of the whole generation this section belongs to (see ReportDraft).
    report_run_id: str | None = None
    # Curated, internal interpretation notes from the skills used by this section.
    # Never rendered into the public Markdown; future writers may consume them.
    semantic_notes: list[str] = field(default_factory=list)
    # How skills were chosen: {"mode": "pinned"|"dynamic", "selector": ..., "skills": [...],
    # "pinned_skill_keys": [...], "unavailable": [{"skill_key", "reason"}]}. Admin/debug only.
    skill_routing: dict[str, Any] = field(default_factory=dict)
    # V1.1: provenance. "definition" sections come straight from ReportDefinition
    # questions; "coordinator" sections were requested by ReportCoordinator and
    # executed through ``extra_analysis.run_analysis`` (same AnalyticsExecutor).
    origin: Literal["definition", "coordinator"] = "definition"
    # Why the coordinator asked for this analysis (RequestedAnalysis.purpose).
    # Always ``None`` for origin="definition".
    purpose: str | None = None
    # Which original section(s) motivated this coordinator-requested analysis.
    related_section_keys: tuple[str, ...] = ()


@dataclass
class ReportDraft:
    """Portable analytical artifact: identity, configuration and section results.

    It carries no credentials and is the intended input of a future writer.
    """

    report_run_id: str
    report_id: int
    name: str
    analysis_model_key: str | None
    analysis_service_tier: str | None
    sections: list[ReportSection] = field(default_factory=list)
