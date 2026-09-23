"""Stable, consumer-agnostic contracts for analytical execution."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from app.services.llm import CachePolicy


class AnalyticsError(Exception):
    """Base exception for the internal analytics API."""


class AnalyticsReportNotFoundError(AnalyticsError):
    """Raised when an internal ``Report.id`` cannot be resolved."""


class AnalyticsBillingLimitExceededError(AnalyticsError):
    """Raised when billing preflight rejects an execution."""


class AnalyticsModelError(AnalyticsError):
    """Raised when the requested execution model cannot be resolved."""


class AnalyticsConfigurationError(AnalyticsError):
    """Raised when the execution request or runtime configuration is invalid."""


@dataclass
class AnalyticsRequest:
    report_id: int
    question: str
    history: list[dict[str, Any]] = field(default_factory=list)
    source: str = "internal"
    execution_id: str | None = None
    model_key: str | None = None
    cache_policy: CachePolicy | None = None
    trace_context: dict[str, Any] = field(default_factory=dict)
    # Advanced override for consumers that have explicitly resolved all roles.
    role_configuration: Any | None = None


@dataclass
class AnalyticsResult:
    answer: str
    report_id: int
    tool_rounds: int = 0
    tools_called: list[dict[str, Any]] = field(default_factory=list)
    dax_query: str | None = None
    model_key: str | None = None
    model: str | None = None
    provider: str | None = None
    gateway: str | None = None
    service_tier: str | None = None
    actual_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    ai_usage_events: list[dict[str, Any]] = field(default_factory=list)
    had_error: bool = False
    error_message: str | None = None
    failure_reason: str | None = None
    route_metadata_json: dict[str, Any] | None = None
    route_validation_warnings: list[str] = field(default_factory=list)
    latency_by_component_ms: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    execution_metadata: dict[str, Any] = field(default_factory=dict)
    # Existing evaluation persistence consumes these two agent diagnostics.
    model_metadata: dict[str, Any] = field(default_factory=dict)
    complexity_assessment: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a compatibility payload for existing persistence adapters."""
        return asdict(self)


class AnalyticsExecutor(Protocol):
    async def execute(self, request: AnalyticsRequest) -> AnalyticsResult:
        ...
