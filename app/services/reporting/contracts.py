"""Structured analytical draft; a future writer can consume it without Power BI."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReportQuestion:
    key: str
    title: str
    question: str
    order: int = 0


@dataclass
class ReportDefinition:
    report_id: int
    name: str
    questions: list[ReportQuestion]
    analysis_model_key: str | None = None
    analysis_service_tier: str | None = None

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


@dataclass
class ReportDraft:
    report_id: int
    name: str
    analysis_model_key: str | None
    analysis_service_tier: str | None
    sections: list[ReportSection] = field(default_factory=list)
