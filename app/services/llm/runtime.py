"""The only LiteLLM generation boundary in KLARA."""
from __future__ import annotations

import copy
import json
import logging
import re
import time
from dataclasses import replace
from typing import Any

from app.services.observability import start_observation
from .contracts import (
    LLMError, LLMMessage, LLMRequest, LLMResponse, LLMUsage,
    ProviderState, ToolCall,
)
from .profiles import PROFILES, profile_for

LOG = logging.getLogger(__name__)


def _safe_status_code(exc: Exception) -> int | None:
    for value in (
        getattr(exc, "status_code", None),
        getattr(exc, "http_status", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def _safe_provider_error_code(exc: Exception) -> str | None:
    candidates = [getattr(exc, "code", None)]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            candidates.extend((error.get("code"), error.get("type")))
    for value in candidates:
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
            return value
    return None


def _dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    return value.model_dump() if hasattr(value, "model_dump") else {}


def normalize_usage(raw: Any) -> LLMUsage:
    usage = _dict(raw)
    responses_shape = "input_tokens" in usage or "output_tokens" in usage
    details = _dict(usage.get("input_tokens_details" if responses_shape else "prompt_tokens_details"))
    output_details = _dict(usage.get("output_tokens_details" if responses_shape else "completion_tokens_details"))
    read = int(details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or usage.get("cache_read_input_tokens") or 0)
    write = int(details.get("cache_write_tokens") or details.get("cache_creation_tokens") or usage.get("cache_creation_input_tokens") or 0)
    total_input = int(usage.get("input_tokens" if responses_shape else "prompt_tokens") or 0)
    output = int(usage.get("output_tokens" if responses_shape else "completion_tokens") or 0)
    reasoning = int(output_details.get("reasoning_tokens") or 0)
    reliable = (("input_tokens" in usage and "output_tokens" in usage) if responses_shape else
                ("prompt_tokens" in usage and "completion_tokens" in usage)) and (
                total_input >= read + write and output >= reasoning)
    return LLMUsage(
        input_total_tokens=total_input, input_uncached_tokens=max(0, total_input - read - write),
        cache_read_tokens=read, cache_write_tokens=write, output_tokens=output,
        reasoning_tokens=reasoning,
        total_tokens=total_input + output, estimated=not reliable,
    )


def instruction_blocks(request: LLMRequest) -> list[dict]:
    blocks = []
    for section in request.instructions:
        block = {"type": "text", "text": section["text"]}
        if section.get("cache_boundary") and request.cache.enabled and request.model.provider == "anthropic":
            block["cache_control"] = {"type": "ephemeral"}
        blocks.append(block)
    return blocks


class LiteLLMRuntime:
    def __init__(self, completion=None, token_counter=None, cost_resolver=None, responses=None):
        self._completion = completion
        self._responses = responses
        self._token_counter = token_counter
        self._cost_resolver = cost_resolver
        self._owner = f"litellm:{id(self)}"

    def _message(self, message: LLMMessage, request: LLMRequest) -> list[dict]:
        state = message.provider_state
        if state is not None:
            if state.owner != self._owner or state.model != request.model.transport_model:
                raise LLMError("invalid_continuation", "Provider state belongs to a different runtime/model")
            return [copy.deepcopy(state.payload)]
        if message.tool_results:
            return [{"role": "tool", "tool_call_id": result.call_id, "content": result.content}
                    for result in message.tool_results]
        payload = {"role": message.role, "content": message.text}
        if message.tool_calls:
            payload["tool_calls"] = [{"id": call.id, "type": "function", "function": {
                "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False),
            }} for call in message.tool_calls]
        return [payload]

    def _serialize_with_decision(self, request: LLMRequest) -> tuple[dict, dict]:
        profile = PROFILES.get(request.model.family_key) if request.model.family_key else None
        if profile is not None and profile.api_surface == "responses":
            return self._serialize_responses(request, profile)
        import httpx
        messages = [{"role": "system", "content": instruction_blocks(request)}] if request.instructions else []
        for message in request.messages:
            messages.extend(self._message(message, request))
        payload = dict(request.model.provider_options)
        # Critical parameters are owned by KLARA, never overridden by provider options.
        payload.update(model=request.model.transport_model, messages=messages,
                       max_tokens=request.model.max_output_tokens, api_key=request.model.api_key,
                       num_retries=2, timeout=httpx.Timeout(600.0, connect=5.0))
        if request.tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": tool.name, "description": tool.description, "parameters": tool.parameters,
            }} for tool in request.tools]
        if request.tool_choice:
            payload["tool_choice"] = {"type": "function", "function": {"name": request.tool_choice}}
            if request.model.provider == "deepseek" and request.model.gateway == "direct":
                # DeepSeek does not support forced tool choice while thinking is enabled.
                # LiteLLM 1.90 drops reasoning_effort="none" for this provider, so this
                # provider-native field is intentionally owned by KLARA.
                extra_body = dict(payload.get("extra_body") or {})
                extra_body["thinking"] = {"type": "disabled"}
                payload["extra_body"] = extra_body
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.model.service_tier:
            payload["service_tier"] = request.model.service_tier
        if request.model.reasoning_effort and not request.model.family_key:
            payload["reasoning_effort"] = request.model.reasoning_effort
        if request.thinking_mode_override not in (None, "off"):
            raise LLMError("invalid_thinking_override", "Only an explicit off thinking override is supported")
        decision = {}
        profile = PROFILES.get(request.model.family_key) if request.model.family_key else None
        if request.model.family_key and profile is None:
            raise LLMError("invalid_model_profile", "Unknown model family profile")
        profile_request = request
        if profile is None and request.thinking_mode_override == "off":
            profile = profile_for(None, request.model.provider, request.model.physical_model,
                                  request.model.gateway)
            if profile is not None:
                # Legacy role defaults have no family_key. Resolve only a known wire
                # contract for this request; keep the assigned model unchanged.
                off_model = replace(request.model, family_key=profile.key, thinking_mode="off",
                                    reasoning_effort=None,
                                    family_options={key: value for key, value in request.model.family_options.items()
                                                    if key != "budget_tokens"})
                profile_request = replace(request, model=off_model)
        if profile is not None:
            decision = profile.apply(payload, profile_request).metadata()
            if profile_request is not request:
                decision["requested_thinking_mode"] = request.model.thinking_mode or "provider_default"
            profile.apply_cache(payload, profile_request)
        elif request.thinking_mode_override == "off":
            if request.model.provider != "deepseek" or request.model.gateway != "direct":
                raise LLMError("unsupported_thinking_control", "Cannot ensure thinking is off for this model")
            # A legacy direct DeepSeek model still needs the native toggle: the
            # provider defaults to thinking enabled when it is omitted.
            for key in ("thinking", "reasoning", "reasoning_effort", "output_config"):
                payload.pop(key, None)
            extra = dict(payload.get("extra_body") or {})
            for key in ("reasoning", "reasoning_effort", "output_config"):
                extra.pop(key, None)
            extra["thinking"] = {"type": "disabled"}
            payload["extra_body"] = extra
            decision = {"requested_thinking_mode": request.model.thinking_mode or "provider_default",
                        "effective_thinking_mode": "off", "thinking_level": None,
                        "thinking_override_reason": request.thinking_override_reason or "request_override"}
        elif request.cache.enabled and request.model.capabilities.supports_cache_key and request.cache.scope.key:
            payload["prompt_cache_key"] = request.cache.scope.key
        return payload, decision

    def _serialize_responses(self, request: LLMRequest, profile) -> tuple[dict, dict]:
        import httpx
        if request.thinking_mode_override not in (None, "off"):
            raise LLMError("invalid_thinking_override", "Only an explicit off thinking override is supported")
        profile.validate(request.model)
        previous_id = None
        start = 0
        for index, message in enumerate(request.messages):
            state = message.provider_state
            if state is None:
                continue
            if state.owner != self._owner or state.model != request.model.transport_model:
                raise LLMError("invalid_continuation", "Provider state belongs to a different runtime/model")
            if not isinstance(state.payload, dict) or not isinstance(state.payload.get("response_id"), str):
                raise LLMError("invalid_continuation", "Invalid Responses continuation")
            previous_id, start = state.payload["response_id"], index + 1
        input_items = []
        for message in request.messages[start:]:
            if message.tool_results:
                input_items.extend({"type": "function_call_output", "call_id": item.call_id,
                                    "output": item.content} for item in message.tool_results)
            elif message.tool_calls:
                raise LLMError("invalid_continuation", "Tool calls require Responses provider state")
            else:
                input_items.append({"role": message.role, "content": message.text})
        if previous_id and not any(item.get("type") == "function_call_output" for item in input_items):
            raise LLMError("invalid_continuation", "Responses continuation requires tool output")
        payload = dict(request.model.provider_options)
        payload.update(model=request.model.transport_model,
                       input=input_items, max_output_tokens=request.model.max_output_tokens,
                       api_key=request.model.api_key, num_retries=2,
                       timeout=httpx.Timeout(600.0, connect=5.0))
        if request.instructions:
            payload["instructions"] = "\n\n".join(str(section["text"]) for section in request.instructions)
        if previous_id:
            payload["previous_response_id"] = previous_id
        if request.tools:
            payload["tools"] = [{"type": "function", "name": tool.name,
                                 "description": tool.description, "parameters": tool.parameters}
                                for tool in request.tools]
        if request.tool_choice:
            payload["tool_choice"] = {"type": "function", "name": request.tool_choice}
        if request.model.service_tier:
            payload["service_tier"] = request.model.service_tier
        decision = profile.apply(payload, request).metadata()
        if request.cache.enabled:
            payload["extra_body"] = {**dict(payload.get("extra_body") or {}),
                                     "prompt_cache_options": {"mode": "implicit", "ttl": "30m"}}
        else:
            payload["extra_body"] = {**dict(payload.get("extra_body") or {}),
                                     "prompt_cache_options": {"mode": "explicit", "ttl": "30m"}}
        profile.apply_cache(payload, request)
        return payload, decision

    def serialize(self, request: LLMRequest) -> dict:
        return self._serialize_with_decision(request)[0]

    async def generate(self, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        import datetime
        request_started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with start_observation(name=request.operation, as_type="generation",
                               model=request.model.physical_model,
                               input={"instructions": request.instructions,
                                      "messages": [m.public_dict() for m in request.messages]}) as span:
            try:
                payload, decision = self._serialize_with_decision(request)
                profile = PROFILES.get(request.model.family_key) if request.model.family_key else None
                if profile is not None and profile.verbosity_levels:
                    decision["effective_verbosity"] = request.model.default_verbosity or "provider_default"
                if profile is not None and profile.api_surface == "responses":
                    completion = self._responses
                    if completion is None:
                        from litellm import aresponses
                        completion = aresponses
                else:
                    completion = self._completion
                    if completion is None:
                        from litellm import acompletion
                        completion = acompletion
                raw = await completion(**payload)
                data = _dict(raw)
                usage = normalize_usage(data.get("usage"))
                if profile is not None:
                    usage = profile.normalize_usage(_dict(data.get("usage")), usage)
                calls = []
                if profile is not None and profile.api_surface == "responses":
                    if data.get("status") != "completed":
                        raise LLMError("incomplete_response", "Responses request did not complete")
                    texts = []
                    for item in data.get("output") or []:
                        item = _dict(item)
                        if item.get("type") == "function_call":
                            raw_arguments = item.get("arguments") or "{}"
                            try:
                                arguments = raw_arguments if isinstance(raw_arguments, dict) else json.loads(raw_arguments)
                            except (ValueError, TypeError):
                                arguments = {}
                            calls.append(ToolCall(item["call_id"], item["name"],
                                                  arguments if isinstance(arguments, dict) else {}))
                        elif item.get("type") == "message":
                            for content in item.get("content") or []:
                                content = _dict(content)
                                if content.get("type") == "output_text":
                                    texts.append(content.get("text") or "")
                    answer_text = "\n".join(texts) or data.get("output_text") or ""
                    finish_reason = data.get("status")
                    provider_created_at = data.get("created_at")
                    response_id = data.get("id")
                    state = (ProviderState(self._owner, request.model.transport_model,
                                           {"response_id": response_id})
                             if calls and isinstance(response_id, str) else None)
                    if calls and state is None:
                        raise LLMError("invalid_continuation", "Responses tool call has no response id")
                else:
                    choice = _dict(data["choices"][0])
                    message = _dict(choice["message"])
                    for item in message.get("tool_calls") or []:
                        call = _dict(item)
                        function = _dict(call.get("function"))
                        raw_arguments = function.get("arguments") or {}
                        if isinstance(raw_arguments, dict):
                            arguments = raw_arguments
                        else:
                            try:
                                arguments = json.loads(raw_arguments)
                            except (ValueError, TypeError):
                                arguments = {}
                        calls.append(ToolCall(call["id"], function["name"], arguments if isinstance(arguments, dict) else {}))
                    answer_text = message.get("content") or ""
                    finish_reason = choice.get("finish_reason")
                    provider_created_at = data.get("created")
                    state = ProviderState(self._owner, request.model.transport_model, copy.deepcopy(message))
                response = LLMResponse(
                    text=answer_text, tool_calls=calls,
                    usage=usage, finish_reason=finish_reason,
                    provider=request.model.provider, model=data.get("model") or request.model.physical_model,
                    provider_state=state,
                    actual_service_tier=data.get("service_tier"),
                    provider_created_at=provider_created_at, thinking_decision={
                        **decision, "request_started_at": request_started_at,
                        "provider_created_at": provider_created_at,
                        "actual_service_tier": data.get("service_tier"),
                    },
                )
                if span is not None:
                    costs = None
                    if self._cost_resolver is not None:
                        try:
                            costs = self._cost_resolver(request.model, response.usage, response=response)
                        except Exception:
                            # Optional telemetry cannot change the generation result.
                            pass
                    update = {
                        "output": {"text": response.text, "tool_names": [c.name for c in calls]},
                        "usage_details": {"input": response.usage.input_total_tokens,
                                          "output": response.usage.output_tokens},
                        "metadata": {**request.model.metadata(), "actual_model": response.model,
                                     **response.thinking_decision,
                                     "usage": response.usage.metadata(), "cache_scope": request.cache.scope.key,
                                     "latency_ms": round((time.monotonic() - started) * 1000)},
                    }
                    if costs is not None:
                        response.pricing_quote = costs
                        if costs.get("billing_status") == "verified":
                            update["cost_details"] = {key: costs[key] for key in
                                ("input", "output", "cache_write", "cache_read", "total")}
                        update["metadata"]["pricing"] = {key: costs.get(key) for key in
                            ("pricing_id", "pricing_tier", "context_band", "service_tier", "calendar_version", "billing_status")}
                    span.update(**update)
                return response
            except Exception as exc:
                kind = getattr(exc, "kind", None)
                if kind is None:
                    name = type(exc).__name__
                    text = str(exc).lower()
                    kind = "context_too_large" if name == "ContextWindowExceededError" or "prompt is too long" in text or ("maximum" in text and "tokens" in text) else "provider_error"
                status_code = getattr(exc, "status_code", None) if isinstance(exc, LLMError) else _safe_status_code(exc)
                provider_error_code = (
                    getattr(exc, "provider_error_code", None)
                    if isinstance(exc, LLMError)
                    else _safe_provider_error_code(exc)
                )
                safe_metadata = {
                    **request.model.metadata(),
                    "error_kind": kind,
                    "provider_http_status": status_code,
                    "provider_error_code": provider_error_code,
                }
                safe_metadata = {key: value for key, value in safe_metadata.items() if value is not None}
                LOG.warning(
                    "LLM request failed provider=%s model_key=%s operation=%s kind=%s http_status=%s "
                    "provider_error_code=%s exception_type=%s",
                    request.model.provider,
                    request.model.model_key,
                    request.operation,
                    kind,
                    status_code,
                    provider_error_code,
                    type(exc).__name__,
                )
                if span is not None:
                    span.update(level="ERROR", status_message=kind, metadata=safe_metadata)
                # Do not leak SDK exceptions containing raw requests/reasoning or credentials.
                raise LLMError(
                    kind,
                    f"Model request failed: {kind}",
                    provider=request.model.provider,
                    status_code=status_code,
                    provider_error_code=provider_error_code,
                ) from None

    async def count_tokens(self, request: LLMRequest) -> int:
        from .token_counting import count_tokens
        if self._token_counter is not None:
            return await self._token_counter(request)
        return await count_tokens(request)
