"""Calendar side of the report scheduler (Phase 3): turns due schedules into occurrences.

``ReportScheduler.tick(now)`` is the only thing APScheduler triggers here; the database is the source of
truth. For every active schedule whose ``next_run_at`` has arrived:

1. compute the occurrences due since ``next_run_at`` (``schedule_rules``);
2. apply the misfire policy: at most ONE of them runs, the others are recorded as ``skipped`` (only the most
   recent ``max_skipped_recorded``: a long outage never creates an unlimited backlog);
3. freeze, for the one that runs, its analytical period (``period_resolver``) and the definition snapshot;
   an unrunnable definition gives a ``blocked`` occurrence instead;
4. move ``next_run_at`` to the first occurrence after ``now`` (compare-and-swap).

Materialising is idempotent and happens BEFORE advancing, so a crash in between, a second replica or two
overlapping ticks never duplicate an occurrence; at worst the next tick redoes step 4.

No Flask, no SQLAlchemy: the store, the snapshot builder, the resolver and the clock are injected.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from app.persistence.schedule_states import (
    ERROR_DEFINITION_NOT_RUNNABLE, ERROR_MISSED_BEYOND_GRACE, ERROR_SUPERSEDED_BY_LATER, MISFIRE_SKIP,
    OCCURRENCE_BLOCKED, OCCURRENCE_SKIPPED, OCCURRENCE_SOURCE_SCHEDULED, OCCURRENCE_WAITING_DATA,
)

from .period_resolver import ReportingPeriodResolver
from .schedule_contracts import NewOccurrence, OccurrenceView, ScheduleView
from .schedule_rules import ScheduleRule, as_utc, next_occurrence, occurrences_between
from .snapshot import with_period

LOG = logging.getLogger(__name__)

# Never look further back than this for missed occurrences, whatever ``next_run_at`` says.
MAX_LOOKBACK = timedelta(days=366)

SnapshotBuilder = Callable[[int], dict]


class DefinitionUnavailable(Exception):
    """Raised by a snapshot builder when the definition cannot run (missing, inactive, no questions)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _env_int(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    try:
        value = int(env.get(name) or default)
    except ValueError:
        return default
    return value if value >= minimum else default


@dataclass(frozen=True)
class SchedulerSettings:
    """Server defaults (a schedule may override deadline and grace). Values still "to be defined" by the
    business live here, configurable, never hard-coded in the logic."""

    default_deadline_hours: int = 6
    default_misfire_grace_hours: int = 24
    on_time_tolerance_minutes: int = 15     # ``skip`` policy: how late still counts as "on time"
    max_schedules_per_tick: int = 50
    max_skipped_recorded: int = 10

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SchedulerSettings":
        env = os.environ if env is None else env
        return cls(
            default_deadline_hours=_env_int(env, "REPORT_SCHEDULE_DEFAULT_DEADLINE_HOURS", 6, 1),
            default_misfire_grace_hours=_env_int(env, "REPORT_SCHEDULE_DEFAULT_MISFIRE_GRACE_HOURS", 24, 0),
            on_time_tolerance_minutes=_env_int(env, "REPORT_SCHEDULE_ON_TIME_TOLERANCE_MINUTES", 15, 0),
            max_schedules_per_tick=_env_int(env, "REPORT_SCHEDULER_MAX_SCHEDULES_PER_TICK", 50, 1),
            max_skipped_recorded=_env_int(env, "REPORT_SCHEDULER_MAX_SKIPPED_RECORDED", 10, 0),
        )


@dataclass
class TickResult:
    """What a tick did (logs, metrics and tests read this)."""

    materialized: list[int] = field(default_factory=list)   # occurrences created to run (waiting_data)
    blocked: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    unrecorded_skips: int = 0                                # missed beyond ``max_skipped_recorded``
    advanced: list[int] = field(default_factory=list)        # schedule ids moved forward by THIS caller
    errors: list[int] = field(default_factory=list)          # schedule ids that failed (retried next tick)


@dataclass(frozen=True)
class MisfirePlan:
    run: datetime | None
    skipped: list[tuple[datetime, str]]  # (scheduled_for, error code), most recent last


def plan_misfire(due: list[datetime], now: datetime, *, policy: str, grace: timedelta,
                 on_time_tolerance: timedelta) -> MisfirePlan:
    """Which of the ``due`` occurrences (ascending) runs, and why each other one is skipped. Pure."""
    window = on_time_tolerance if policy == MISFIRE_SKIP else grace
    if not due:
        return MisfirePlan(None, [])
    if now - due[-1] <= window:
        return MisfirePlan(due[-1], [(when, ERROR_SUPERSEDED_BY_LATER) for when in due[:-1]])
    # Even the most recent one is too late: all of them are beyond the window.
    return MisfirePlan(None, [(when, ERROR_MISSED_BEYOND_GRACE) for when in due])


class ReportScheduler:
    def __init__(self, store, snapshot_builder: SnapshotBuilder, *, resolver: ReportingPeriodResolver | None = None,
                 settings: SchedulerSettings | None = None, clock: Callable[[], datetime] | None = None):
        self.store = store
        self.snapshot_builder = snapshot_builder
        self.resolver = resolver or ReportingPeriodResolver()
        self.settings = settings or SchedulerSettings()
        self.clock = clock or _utcnow

    # ── calendar helpers (also used by the web layer to create/resume schedules) ───────────────

    @staticmethod
    def first_run_after(rule: ScheduleRule, now: datetime) -> datetime:
        """``next_run_at`` for a schedule created or resumed at ``now``: never in the past, never a backlog."""
        return next_occurrence(rule, now)

    # ── tick ───────────────────────────────────────────────────────────────────────────────────

    def tick(self, now: datetime | None = None) -> TickResult:
        now = as_utc(now or self.clock())
        result = TickResult()
        for schedule in self.store.list_due_schedules(now, limit=self.settings.max_schedules_per_tick):
            try:
                self.process_schedule(schedule, now, result)
            except Exception:  # noqa: BLE001 - one broken schedule must not stop the others
                result.errors.append(schedule.id)
                LOG.exception("[ReportScheduler] schedule=%s failed; it will be retried next tick", schedule.id)
        if result.materialized or result.blocked or result.skipped or result.errors:
            LOG.info("[ReportScheduler] tick materialized=%s blocked=%s skipped=%s(+%s unrecorded) errors=%s",
                     result.materialized, result.blocked, result.skipped, result.unrecorded_skips, result.errors)
        return result

    def process_schedule(self, schedule: ScheduleView, now: datetime, result: TickResult | None = None) -> TickResult:
        result = result if result is not None else TickResult()
        expected = schedule.next_run_at
        if expected is None or expected > now:
            return result
        rule, spec = schedule.rule, schedule.spec
        due = occurrences_between(rule, max(expected, now - MAX_LOOKBACK), now)
        hours = spec.misfire_grace_hours or self.settings.default_misfire_grace_hours
        plan = plan_misfire(due, now, policy=spec.misfire_policy, grace=timedelta(hours=hours),
                            on_time_tolerance=timedelta(minutes=self.settings.on_time_tolerance_minutes))

        recorded = plan.skipped[-self.settings.max_skipped_recorded:] if self.settings.max_skipped_recorded else []
        result.unrecorded_skips += len(plan.skipped) - len(recorded)
        for when, code in recorded:
            view = self._materialize_skipped(schedule, when, code)
            result.skipped.append(view.id)
        if plan.run is not None:
            view = self.materialize(schedule, plan.run, now=now)
            (result.blocked if view.status == OCCURRENCE_BLOCKED else result.materialized).append(view.id)

        if self.store.advance_schedule(schedule.id, expected_next_run_at=expected,
                                       next_run_at=next_occurrence(rule, now)):
            result.advanced.append(schedule.id)
        return result

    # ── materialisation ────────────────────────────────────────────────────────────────────────

    def materialize(self, schedule: ScheduleView, scheduled_for: datetime, *, now: datetime | None = None,
                    trigger_source: str = OCCURRENCE_SOURCE_SCHEDULED) -> OccurrenceView:
        """Create (or return the existing) occurrence for ``scheduled_for`` with its frozen period and snapshot.

        Also the entry point of a manual "run now" (``trigger_source=manual``, ``scheduled_for=now``)."""
        now = as_utc(now or self.clock())
        scheduled_for = as_utc(scheduled_for)
        period = self.resolver.resolve(schedule.rule, scheduled_for)
        try:
            snapshot = with_period(self.snapshot_builder(schedule.definition_id), period)
        except DefinitionUnavailable as exc:
            new = NewOccurrence(schedule_id=schedule.id, scheduled_for=scheduled_for, period=period,
                                definition_snapshot=None, trigger_source=trigger_source, status=OCCURRENCE_BLOCKED,
                                last_error_code=ERROR_DEFINITION_NOT_RUNNABLE, last_error_message=str(exc)[:2000])
        else:
            # A late occurrence (picked up within the grace window) still gets the whole waiting time.
            hours = schedule.spec.refresh_deadline_hours or self.settings.default_deadline_hours
            new = NewOccurrence(schedule_id=schedule.id, scheduled_for=scheduled_for, period=period,
                                definition_snapshot=snapshot, trigger_source=trigger_source,
                                status=OCCURRENCE_WAITING_DATA,
                                refresh_deadline_at=max(scheduled_for, now) + timedelta(hours=hours),
                                next_check_at=now)
        view, created = self.store.materialize_occurrence(new)
        if created:
            LOG.info("[ReportScheduler] occurrence=%s schedule=%s scheduled_for=%s status=%s period=%s",
                     view.id, schedule.id, scheduled_for.isoformat(), view.status, period.label)
        return view

    def _materialize_skipped(self, schedule: ScheduleView, scheduled_for: datetime, code: str) -> OccurrenceView:
        period = self.resolver.resolve(schedule.rule, scheduled_for)
        view, _ = self.store.materialize_occurrence(NewOccurrence(
            schedule_id=schedule.id, scheduled_for=scheduled_for, period=period, definition_snapshot=None,
            status=OCCURRENCE_SKIPPED, last_error_code=code))
        return view
