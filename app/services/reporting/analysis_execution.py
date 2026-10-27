"""Scheduling policy for the independent analyses of one report (V1.4 controlled concurrency).

This is the ONLY place that knows *how many* questions run at once and *when* the next one may
start. It knows nothing about Power BI, DAX, models, persistence, the Coordinator, the Composer
or the Writer: ``ReportGenerator`` hands it a ``run_one(question)`` coroutine (analysis + usage
+ progress for ONE question) and it returns the finished sections in the order the questions
were given. That order is the editorial order (definition/snapshot); the order in which the
questions happen to finish is never observable from the result.

Two strategies share one tiny contract:

* ``SequentialAnalysisExecution``        one at a time (``REPORT_ANALYSIS_CONCURRENCY=1``, ``/generate``, CLI).
* ``ControlledConcurrentAnalysisExecution`` at most N in flight; the next one is only started when a slot frees up.

Both are cooperative about stopping: when ``checkpoint`` raises (cancellation) or a question
raises (a global failure, a lost lease, a cancel raised while persisting), NO new question is
started; questions already in flight are allowed to finish (and persist their own result),
then the first stop reason is re-raised. A question failing normally never gets here: the
generator turns it into a ``had_error`` section, so one failure never cancels its siblings.

Future policies (per-provider / per-model limits, adaptive concurrency) replace the strategy
or grow this module; ``ReportGenerator`` and ``AnalyticsExecutor`` do not change.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from .contracts import ReportQuestion, ReportSection

LOG = logging.getLogger(__name__)

ENV_ANALYSIS_CONCURRENCY = "REPORT_ANALYSIS_CONCURRENCY"
# Conservative: LLM provider rate limits and Power BI DAX throttling are not measured yet (V1.5).
DEFAULT_ANALYSIS_CONCURRENCY = 2
# Hard ceiling: "unlimited" is never a valid configuration.
MAX_ANALYSIS_CONCURRENCY = 16

RunOne = Callable[[ReportQuestion], Awaitable[ReportSection]]
Checkpoint = Callable[[], Awaitable[None] | None]


def parse_analysis_concurrency(raw: Any) -> int:
    """Validate a configured limit: an integer in ``[1, MAX_ANALYSIS_CONCURRENCY]``."""
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{ENV_ANALYSIS_CONCURRENCY} must be an integer >= 1") from None
    if value < 1 or value > MAX_ANALYSIS_CONCURRENCY:
        raise ValueError(f"{ENV_ANALYSIS_CONCURRENCY} must be between 1 and {MAX_ANALYSIS_CONCURRENCY}")
    return value


def resolve_analysis_concurrency(config: Mapping[str, Any] | None = None, *, strict: bool = True) -> int:
    """Server-side limit: app config first, then the environment, then the safe default.

    ``strict=False`` falls back to the default instead of raising (read-only surfaces such as
    the status payload must never fail because of a bad deployment value; a run still does).
    """
    raw = (config or {}).get(ENV_ANALYSIS_CONCURRENCY)
    if raw in (None, ""):
        raw = os.getenv(ENV_ANALYSIS_CONCURRENCY)
    if raw in (None, ""):
        return DEFAULT_ANALYSIS_CONCURRENCY
    try:
        return parse_analysis_concurrency(raw)
    except ValueError:
        if strict:
            raise
        LOG.warning("Invalid %s ignored; using %s", ENV_ANALYSIS_CONCURRENCY, DEFAULT_ANALYSIS_CONCURRENCY)
        return DEFAULT_ANALYSIS_CONCURRENCY


@dataclass
class AnalysisStats:
    """Operational metadata of one analysis stage (no question/answer content)."""
    configured_concurrency: int = 1
    questions_total: int = 0
    questions_started: int = 0
    peak_active: int = 0
    duration_ms: int | None = None
    active: int = 0

    def enter(self) -> None:
        self.active += 1
        self.questions_started += 1
        self.peak_active = max(self.peak_active, self.active)

    def leave(self) -> None:
        self.active -= 1

    def to_payload(self) -> dict[str, Any]:
        return {
            "configured_concurrency": self.configured_concurrency,
            "questions_total": self.questions_total,
            "questions_started": self.questions_started,
            "peak_active": self.peak_active,
            "analysis_stage_duration_ms": self.duration_ms,
        }


class AnalysisExecutionStrategy(Protocol):
    concurrency: int

    async def execute(
        self, questions: Sequence[ReportQuestion], run_one: RunOne, *,
        checkpoint: Checkpoint | None = None, stats: AnalysisStats | None = None,
    ) -> list[ReportSection]: ...


def _ensure_unique(questions: Sequence[ReportQuestion]) -> None:
    keys = [question.key for question in questions]
    if len(set(keys)) != len(keys):
        duplicated = sorted({key for key in keys if keys.count(key) > 1})
        raise ValueError(f"duplicate question keys cannot be scheduled: {', '.join(duplicated)}")


async def _call(checkpoint: Checkpoint | None) -> None:
    if checkpoint is not None:
        result = checkpoint()
        if inspect.isawaitable(result):
            await result


class SequentialAnalysisExecution:
    """One question at a time, in the given order (the V1.0-V1.3 behaviour)."""
    concurrency = 1

    async def execute(self, questions, run_one, *, checkpoint=None, stats=None) -> list[ReportSection]:
        _ensure_unique(questions)
        stats = stats if stats is not None else AnalysisStats()
        stats.configured_concurrency, stats.questions_total = 1, len(questions)
        started = time.monotonic()
        results: dict[str, ReportSection] = {}
        try:
            for question in questions:
                await _call(checkpoint)
                stats.enter()
                try:
                    results[question.key] = await run_one(question)
                finally:
                    stats.leave()
        finally:
            stats.duration_ms = round((time.monotonic() - started) * 1000)
        return [results[question.key] for question in questions]


class ControlledConcurrentAnalysisExecution:
    """At most ``limit`` questions in flight; the next one starts only when a slot frees up.

    Questions are launched lazily and in order, so on cancellation the not-yet-started ones
    simply never exist (nothing to cancel). Results are collected by question key and
    re-ordered at the end: completion order never leaks into the returned list.
    """

    def __init__(self, limit: int):
        self.concurrency = parse_analysis_concurrency(limit)

    async def execute(self, questions, run_one, *, checkpoint=None, stats=None) -> list[ReportSection]:
        _ensure_unique(questions)
        stats = stats if stats is not None else AnalysisStats()
        stats.configured_concurrency, stats.questions_total = self.concurrency, len(questions)
        started = time.monotonic()
        results: dict[str, ReportSection] = {}
        pending = deque(questions)
        active: set[asyncio.Future] = set()
        stop: Exception | None = None  # first reason to stop starting questions

        async def worker(question: ReportQuestion) -> None:
            nonlocal stop
            stats.enter()
            try:
                results[question.key] = await run_one(question)
            except Exception as exc:  # noqa: BLE001 - recorded, re-raised once in-flight work is done
                if stop is None:
                    stop = exc
            finally:
                stats.leave()

        try:
            while True:
                while stop is None and pending and len(active) < self.concurrency:
                    try:
                        await _call(checkpoint)
                    except Exception as exc:  # noqa: BLE001 - cancellation request or lost lease
                        stop = exc
                        break
                    if stop is not None or len(active) >= self.concurrency:
                        break  # something finished/failed while the checkpoint was awaited
                    active.add(asyncio.ensure_future(worker(pending.popleft())))
                if not active:
                    break
                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                active -= done
        finally:
            # Only reachable with live tasks on an external cancellation / BaseException: never leak them.
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            stats.duration_ms = round((time.monotonic() - started) * 1000)
        if stop is not None:
            raise stop
        return [results[question.key] for question in questions]


def build_analysis_execution(concurrency: int) -> AnalysisExecutionStrategy:
    """Strategy for a configured limit; ``1`` is exactly the sequential behaviour."""
    limit = parse_analysis_concurrency(concurrency)
    return SequentialAnalysisExecution() if limit == 1 else ControlledConcurrentAnalysisExecution(limit)
