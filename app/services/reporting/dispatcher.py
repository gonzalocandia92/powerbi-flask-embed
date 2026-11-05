"""Occurrence dispatcher (Phase 3): moves materialised occurrences towards a run.

1. ``waiting_data`` occurrences whose ``next_check_at`` has arrived ask ``DataFreshnessService`` whether the
   Power BI data covers their frozen period:

* fresh    -> ``ready`` (with ``refresh_completed_at``);
* blocked  -> ``blocked`` (permanent: credentials, missing report/dataset, unknown policy, unreadable snapshot);
* wait     -> stays ``waiting_data``; the next check is scheduled with exponential backoff (capped, and never
              later than the deadline) and the reason is kept in ``last_error_code``;
* wait past ``refresh_deadline_at`` -> ``expired``: no report is generated over possibly stale data.

   The check comes BEFORE the deadline test, so data that arrived while the scheduler was down is still used.
2. ``ready`` occurrences are enqueued (oldest first) while the concurrency caps admit them:
   ``runs.enqueue_occurrence`` turns occurrence + run into ONE transaction; one that does not fit stays
   ``ready`` for the next poll.

Every write is a guarded transition: two dispatchers never move the same occurrence twice. Without a ``runs``
collaborator only step 1 runs. No Flask, no SQLAlchemy here.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from app.persistence.schedule_states import (
    ERROR_REFRESH_DEADLINE_EXCEEDED, ERROR_SNAPSHOT_UNREADABLE, FRESHNESS_IMPORT_REFRESH_AFTER_PERIOD_END,
    OCCURRENCE_BLOCKED, OCCURRENCE_EXPIRED, OCCURRENCE_READY, OCCURRENCE_WAITING_DATA,
)

from .concurrency import ConcurrencyCaps, RunUsage
from .freshness import BLOCKED, FRESH, DataFreshnessService, FreshnessContext, FreshnessVerdict
from .schedule_contracts import OccurrenceView
from .schedule_rules import as_utc
from .snapshot import period_from_snapshot

LOG = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _env_int(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    try:
        value = int(env.get(name) or default)
    except ValueError:
        return default
    return value if value >= minimum else default


@dataclass(frozen=True)
class DispatcherSettings:
    backoff_base_minutes: int = 5
    backoff_max_minutes: int = 60
    max_occurrences_per_tick: int = 50

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DispatcherSettings":
        env = os.environ if env is None else env
        base = _env_int(env, "REPORT_SCHEDULE_FRESHNESS_BACKOFF_BASE_MINUTES", 5, 1)
        return cls(
            backoff_base_minutes=base,
            backoff_max_minutes=max(base, _env_int(env, "REPORT_SCHEDULE_FRESHNESS_BACKOFF_MAX_MINUTES", 60, 1)),
            max_occurrences_per_tick=_env_int(env, "REPORT_SCHEDULER_MAX_OCCURRENCES_PER_TICK", 50, 1),
        )

    def backoff(self, attempts: int) -> timedelta:
        """Wait after the ``attempts``-th failed check: base, 2*base, 4*base ... capped at max."""
        exponent = min(max(attempts, 1) - 1, 16)
        return timedelta(minutes=min(self.backoff_base_minutes * 2 ** exponent, self.backoff_max_minutes))


@dataclass
class DispatchResult:
    ready: list[int] = field(default_factory=list)
    waiting: list[int] = field(default_factory=list)
    expired: list[int] = field(default_factory=list)
    blocked: list[int] = field(default_factory=list)
    lost: list[int] = field(default_factory=list)    # another dispatcher moved it first
    errors: list[int] = field(default_factory=list)
    enqueued: dict[int, str] = field(default_factory=dict)   # occurrence id -> run id
    deferred: list[int] = field(default_factory=list)        # ready, but the caps are full


class OccurrenceDispatcher:
    def __init__(self, store, freshness: DataFreshnessService, *, runs=None, caps: ConcurrencyCaps | None = None,
                 schedule_policy: Callable[[int], str] | None = None, settings: DispatcherSettings | None = None,
                 clock: Callable[[], datetime] | None = None):
        """``runs``: ``open_scheduled_usage() -> {report_id: open runs}`` and ``enqueue_occurrence(id) -> run | None``
        (``ReportRunStore``). ``schedule_policy(schedule_id) -> freshness policy name`` (default: via the store)."""
        self.store = store
        self.freshness = freshness
        self.runs = runs
        self.caps = caps or ConcurrencyCaps()
        self.schedule_policy = schedule_policy or self._policy_from_store
        self.settings = settings or DispatcherSettings()
        self.clock = clock or _utcnow

    def tick(self, now: datetime | None = None) -> DispatchResult:
        now = as_utc(now or self.clock())
        result = DispatchResult()
        self.check_waiting(now, result)
        if self.runs is not None:
            self.enqueue_ready(result)
        return result

    # ── ready -> enqueued ──────────────────────────────────────────────────────────────────────

    def enqueue_ready(self, result: DispatchResult) -> DispatchResult:
        ready = self.store.list_occurrences_in([OCCURRENCE_READY], limit=self.settings.max_occurrences_per_tick)
        if not ready:
            return result
        usage = RunUsage(dict(self.runs.open_scheduled_usage()))
        for occurrence in ready:
            snapshot = occurrence.definition_snapshot if isinstance(occurrence.definition_snapshot, dict) else {}
            report_id = snapshot.get("report_id")
            if not isinstance(report_id, int) or isinstance(report_id, bool):
                if self.store.transition(occurrence.id, expected_status=OCCURRENCE_READY,
                                         new_status=OCCURRENCE_BLOCKED,
                                         changes={"last_error_code": ERROR_SNAPSHOT_UNREADABLE}):
                    result.blocked.append(occurrence.id)
                continue
            if not self.caps.admits(report_id, usage):
                result.deferred.append(occurrence.id)
                continue
            try:
                run = self.runs.enqueue_occurrence(occurrence.id)
            except Exception:  # noqa: BLE001 - stays ready; retried next poll
                result.errors.append(occurrence.id)
                LOG.exception("[ReportScheduler] enqueue of occurrence=%s failed", occurrence.id)
                continue
            if run is None:
                result.lost.append(occurrence.id)
                continue
            usage.add(report_id)
            result.enqueued[occurrence.id] = run.id
            LOG.info("[ReportScheduler] occurrence=%s enqueued run=%s report=%s", occurrence.id, run.id, report_id)
        if result.deferred:
            LOG.info("[ReportScheduler] concurrency caps full: deferred occurrences=%s", result.deferred)
        return result

    # ── waiting_data -> ready | expired | blocked ──────────────────────────────────────────────

    def check_waiting(self, now: datetime, result: DispatchResult) -> DispatchResult:
        for occurrence in self.store.list_occurrences_in(
                [OCCURRENCE_WAITING_DATA], check_due_at=now, limit=self.settings.max_occurrences_per_tick):
            try:
                self.check_occurrence(occurrence, now, result)
            except Exception:  # noqa: BLE001 - one bad occurrence must not stop the others
                result.errors.append(occurrence.id)
                LOG.exception("[ReportScheduler] freshness check of occurrence=%s failed", occurrence.id)
        if any((result.ready, result.expired, result.blocked, result.errors)):
            LOG.info("[ReportScheduler] freshness ready=%s expired=%s blocked=%s waiting=%s errors=%s",
                     result.ready, result.expired, result.blocked, result.waiting, result.errors)
        return result

    def check_occurrence(self, occurrence: OccurrenceView, now: datetime, result: DispatchResult) -> None:
        context = self._context(occurrence, now)
        if context is None:
            self._move(occurrence, OCCURRENCE_BLOCKED, result.blocked, result, {
                "refresh_checked_at": now, "next_check_at": None, "last_error_code": ERROR_SNAPSHOT_UNREADABLE,
                "last_error_message": "La ocurrencia no tiene un snapshot con período y report válidos."})
            return
        verdict = self.freshness.check(self.schedule_policy(occurrence.schedule_id), context)
        attempts = occurrence.check_attempts + 1
        checked = {"refresh_checked_at": now, "check_attempts": attempts}
        if verdict.kind == FRESH:
            self._move(occurrence, OCCURRENCE_READY, result.ready, result, {
                **checked, "refresh_completed_at": verdict.refresh_completed_at, "next_check_at": None,
                "last_error_code": None, "last_error_message": None})
        elif verdict.kind == BLOCKED:
            self._move(occurrence, OCCURRENCE_BLOCKED, result.blocked, result, {
                **checked, "next_check_at": None, **self._reason(verdict)})
        elif occurrence.refresh_deadline_at is not None and now >= occurrence.refresh_deadline_at:
            self._move(occurrence, OCCURRENCE_EXPIRED, result.expired, result, {
                **checked, "next_check_at": None, "last_error_code": ERROR_REFRESH_DEADLINE_EXCEEDED,
                "last_error_message": f"Venció la espera de datos ({verdict.code}): {verdict.message or ''}"[:2000]})
        else:
            next_check = now + self.settings.backoff(attempts)
            if occurrence.refresh_deadline_at is not None:
                next_check = min(next_check, occurrence.refresh_deadline_at)  # expire on time
            self._move(occurrence, None, result.waiting, result, {
                **checked, "next_check_at": next_check, **self._reason(verdict)})

    # ── internals ──────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _context(occurrence: OccurrenceView, now: datetime) -> FreshnessContext | None:
        snapshot = occurrence.definition_snapshot
        try:
            period = period_from_snapshot(snapshot) if isinstance(snapshot, dict) else None
            report_id = snapshot.get("report_id") if isinstance(snapshot, dict) else None
        except (KeyError, TypeError, ValueError):
            return None
        if period is None or not isinstance(report_id, int) or isinstance(report_id, bool):
            return None
        return FreshnessContext(report_id=report_id, period=period, now=now)

    @staticmethod
    def _reason(verdict: FreshnessVerdict) -> dict:
        return {"last_error_code": verdict.code, "last_error_message": (verdict.message or "")[:2000] or None}

    def _move(self, occurrence: OccurrenceView, new_status: str | None, bucket: list[int], result: DispatchResult,
              changes: dict) -> None:
        if self.store.transition(occurrence.id, expected_status=OCCURRENCE_WAITING_DATA, new_status=new_status,
                                 changes=changes):
            bucket.append(occurrence.id)
        else:
            result.lost.append(occurrence.id)

    def _policy_from_store(self, schedule_id: int) -> str:
        schedule = self.store.get_schedule(schedule_id)
        # A deleted schedule cascades its occurrences away; this default only covers a race with that delete.
        return schedule.spec.freshness_policy if schedule is not None else FRESHNESS_IMPORT_REFRESH_AFTER_PERIOD_END
