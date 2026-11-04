"""Administration of report schedules (Phase 3): what the admin UI may do, in one place.

Orchestration only, no Flask and no SQLAlchemy: the store and the ``ReportScheduler`` (calendar, period
resolver, snapshot builder, server defaults) are injected. The web layer parses requests into ``ScheduleSpec``
and calls this; the scheduler process never calls it.

* create / update compute ``next_run_at`` from NOW: never in the past, never a backlog;
* pause clears ``next_run_at``; resume recomputes it from now (missed slots while paused are not run);
* ``preview`` lists the next occurrences and the period each would analyse, without writing anything;
* ``run_now`` materialises a MANUAL occurrence (``scheduled_for = now``) that follows the same flow as a
  scheduled one (freshness check, caps, review), distinct from retrying an existing occurrence;
* ``retry`` re-opens a settled occurrence (expired / blocked / run_failed) with a fresh deadline. A
  ``run_failed`` retry creates a NEW run (and spends tokens again): the UI asks for confirmation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.persistence.schedule_states import (
    ERROR_DEFINITION_NOT_RUNNABLE, OCCURRENCE_SOURCE_MANUAL, RETRYABLE_OCCURRENCE_STATUSES,
)

from .period_resolver import ResolvedPeriod
from .schedule_contracts import (
    OccurrenceView, ScheduleConflictError, ScheduleNotFoundError, ScheduleSpec, ScheduleValidationError, ScheduleView,
)
from .schedule_rules import ScheduleRule, as_utc, upcoming_occurrences
from .scheduler import DefinitionUnavailable, ReportScheduler
from .snapshot import with_period

MAX_PREVIEW = 12
OCCURRENCE_NOT_FOUND = "occurrence_not_found"
OCCURRENCE_NOT_RETRYABLE = "occurrence_not_retryable"
OCCURRENCE_CHANGED = "occurrence_changed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PreviewItem:
    scheduled_for: datetime   # aware UTC
    local: datetime           # the same instant in the schedule's timezone
    period: ResolvedPeriod


class ScheduleService:
    def __init__(self, store, scheduler: ReportScheduler, *, clock: Callable[[], datetime] | None = None):
        self.store = store
        self.scheduler = scheduler
        self.clock = clock or _utcnow

    # ── configuration ──────────────────────────────────────────────────────────────────────────

    def create(self, definition_id: int, spec: ScheduleSpec, *, created_by_user_id: int | None,
               active: bool = True) -> ScheduleView:
        return self.store.create_schedule(definition_id, spec, created_by_user_id=created_by_user_id,
                                          is_active=active, next_run_at=self._first_run(spec.rule()))

    def update(self, schedule_id: int, spec: ScheduleSpec) -> ScheduleView:
        self._schedule(schedule_id)
        return self.store.update_schedule(schedule_id, spec, next_run_at=self._first_run(spec.rule()))

    def pause(self, schedule_id: int) -> ScheduleView:
        self._schedule(schedule_id)
        return self.store.set_active(schedule_id, False)

    def resume(self, schedule_id: int) -> ScheduleView:
        schedule = self._schedule(schedule_id)
        return self.store.set_active(schedule_id, True, next_run_at=self._first_run(schedule.rule))

    def get(self, schedule_id: int) -> ScheduleView:
        return self._schedule(schedule_id)

    # ── preview ────────────────────────────────────────────────────────────────────────────────

    def preview(self, rule: ScheduleRule, *, count: int = 5, after: datetime | None = None) -> list[PreviewItem]:
        after = as_utc(after or self.clock())
        resolver = self.scheduler.resolver
        return [PreviewItem(scheduled_for=when, local=when.astimezone(rule.tz), period=resolver.resolve(rule, when))
                for when in upcoming_occurrences(rule, after, max(1, min(count, MAX_PREVIEW)))]

    # ── occurrences ────────────────────────────────────────────────────────────────────────────

    def run_now(self, schedule_id: int) -> OccurrenceView:
        schedule = self._schedule(schedule_id)
        now = as_utc(self.clock())
        return self.scheduler.materialize(schedule, now, now=now, trigger_source=OCCURRENCE_SOURCE_MANUAL)

    def retry(self, occurrence_id: int) -> OccurrenceView:
        occurrence = self.store.get_occurrence(occurrence_id)
        if occurrence is None:
            raise ScheduleNotFoundError("La ocurrencia no existe.", code=OCCURRENCE_NOT_FOUND)
        if occurrence.status not in RETRYABLE_OCCURRENCE_STATUSES:
            raise ScheduleConflictError("Solo se puede reintentar una ocurrencia vencida, bloqueada o fallida.",
                                        code=OCCURRENCE_NOT_RETRYABLE)
        schedule = self._schedule(occurrence.schedule_id)
        snapshot = None
        if occurrence.definition_snapshot is None:  # blocked because the definition could not run: try again now
            period = self.scheduler.resolver.resolve(schedule.rule, occurrence.scheduled_for)
            try:
                snapshot = with_period(self.scheduler.snapshot_builder(schedule.definition_id), period)
            except DefinitionUnavailable as exc:
                raise ScheduleValidationError(str(exc), code=ERROR_DEFINITION_NOT_RUNNABLE) from exc
        now = as_utc(self.clock())
        hours = schedule.spec.refresh_deadline_hours or self.scheduler.settings.default_deadline_hours
        reopened = self.store.reopen_occurrence(occurrence_id, expected_status=occurrence.status, snapshot=snapshot,
                                                changes={
                                                    "refresh_deadline_at": now + timedelta(hours=hours),
                                                    "next_check_at": now, "check_attempts": 0,
                                                    "refresh_checked_at": None, "refresh_completed_at": None,
                                                    "last_error_code": None, "last_error_message": None})
        if not reopened:
            raise ScheduleConflictError("La ocurrencia cambió de estado; recargá e intentá de nuevo.",
                                        code=OCCURRENCE_CHANGED)
        return self.store.get_occurrence(occurrence_id)

    # ── internals ──────────────────────────────────────────────────────────────────────────────

    def _first_run(self, rule: ScheduleRule) -> datetime:
        return self.scheduler.first_run_after(rule, as_utc(self.clock()))

    def _schedule(self, schedule_id: int) -> ScheduleView:
        schedule = self.store.get_schedule(schedule_id)
        if schedule is None:
            raise ScheduleNotFoundError("La programación no existe.")
        return schedule
