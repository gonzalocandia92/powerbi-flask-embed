"""Versioned, provider-specific LLM behaviour owned by KLARA.

Profiles describe *wire* controls.  A capability flag is never treated as an
instruction to turn reasoning on or off.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any


CONTROLLED_OPTIONS = {"thinking", "reasoning", "reasoning_effort", "service_tier",
                      "output_config", "max_tokens", "model", "api_key", "tools", "tool_choice",
                      "prompt_cache_key", "prompt_cache_options", "user_id"}


@dataclass(frozen=True)
class ThinkingDecision:
    requested: str
    effective: str
    level: str | None = None
    override_reason: str | None = None

    def metadata(self) -> dict[str, str | None]:
        return {"requested_thinking_mode": self.requested,
                "effective_thinking_mode": self.effective,
                "thinking_level": self.level if self.effective == "on" else None,
                "thinking_override_reason": self.override_reason}


class ModelFamilyProfile:
    key = ""
    label = ""
    provider = ""
    models: tuple[str, ...] = ()
    levels: tuple[str, ...] = ()
    supports_thinking = False
    supports_flex = False
    supports_report_cache_scope = False
    family_fields: tuple[str, ...] = ()

    def supports(self, provider: str, physical_model: str, gateway: str) -> bool:
        return provider == self.provider and physical_model in self.models and gateway == "direct"

    def validate(self, model) -> None:
        if not self.supports(model.provider, model.physical_model, model.gateway):
            raise ValueError(f"{model.physical_model} is not supported by {self.key}")
        mode = model.thinking_mode
        if mode not in {"on", "off"}:
            raise ValueError("thinking_mode must explicitly be on or off")
        if mode == "on" and not self.supports_thinking:
            raise ValueError(f"{self.key} does not support thinking")
        if mode == "on" and self.levels and model.reasoning_effort not in self.levels:
            raise ValueError(f"reasoning_effort must be one of {', '.join(self.levels)}")
        if mode == "off" and model.reasoning_effort:
            raise ValueError("reasoning_effort requires thinking_mode=on")
        if model.service_tier == "flex" and not self.supports_flex:
            raise ValueError(f"{self.key} does not support flex")
        if model.service_tier not in (None, "flex"):
            raise ValueError(f"service_tier {model.service_tier} is not supported by {self.key}")
        unknown_fields = set(model.family_options or {}) - set(self.family_fields)
        if unknown_fields:
            raise ValueError(f"Unsupported family options for {self.key}: {', '.join(sorted(unknown_fields))}")
        if "cache_by_report" in (model.family_options or {}):
            if not self.supports_report_cache_scope or type(model.family_options["cache_by_report"]) is not bool:
                raise ValueError(f"cache_by_report is not valid for {self.key}")
            if model.family_options["cache_by_report"] and model.gateway != "direct":
                raise ValueError("Report cache grouping requires a direct provider gateway")
        def check_options(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if str(key).lower() in CONTROLLED_OPTIONS:
                        raise ValueError(f"provider option {key} is owned by the family profile")
                    check_options(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    check_options(child)
        check_options(model.provider_options)

    def apply(self, payload: dict[str, Any], request) -> ThinkingDecision:
        mode = request.model.thinking_mode
        if request.thinking_mode_override not in (None, "off"):
            raise ValueError("Only an explicit off thinking override is supported")
        if request.thinking_mode_override == "off":
            # Validate the controls that will actually be sent. A role may keep
            # its normal on/budget configuration while this call requires off.
            off_model = replace(request.model, thinking_mode="off", reasoning_effort=None,
                                family_options={key: value for key, value in request.model.family_options.items()
                                                if key != "budget_tokens"})
            self.validate(off_model)
            return ThinkingDecision(mode, "off", override_reason=request.thinking_override_reason or "request_override")
        self.validate(request.model)
        return ThinkingDecision(mode, mode, request.model.reasoning_effort)

    def apply_cache(self, payload: dict[str, Any], request) -> None:
        if not (self.supports_report_cache_scope and request.model.family_options.get("cache_by_report")
                and request.cache.enabled and request.cache.scope.kind == "report"):
            return
        key = request.cache.scope.key
        if key is None:
            return  # Workflows without a complete report scope use provider defaults.
        if not re.fullmatch(r"empresa[0-9]+_reporte[0-9]+", key):
            raise ValueError("Invalid report cache scope")
        self.set_cache_group(payload, key)

    def set_cache_group(self, payload: dict[str, Any], key: str) -> None:
        raise NotImplementedError

    def pricing_band(self, at, usage) -> str | None:
        return None

    def billing_model(self, physical_model: str) -> str:
        return physical_model

    def context_band(self, usage) -> str | None:
        return None

    def normalize_usage(self, raw_usage: dict[str, Any], normalized):
        """Family hook for provider usage fields after the common LiteLLM shape."""
        return normalized


class ClaudeHaiku45Profile(ModelFamilyProfile):
    key, label, provider = "claude-haiku-4.5", "Claude Haiku 4.5", "anthropic"
    models = ("claude-haiku-4-5-20251001", "claude-haiku-4-5")
    supports_thinking = True
    family_fields = ('budget_tokens',)

    def billing_model(self, physical_model: str) -> str:
        return 'claude-haiku-4-5-20251001' if physical_model == 'claude-haiku-4-5' else physical_model

    def validate(self, model) -> None:
        super().validate(model)
        budget = model.family_options.get("budget_tokens")
        if model.thinking_mode == "on":
            if not isinstance(budget, int) or budget < 1024 or budget >= model.max_output_tokens:
                raise ValueError("Claude thinking requires 1024 <= budget_tokens < max_output_tokens")
        elif budget is not None:
            raise ValueError("budget_tokens requires thinking_mode=on")

    def apply(self, payload: dict[str, Any], request) -> ThinkingDecision:
        decision = super().apply(payload, request)
        if decision.effective == "on":
            payload["thinking"] = {"type": "enabled", "budget_tokens": request.model.family_options["budget_tokens"]}
            payload.pop("temperature", None)
        else:
            payload["thinking"] = {"type": "disabled"}
        return decision

    def normalize_usage(self, raw_usage: dict[str, Any], normalized):
        details = raw_usage.get('output_tokens_details') or {}
        if not isinstance(details, dict):
            details = details.model_dump() if hasattr(details, 'model_dump') else {}
        thinking = int(details.get('thinking_tokens') or 0)
        return (replace(normalized, reasoning_tokens=thinking,
                        estimated=normalized.estimated or thinking > normalized.output_tokens)
                if thinking and not normalized.reasoning_tokens else normalized)


class GPT41Profile(ModelFamilyProfile):
    key, label, provider = "openai-gpt-4.1", "GPT-4.1", "openai"
    models = ("gpt-4.1-mini", "gpt-4.1", "gpt-4.1-nano", "openai/gpt-4.1")
    supports_report_cache_scope = True
    family_fields = ("cache_by_report",)

    def set_cache_group(self, payload: dict[str, Any], key: str) -> None:
        extra = dict(payload.get("extra_body") or {})
        extra["prompt_cache_key"] = key
        payload["extra_body"] = extra

    def supports(self, provider: str, physical_model: str, gateway: str) -> bool:
        return (provider == self.provider and
                ((gateway == 'direct' and physical_model in self.models[:3]) or
                 (gateway == 'openrouter' and physical_model == 'openai/gpt-4.1')))


class GPT56Profile(ModelFamilyProfile):
    key, label, provider = "openai-gpt-5.6", "GPT-5.6", "openai"
    models = ("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
    levels = ("low", "medium", "high", "xhigh", "max")
    supports_thinking = supports_flex = True
    supports_report_cache_scope = True
    family_fields = ("cache_by_report",)

    def set_cache_group(self, payload: dict[str, Any], key: str) -> None:
        extra = dict(payload.get("extra_body") or {})
        extra["prompt_cache_key"] = key
        payload["extra_body"] = extra

    def billing_model(self, physical_model: str) -> str:
        return 'gpt-5.6-sol' if physical_model == 'gpt-5.6' else physical_model

    def apply(self, payload: dict[str, Any], request) -> ThinkingDecision:
        decision = super().apply(payload, request)
        # Provider-native forwarding preserves all GPT-5.6 effort values on the
        # verified LiteLLM adapter; the HTTP wire contract checks every level.
        extra = dict(payload.get("extra_body") or {})
        extra["reasoning_effort"] = decision.level if decision.effective == "on" else "none"
        payload["extra_body"] = extra
        payload.pop("reasoning_effort", None)
        # LiteLLM 1.101.0 rejects temperature for this GPT family even when
        # reasoning_effort is none; the rewriter still needs to complete.
        payload.pop("temperature", None)
        return decision

    def context_band(self, usage) -> str:
        return "long" if usage.input_total_tokens > 272_000 else "short"


class DeepSeekV4Profile(ModelFamilyProfile):
    key, label, provider = "deepseek-v4", "DeepSeek V4", "deepseek"
    models = ("deepseek-flash", "deepseek-v4-pro")
    levels = ("low", "high", "max")
    supports_thinking = True
    supports_report_cache_scope = True
    family_fields = ("cache_by_report",)

    def set_cache_group(self, payload: dict[str, Any], key: str) -> None:
        extra = dict(payload.get("extra_body") or {})
        extra["user_id"] = key
        payload["extra_body"] = extra

    def apply(self, payload: dict[str, Any], request) -> ThinkingDecision:
        decision = super().apply(payload, request)
        if request.tool_choice and decision.effective == "on":
            decision = ThinkingDecision("on", "off", override_reason="forced_tool_choice")
        extra = dict(payload.get("extra_body") or {})
        extra["thinking"] = {"type": "enabled" if decision.effective == "on" else "disabled"}
        if decision.effective == "on":
            # LiteLLM 1.90.3 silently omits this control for DeepSeek when it
            # is supplied as a generic completion keyword. The native body is
            # covered by an HTTP-wire contract test.
            extra["reasoning_effort"] = decision.level
            payload.pop("temperature", None)
        else:
            extra.pop("reasoning_effort", None)
            payload.pop("reasoning_effort", None)
        payload["extra_body"] = extra
        return decision

    def pricing_band(self, at, usage) -> str:
        from .pricing_calendar import deepseek_pricing_band
        return deepseek_pricing_band(at)


PROFILES = {item.key: item for item in (
    ClaudeHaiku45Profile(), GPT41Profile(), GPT56Profile(), DeepSeekV4Profile(),
)}


def profile_for(key: str | None, provider: str, model: str, gateway: str):
    if key:
        return PROFILES.get(key)
    return next((item for item in PROFILES.values() if item.supports(provider, model, gateway)), None)


def profile_catalog() -> list[dict[str, Any]]:
    return [{"key": p.key, "label": p.label, "provider": p.provider,
             "models": list(p.models), "levels": list(p.levels),
             "family_fields": list(p.family_fields),
             "supports_thinking": p.supports_thinking, "supports_flex": p.supports_flex,
             "supports_report_cache_scope": p.supports_report_cache_scope}
            for p in PROFILES.values()]
