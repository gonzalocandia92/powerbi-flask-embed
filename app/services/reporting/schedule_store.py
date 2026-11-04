"""Persistence of report schedules and their occurrences (Phase 3).

The only module that talks SQLAlchemy about schedules. Callers exchange the values of
``schedule_contracts`` (never ORM rows), always get aware UTC datetimes back and may pass naive (UTC) or
aware ones in; columns hold naive UTC like the rest of the database.

Concurrency without long transactions, so several scheduler replicas (or a restart mid-tick) are safe:

* ``materialize_occurrence`` is idempotent: ``UNIQUE(schedule_id, scheduled_for)`` decides, and the loser
  of a race gets the existing row back.
* ``advance_schedule`` is a compare-and-swap on ``next_run_at``: only one caller moves a schedule forward.
* ``transition`` is ``UPDATE ... WHERE id = ? AND status = <expected>`` and only for transitions declared in
  ``schedule_states.TRANSITIONS``. Each method commits (or rolls back) its own work.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import AnalyticalReportDefinition, ReportSchedule, ReportScheduleOccurrence
from app.persistence.schedule_states import OCCURRENCE_WAITING_DATA, PENDING_OCCURRENCE_STATUSES, TRANSITIONS

from .schedule_contracts import (
    DEFINITION_NOT_FOUND, NewOccurrence, OccurrenceTransitionError, OccurrenceView, ScheduleNotFoundError,
    ScheduleSpec, ScheduleValidationError, ScheduleView,
)
from .schedule_rules import as_utc

MAX_PAGE_SIZE = 200

# Identity and frozen content of an occurrence: never rewritten (the snapshot is what it runs).
_OCCURRENCE_FROZEN = frozenset({
    "id", "schedule_id", "scheduled_for", "trigger_source", "status", "period_start", "period_end_exclusive",
    "definition_snapshot_json", "created_at", "updated_at",
})
_OCCURRENCE_UPDATABLE = frozenset(ReportScheduleOccurrence.__table__.columns.keys()) - _OCCURRENCE_FROZEN


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive(value: datetime | None) -> datetime | None:
    return None if value is None else as_utc(value).replace(tzinfo=None)


def _aware(value: datetime | None) -> datetime | None:
    return None if value is None else as_utc(value)


def _limit(value: int) -> int:
    return max(1, min(int(value), MAX_PAGE_SIZE))


def _spec_columns(spec: ScheduleSpec) -> dict[str, Any]:
    return {
        "name": spec.name, "frequency": spec.frequency, "weekday": spec.weekday,
        "day_of_month": spec.day_of_month, "local_time": spec.local_time, "timezone": spec.timezone,
        "freshness_policy": spec.freshness_policy, "refresh_deadline_hours": spec.refresh_deadline_hours,
        "misfire_policy": spec.misfire_policy, "misfire_grace_hours": spec.misfire_grace_hours,
        "config_json": dict(spec.config) or None,
    }


def to_schedule_view(row: ReportSchedule) -> ScheduleView:
    spec = ScheduleSpec(
        name=row.name, frequency=row.frequency, local_time=row.local_time, timezone=row.timezone,
        weekday=row.weekday, day_of_month=row.day_of_month, freshness_policy=row.freshness_policy,
        misfire_policy=row.misfire_policy, refresh_deadline_hours=row.refresh_deadline_hours,
        misfire_grace_hours=row.misfire_grace_hours, config=row.config_json or {})
    return ScheduleView(
        id=row.id, definition_id=row.definition_id, spec=spec, is_active=bool(row.is_active),
        next_run_at=_aware(row.next_run_at), created_by_user_id=row.created_by_user_id,
        created_at=_aware(row.created_at), updated_at=_aware(row.updated_at))


def to_occurrence_view(row: ReportScheduleOccurrence) -> OccurrenceView:
    return OccurrenceView(
        id=row.id, schedule_id=row.schedule_id, scheduled_for=_aware(row.scheduled_for),
        trigger_source=row.trigger_source, status=row.status, period_start=_aware(row.period_start),
        period_end_exclusive=_aware(row.period_end_exclusive), definition_snapshot=row.definition_snapshot_json,
        refresh_deadline_at=_aware(row.refresh_deadline_at), refresh_checked_at=_aware(row.refresh_checked_at),
        refresh_completed_at=_aware(row.refresh_completed_at), next_check_at=_aware(row.next_check_at),
        check_attempts=row.check_attempts or 0, last_error_code=row.last_error_code,
        last_error_message=row.last_error_message, created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at))


class ScheduleStore:
    # ── schedules ──────────────────────────────────────────────────────────────────────────────

    def create_schedule(self, definition_id: int, spec: ScheduleSpec, *, next_run_at: datetime | None,
                        created_by_user_id: int | None = None, is_active: bool = True) -> ScheduleView:
        if db.session.get(AnalyticalReportDefinition, definition_id) is None:
            raise ScheduleValidationError(f"Definition not found: {definition_id}", code=DEFINITION_NOT_FOUND)
        row = ReportSchedule(definition_id=definition_id, is_active=is_active,
                             next_run_at=_naive(next_run_at) if is_active else None,
                             created_by_user_id=created_by_user_id, **_spec_columns(spec))
        return self._commit_row(row)

    def update_schedule(self, schedule_id: int, spec: ScheduleSpec, *, next_run_at: datetime | None) -> ScheduleView:
        """Replace the configuration. ``next_run_at`` is ignored while the schedule is paused."""
        row = self._schedule_row(schedule_id)
        for name, value in _spec_columns(spec).items():
            setattr(row, name, value)
        row.next_run_at = _naive(next_run_at) if row.is_active else None
        return self._commit_row(row)

    def set_active(self, schedule_id: int, active: bool, *, next_run_at: datetime | None = None) -> ScheduleView:
        """Pause (``next_run_at`` -> NULL) or resume (``next_run_at`` computed by the caller from *now*)."""
        row = self._schedule_row(schedule_id)
        row.is_active = bool(active)
        row.next_run_at = _naive(next_run_at) if active else None
        return self._commit_row(row)

    def get_schedule(self, schedule_id: int) -> ScheduleView | None:
        row = db.session.execute(select(ReportSchedule).where(ReportSchedule.id == schedule_id)
                                 .execution_options(populate_existing=True)).scalar_one_or_none()
        return None if row is None else to_schedule_view(row)

    def list_schedules(self, *, definition_id: int | None = None) -> list[ScheduleView]:
        stmt = select(ReportSchedule).order_by(ReportSchedule.id).execution_options(populate_existing=True)
        if definition_id is not None:
            stmt = stmt.where(ReportSchedule.definition_id == definition_id)
        return [to_schedule_view(row) for row in db.session.execute(stmt).scalars()]

    def list_due_schedules(self, now: datetime, *, limit: int = 50) -> list[ScheduleView]:
        """Active schedules whose ``next_run_at`` has arrived, oldest first."""
        rows = db.session.execute(
            select(ReportSchedule)
            .where(ReportSchedule.is_active.is_(True), ReportSchedule.next_run_at.is_not(None),
                   ReportSchedule.next_run_at <= _naive(now))
            .order_by(ReportSchedule.next_run_at, ReportSchedule.id).limit(_limit(limit))
            .execution_options(populate_existing=True)).scalars()
        views = [to_schedule_view(row) for row in rows]
        db.session.rollback()  # end the read transaction: the next tick must see fresh data
        return views

    def advance_schedule(self, schedule_id: int, *, expected_next_run_at: datetime,
                         next_run_at: datetime) -> bool:
        """Compare-and-swap of ``next_run_at``. False: paused, or another caller already moved it."""
        return self._execute_one(
            update(ReportSchedule)
            .where(ReportSchedule.id == schedule_id, ReportSchedule.is_active.is_(True),
                   ReportSchedule.next_run_at == _naive(expected_next_run_at))
            .values(next_run_at=_naive(next_run_at), updated_at=_naive(_utcnow())))

    # ── occurrences ────────────────────────────────────────────────────────────────────────────

    def materialize_occurrence(self, new: NewOccurrence) -> tuple[OccurrenceView, bool]:
        """Insert the occurrence, or return the existing one for that slot. ``(view, created)``."""
        row = ReportScheduleOccurrence(
            schedule_id=new.schedule_id, scheduled_for=_naive(new.scheduled_for),
            trigger_source=new.trigger_source, status=new.status,
            period_start=_naive(new.period.start), period_end_exclusive=_naive(new.period.end_exclusive),
            definition_snapshot_json=new.definition_snapshot,
            refresh_deadline_at=_naive(new.refresh_deadline_at), next_check_at=_naive(new.next_check_at),
            check_attempts=0, last_error_code=new.last_error_code, last_error_message=new.last_error_message)
        try:
            db.session.add(row)
            db.session.commit()
            return to_occurrence_view(row), True
        except IntegrityError:
            db.session.rollback()
            existing = self.find_occurrence(new.schedule_id, new.scheduled_for)
            if existing is None:  # not a slot collision (e.g. the schedule is gone)
                raise
            return existing, False
        except Exception:
            db.session.rollback()
            raise

    def get_occurrence(self, occurrence_id: int) -> OccurrenceView | None:
        return self._one_occurrence(ReportScheduleOccurrence.id == occurrence_id)

    def find_occurrence(self, schedule_id: int, scheduled_for: datetime) -> OccurrenceView | None:
        return self._one_occurrence(ReportScheduleOccurrence.schedule_id == schedule_id,
                                    ReportScheduleOccurrence.scheduled_for == _naive(scheduled_for))

    def list_occurrences(self, schedule_id: int, *, limit: int = 50, offset: int = 0) -> list[OccurrenceView]:
        """History of a schedule, most recent first."""
        rows = db.session.execute(
            select(ReportScheduleOccurrence).where(ReportScheduleOccurrence.schedule_id == schedule_id)
            .order_by(ReportScheduleOccurrence.scheduled_for.desc(), ReportScheduleOccurrence.id.desc())
            .limit(_limit(limit)).offset(max(0, int(offset))).execution_options(populate_existing=True)).scalars()
        return [to_occurrence_view(row) for row in rows]

    def list_occurrences_in(self, statuses: Iterable[str], *, check_due_at: datetime | None = None,
                            limit: int = 50) -> list[OccurrenceView]:
        """Occurrences in ``statuses``, oldest first. With ``check_due_at``: only those whose
        ``next_check_at`` is NULL or has arrived (what the dispatcher polls)."""
        stmt = select(ReportScheduleOccurrence).where(ReportScheduleOccurrence.status.in_(list(statuses)))
        if check_due_at is not None:
            stmt = stmt.where(or_(ReportScheduleOccurrence.next_check_at.is_(None),
                                  ReportScheduleOccurrence.next_check_at <= _naive(check_due_at)))
        rows = db.session.execute(
            stmt.order_by(ReportScheduleOccurrence.scheduled_for, ReportScheduleOccurrence.id)
            .limit(_limit(limit)).execution_options(populate_existing=True)).scalars()
        views = [to_occurrence_view(row) for row in rows]
        db.session.rollback()
        return views

    def transition(self, occurrence_id: int, *, expected_status: str, new_status: str | None = None,
                   changes: Mapping[str, Any] | None = None) -> bool:
        """Guarded update. ``new_status=None`` updates fields only (e.g. the next freshness check) and is
        allowed only while the scheduler still owns the occurrence. False: the status was no longer
        ``expected_status`` (someone else moved it)."""
        if new_status is None:
            if expected_status not in PENDING_OCCURRENCE_STATUSES:
                raise OccurrenceTransitionError(f"Occurrence in {expected_status!r} is settled")
        elif new_status not in TRANSITIONS.get(expected_status, ()):
            raise OccurrenceTransitionError(f"{expected_status!r} -> {new_status!r} is not allowed")
        values = self._checked(changes or {})
        if new_status is not None:
            values["status"] = new_status
        values["updated_at"] = _naive(_utcnow())
        return self._execute_one(
            update(ReportScheduleOccurrence)
            .where(ReportScheduleOccurrence.id == occurrence_id, ReportScheduleOccurrence.status == expected_status)
            .values(**values))

    def reopen_occurrence(self, occurrence_id: int, *, expected_status: str, changes: Mapping[str, Any],
                          snapshot: dict | None = None) -> bool:
        """Explicit admin retry: ``expected_status`` (a retryable one) -> ``waiting_data``.

        ``snapshot`` may be given ONLY when the occurrence has none (it was blocked because its definition could
        not run): it is then frozen once; an existing snapshot is never replaced."""
        if OCCURRENCE_WAITING_DATA not in TRANSITIONS.get(expected_status, ()):
            raise OccurrenceTransitionError(f"{expected_status!r} cannot be reopened")
        values = self._checked(changes)
        if snapshot is not None:
            current = self.get_occurrence(occurrence_id)
            if current is None or current.definition_snapshot is not None:
                raise ValueError("Only an occurrence without a snapshot may receive one")
            values["definition_snapshot_json"] = snapshot
        values.update(status=OCCURRENCE_WAITING_DATA, updated_at=_naive(_utcnow()))
        return self._execute_one(
            update(ReportScheduleOccurrence)
            .where(ReportScheduleOccurrence.id == occurrence_id, ReportScheduleOccurrence.status == expected_status)
            .values(**values))

    def count_occurrences(self, schedule_id: int) -> dict[str, int]:
        """``{status: count}`` of a schedule's occurrences (observability)."""
        rows = db.session.execute(
            select(ReportScheduleOccurrence.status, func.count(ReportScheduleOccurrence.id))
            .where(ReportScheduleOccurrence.schedule_id == schedule_id)
            .group_by(ReportScheduleOccurrence.status)).all()
        return {status: int(count) for status, count in rows}

    # ── internals ──────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _checked(changes: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(changes) - _OCCURRENCE_UPDATABLE
        if unknown:
            raise ValueError(f"Not updatable occurrence columns: {sorted(unknown)}")
        return {name: _naive(value) if isinstance(value, datetime) else value for name, value in changes.items()}

    @staticmethod
    def _schedule_row(schedule_id: int) -> ReportSchedule:
        row = db.session.get(ReportSchedule, schedule_id, populate_existing=True)
        if row is None:
            raise ScheduleNotFoundError(f"Schedule not found: {schedule_id}")
        return row

    @staticmethod
    def _commit_row(row: ReportSchedule) -> ScheduleView:
        try:
            db.session.add(row)
            db.session.commit()
            return to_schedule_view(row)
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def _one_occurrence(*conditions) -> OccurrenceView | None:
        row = db.session.execute(select(ReportScheduleOccurrence).where(*conditions)
                                 .execution_options(populate_existing=True)).scalar_one_or_none()
        return None if row is None else to_occurrence_view(row)

    @staticmethod
    def _execute_one(statement) -> bool:
        try:
            changed = db.session.execute(statement).rowcount
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return changed == 1
