from __future__ import annotations
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelCapabilities:
    supports_tools: bool = True
    supports_reasoning: bool = False
    supports_cache_key: bool = False
    supports_flex: bool = False
    context_window: int = 200_000


@dataclass(frozen=True)
class ModelConfig:
    model_key: str
    physical_model: str
    provider: str = "anthropic"
    gateway: str = "direct"
    max_output_tokens: int = 4096
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    service_tier: str | None = None
    pricing_tier: str | None = None
    reasoning_effort: str | None = None
    provider_options: dict[str, Any] = field(default_factory=dict)
    api_key: str | None = field(default=None, repr=False, compare=False)
    family_key: str | None = None
    family_options: dict[str, Any] = field(default_factory=dict)
    thinking_mode: str | None = None

    @property
    def transport_model(self) -> str:
        prefix = "openrouter" if self.gateway == "openrouter" else self.provider
        return f"{prefix}/{self.physical_model}"

    def metadata(self) -> dict[str, Any]:
        return {"model_key": self.model_key, "provider": self.provider,
                "physical_model": self.physical_model, "gateway": self.gateway,
                "service_tier": self.service_tier, "pricing_tier": self.pricing_tier,
                "family_key": self.family_key, "requested_thinking_mode": self.thinking_mode,
                "max_output_tokens": self.max_output_tokens}


@dataclass(frozen=True)
class CacheScope:
    kind: str = "report"
    key: str | None = None


@dataclass(frozen=True)
class CachePolicy:
    enabled: bool = True
    scope: CacheScope = field(default_factory=CacheScope)


def report_cache_scope(empresa_id: int | None, report_id: int | None) -> CacheScope:
    """Stable, provider-safe cache group for a single company's report."""
    if empresa_id is None or report_id is None:
        return CacheScope()
    return CacheScope(key=f"empresa{int(empresa_id)}_reporte{int(report_id)}")


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str


@dataclass(frozen=True, repr=False)
class ProviderState:
    """Ephemeral continuation only; never persist or include in telemetry."""
    owner: str
    model: str
    payload: Any = field(repr=False)

    def __repr__(self) -> str:
        return "<ProviderState redacted>"


@dataclass
class LLMMessage:
    role: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    provider_state: ProviderState | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.text}


@dataclass(frozen=True)
class LLMUsage:
    input_total_tokens: int = 0
    input_uncached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False

    def ledger_fields(self) -> dict[str, int]:
        return {"input_tokens": self.input_uncached_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens, "cache_write_tokens": self.cache_write_tokens}

    def metadata(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LLMRequest:
    model: ModelConfig
    instructions: list[dict[str, Any]]
    messages: list[LLMMessage]
    tools: list[ToolDefinition] = field(default_factory=list)
    tool_choice: str | None = None
    temperature: float | None = None
    cache: CachePolicy = field(default_factory=CachePolicy)
    operation: str = "chat-response"
    thinking_mode_override: str | None = None
    thinking_override_reason: str | None = None


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    finish_reason: str | None = None
    provider: str = ""
    model: str = ""
    provider_state: ProviderState | None = field(default=None, repr=False)
    actual_service_tier: str | None = None
    provider_created_at: int | None = None
    thinking_decision: dict[str, Any] = field(default_factory=dict)
    pricing_quote: dict[str, Any] = field(default_factory=dict)

    def message(self) -> LLMMessage:
        return LLMMessage("assistant", self.text, self.tool_calls, provider_state=self.provider_state)


class LLMError(RuntimeError):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        provider_error_code: str | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.provider = provider
        self.status_code = status_code
        self.provider_error_code = provider_error_code


def provider_error_type(provider: str | None, kind: str) -> str:
    """Return a stable, provider-scoped error label for public telemetry."""
    provider_slug = re.sub(r"[^a-z0-9]+", "_", str(provider or "unknown").lower()).strip("_") or "unknown"
    if kind == "context_too_large":
        return f"{provider_slug}_prompt_too_long"
    if kind == "missing_api_key":
        return f"missing_{provider_slug}_api_key"
    return f"{provider_slug}_{kind}"


def safe_error_metadata(
    exc: Exception,
    *,
    provider: str | None = None,
    recoverable: bool | None = None,
    failure_scope: str | None = None,
) -> dict[str, Any]:
    """Expose only allow-listed error fields; never provider messages or payloads."""
    kind = getattr(exc, "kind", None) or "provider_error"
    resolved_provider = getattr(exc, "provider", None) or provider
    metadata: dict[str, Any] = {
        "error_type": provider_error_type(resolved_provider, kind),
    }
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        metadata["provider_http_status"] = status_code
    error_code = getattr(exc, "provider_error_code", None)
    if isinstance(error_code, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", error_code):
        metadata["provider_error_code"] = error_code
    if recoverable is not None:
        metadata["recoverable"] = recoverable
    if failure_scope:
        metadata["failure_scope"] = failure_scope
    return metadata


class LLMRuntime(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse: ...
    async def count_tokens(self, request: LLMRequest) -> int: ...
