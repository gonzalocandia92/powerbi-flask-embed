"""Resilience around an ``AnalyticsExecutor``: repeat a whole analytical execution after a transient failure.

Responsibilities are split on purpose:

* ``AnalyticsRetryPolicy``        decides IF a failed result deserves another attempt and HOW LONG to wait. It only
                                  reads the normalized ``AnalyticsResult.failure``; it knows no provider, SDK or HTTP
                                  detail. A future policy (e.g. attempt 3 on another tier/model) replaces it.
* ``RetryingAnalyticsExecutor``   decorator that implements ``AnalyticsExecutor`` and drives the attempts. Consumers
                                  (Reporting) keep depending on ``AnalyticsExecutor`` only.

This is NOT the LLM runtime's own retry (``LiteLLMRuntime`` ``num_retries``): that one repeats a single HTTP call,
this one repeats the full execution of one question. Both are independent.

Every attempt re-runs the SAME prepared execution (same question, model, tier, skills, history, context). The final
result is the one of the last attempt; ``ai_usage_events`` accumulate ALL attempts so the ledger is written once,
by the consumer, with nothing lost; failed attempts that were overcome are kept in ``recovered_errors``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .contracts import (
    AnalyticsRequest,
    AnalyticsResult,
    PreparedAnalyticsExecution,
)

LOG = logging.getLogger(__name__)

ENV_RETRY_MAX_ATTEMPTS = "REPORT_ANALYSIS_RETRY_MAX_ATTEMPTS"
ENV_RETRY_BASE_DELAY_SECONDS = "REPORT_ANALYSIS_RETRY_BASE_DELAY_SECONDS"
DEFAULT_RETRY_MAX_ATTEMPTS = 2  # 1 execution + at most 1 retry
DEFAULT_RETRY_BASE_DELAY_SECONDS = 5.0
# Hard ceilings: unbounded retries / waits are never a valid configuration.
MAX_RETRY_ATTEMPTS_LIMIT = 5
MAX_RETRY_DELAY_SECONDS_LIMIT = 120.0

Sleeper = Callable[[float], Awaitable[None]]


class AnalyticsRetryPolicy(Protocol):
    max_attempts: int

    def should_retry(self, result: AnalyticsResult, attempt: int) -> bool:
        """``attempt`` is the 1-based number of the attempt that produced ``result``."""
        ...

    def delay_seconds(self, attempt: int) -> float:
        """Wait before the attempt that follows ``attempt``."""
        ...


class TransientFailureRetryPolicy:
    """Retry only failures the Analytics layer flagged as transient and that come from the main model."""

    def __init__(self, max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS,
                 base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS):
        self.max_attempts = min(max(int(max_attempts), 1), MAX_RETRY_ATTEMPTS_LIMIT)
        self.base_delay_seconds = min(max(float(base_delay_seconds), 0.0), MAX_RETRY_DELAY_SECONDS_LIMIT)

    def should_retry(self, result: AnalyticsResult, attempt: int) -> bool:
        if attempt >= self.max_attempts or not result.had_error:
            return False
        failure = result.failure
        return failure is not None and failure.retryable is True and failure.scope == "main_model"

    def delay_seconds(self, attempt: int) -> float:
        # Constant for now; exponential backoff / jitter would only change this method.
        return self.base_delay_seconds


def _read_setting(config: Mapping[str, Any] | None, name: str) -> Any:
    raw = (config or {}).get(name)
    return os.getenv(name) if raw in (None, "") else raw


def build_retry_policy(config: Mapping[str, Any] | None = None) -> TransientFailureRetryPolicy:
    """Server-side configuration (app config, then environment, then defaults). Invalid values fall back to the
    defaults (a resilience knob must never break a run); out-of-range values are clamped by the policy."""
    def parse(name: str, kind, default):
        raw = _read_setting(config, name)
        if raw in (None, ""):
            return default
        try:
            if isinstance(raw, bool):
                raise ValueError
            return kind(str(raw).strip())
        except ValueError:
            LOG.warning("Invalid %s ignored; using %s", name, default)
            return default

    return TransientFailureRetryPolicy(
        max_attempts=parse(ENV_RETRY_MAX_ATTEMPTS, int, DEFAULT_RETRY_MAX_ATTEMPTS),
        base_delay_seconds=parse(ENV_RETRY_BASE_DELAY_SECONDS, float, DEFAULT_RETRY_BASE_DELAY_SECONDS),
    )


def _stamp_events(events: list[dict[str, Any]], attempt: int) -> list[dict[str, Any]]:
    """Copy of ``events`` tagged with the attempt that produced them (``attempt``/``retry`` follow the ledger
    semantics already understood by the cost summary)."""
    stamped = []
    for event in events:
        event = dict(event)
        event["metadata_json"] = {
            **(event.get("metadata_json") or {}),
            "attempt": attempt, "retry": attempt > 1,
            "analysis_attempt": attempt, "analysis_retry": attempt > 1,
        }
        stamped.append(event)
    return stamped


def _recovered_entry(result: AnalyticsResult, attempt: int) -> dict[str, Any]:
    """Safe summary of an overcome failure: allow-listed fields only, never the raw provider/error message."""
    failure = result.failure
    entry = {
        "reason": (failure.reason if failure else None) or result.failure_reason,
        "scope": failure.scope if failure else None,
        "attempt": attempt,
        "retryable": failure.retryable if failure else None,
        "provider": failure.provider if failure else None,
        "provider_http_status": failure.http_status if failure else None,
        "provider_error_code": failure.provider_error_code if failure else None,
    }
    return {key: value for key, value in entry.items() if value is not None}


class RetryingAnalyticsExecutor:
    """``AnalyticsExecutor`` decorator that repeats a transiently failed execution according to a policy."""

    def __init__(self, delegate, *, policy: AnalyticsRetryPolicy | None = None, sleep: Sleeper | None = None):
        self.delegate = delegate
        self.policy = policy if policy is not None else TransientFailureRetryPolicy()
        self._sleep = sleep if sleep is not None else asyncio.sleep

    async def prepare(self, request: AnalyticsRequest) -> PreparedAnalyticsExecution:
        return await self.delegate.prepare(request)

    async def execute_prepared(
        self,
        prepared: PreparedAnalyticsExecution,
        *,
        history: list[dict[str, Any]] | None = None,
        execution_id: str | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> AnalyticsResult:
        base_trace = trace_context if trace_context is not None else prepared.request.trace_context

        async def run(attempt: int) -> AnalyticsResult:
            return await self.delegate.execute_prepared(
                prepared, history=history, execution_id=execution_id,
                trace_context={**base_trace, "analysis_attempt": attempt, "analysis_retry": attempt > 1},
            )

        return await self._run_attempts(run)

    async def execute(self, request: AnalyticsRequest) -> AnalyticsResult:
        if hasattr(self.delegate, "prepare") and hasattr(self.delegate, "execute_prepared"):
            # Preflight happens once: global errors (billing, configuration, model, report) propagate untouched.
            prepared = await self.prepare(request)
            return await self.execute_prepared(prepared)

        async def run(attempt: int) -> AnalyticsResult:  # delegates that only expose ``execute``
            trace = {**request.trace_context, "analysis_attempt": attempt, "analysis_retry": attempt > 1}
            return await self.delegate.execute(replace(request, trace_context=trace))

        return await self._run_attempts(run)

    async def _run_attempts(self, run: Callable[[int], Awaitable[AnalyticsResult]]) -> AnalyticsResult:
        attempt = 1
        prior_events: list[dict[str, Any]] = []
        prior_recovered: list[dict[str, Any]] = []
        while True:
            result = await run(attempt)
            if not self.policy.should_retry(result, attempt):
                break
            LOG.warning("Analytics attempt %s failed transiently (reason=%s); retrying",
                        attempt, result.failure_reason)
            prior_events.extend(_stamp_events(result.ai_usage_events, attempt))
            prior_recovered.extend({**item, "attempt": attempt} for item in result.recovered_errors)
            prior_recovered.append(_recovered_entry(result, attempt))
            # Cancellation during the backoff propagates: no further attempt starts.
            await self._sleep(self.policy.delay_seconds(attempt))
            attempt += 1
        if attempt == 1:
            return result  # no retry happened: the result is returned exactly as the delegate produced it
        # Functional data (answer, DAX, tools, model, tokens...) comes from the final attempt only; usage events
        # and overcome failures accumulate so the consumer records every provider call exactly once.
        return replace(
            result,
            ai_usage_events=prior_events + _stamp_events(result.ai_usage_events, attempt),
            recovered_errors=prior_recovered + list(result.recovered_errors),
        )
