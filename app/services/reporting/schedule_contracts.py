"""Values exchanged with the schedule store (Phase 3): inputs, read models and errors.

Pure: no Flask, no SQLAlchemy. The scheduler, the dispatcher and the web layer talk to ``ScheduleStore``
only through these types, so the store can be replaced (tests use the real one over SQLite) and no caller
ever holds an ORM row. Datetimes are aware UTC on the way out; naive values on the way in are UTC.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Mapping

from app.persistence.schedule_states import (
    DEFAULT_FRESHNESS_POLICY, DEFAULT_MISFIRE_POLICY, FRESHNESS_POLICIES, MISFIRE_POLICIES,
    OCCURRENCE_SOURCE_SCHEDULED, OCCURRENCE_SOURCES, OCCURRENCE_WAITING_DATA, OCCURRENCE_STATUSES,
)

from .period_resolver import ResolvedPeriod
from .schedule_rules import MONTHLY, WEEKLY, ScheduleRule, ScheduleRuleError

MAX_NAME_CHARS = 200
MAX_POLICY_HOURS = 24 * 31  # a deadline/grace window longer than a month is a misconfiguration

# Validation codes (stable: the web layer maps them to messages).
NAME_REQUIRED = "name_required"
NAME_TOO_LONG = "name_too_long"
INVALID_RULE = "invalid_rule"
UNKNOWN_FRESHNESS_POLICY = "unknown_freshness_policy"
UNKNOWN_MISFIRE_POLICY = "unknown_misfire_policy"
INVALID_HOURS = "invalid_hours"
INVALID_CONFIG = "invalid_config"
DEFINITION_NOT_FOUND = "definition_not_found"
UNKNOWN_OCCURRENCE_SOURCE = "unknown_occurrence_source"


class ScheduleError(Exception):
    code = "schedule_error"

    def __init__(self, message: str = "", *, code: str | None = None):
        super().__init__(message or self.code)
        if code is not None:
            self.code = code


class ScheduleValidationError(ScheduleError, ValueError):
    code = "schedule_invalid"


class ScheduleNotFoundError(ScheduleError, LookupError):
    code = "schedule_not_found"


class ScheduleConflictError(ScheduleError):
    """Valid request, but the state does not allow it (e.g. retrying an occurrence that is not settled)."""

    code = "schedule_conflict"


class OccurrenceTransitionError(ScheduleError, ValueError):
    """The requested status change is not declared in ``schedule_states.TRANSITIONS`` (a programming error)."""

    code = "occurrence_transition_not_allowed"


def _hours(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_POLICY_HOURS:
        raise ScheduleValidationError(f"{name} must be an integer between 1 and {MAX_POLICY_HOURS}",
                                      code=INVALID_HOURS)
    return value


@dataclass(frozen=True)
class ScheduleSpec:
    """What an administrator configures. Validated on construction; ``rule()`` gives the calendar rule."""

    name: str
    frequency: str
    local_time: time
    timezone: str
    weekday: int | None = None
    day_of_month: int | None = None
    freshness_policy: str = DEFAULT_FRESHNESS_POLICY
    misfire_policy: str = DEFAULT_MISFIRE_POLICY
    refresh_deadline_hours: int | None = None   # None = server default
    misfire_grace_hours: int | None = None      # None = server default
    config: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = self.name.strip() if isinstance(self.name, str) else ""
        if not name:
            raise ScheduleValidationError("name is required", code=NAME_REQUIRED)
        if len(name) > MAX_NAME_CHARS:
            raise ScheduleValidationError(f"name exceeds {MAX_NAME_CHARS} characters", code=NAME_TOO_LONG)
        object.__setattr__(self, "name", name)
        # Only the field of the chosen frequency may be set: no silently ignored values in the database.
        if (self.frequency == WEEKLY and self.day_of_month is not None) or (
                self.frequency == MONTHLY and self.weekday is not None):
            raise ScheduleValidationError("weekday is for weekly schedules, day_of_month for monthly ones",
                                          code=INVALID_RULE)
        try:
            self.rule()
        except ScheduleRuleError as exc:
            raise ScheduleValidationError(str(exc), code=INVALID_RULE) from exc
        if self.freshness_policy not in FRESHNESS_POLICIES:
            raise ScheduleValidationError(f"Unknown freshness policy: {self.freshness_policy!r}",
                                          code=UNKNOWN_FRESHNESS_POLICY)
        if self.misfire_policy not in MISFIRE_POLICIES:
            raise ScheduleValidationError(f"Unknown misfire policy: {self.misfire_policy!r}",
                                          code=UNKNOWN_MISFIRE_POLICY)
        _hours(self.refresh_deadline_hours, "refresh_deadline_hours")
        _hours(self.misfire_grace_hours, "misfire_grace_hours")
        if not isinstance(self.config, Mapping):
            raise ScheduleValidationError("config must be an object", code=INVALID_CONFIG)
        object.__setattr__(self, "config", dict(self.config))

    def rule(self) -> ScheduleRule:
        return ScheduleRule(frequency=self.frequency, local_time=self.local_time, timezone=self.timezone,
                            weekday=self.weekday, day_of_month=self.day_of_month)


@dataclass(frozen=True)
class ScheduleView:
    id: int
    definition_id: int
    spec: ScheduleSpec
    is_active: bool
    next_run_at: datetime | None
    created_by_user_id: int | None
    created_at: datetime
    updated_at: datetime

    @property
    def rule(self) -> ScheduleRule:
        return self.spec.rule()


@dataclass(frozen=True)
class NewOccurrence:
    """An occurrence to materialise. ``definition_snapshot`` is None only for a ``blocked`` one."""

    schedule_id: int
    scheduled_for: datetime
    period: ResolvedPeriod
    definition_snapshot: dict[str, Any] | None
    trigger_source: str = OCCURRENCE_SOURCE_SCHEDULED
    status: str = OCCURRENCE_WAITING_DATA
    refresh_deadline_at: datetime | None = None
    next_check_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None

    def __post_init__(self) -> None:
        if self.trigger_source not in OCCURRENCE_SOURCES:
            raise ScheduleValidationError(f"Unknown occurrence source: {self.trigger_source!r}",
                                          code=UNKNOWN_OCCURRENCE_SOURCE)
        if self.status not in OCCURRENCE_STATUSES:
            raise OccurrenceTransitionError(f"Unknown occurrence status: {self.status!r}")


@dataclass(frozen=True)
class OccurrenceView:
    id: int
    schedule_id: int
    scheduled_for: datetime
    trigger_source: str
    status: str
    period_start: datetime
    period_end_exclusive: datetime
    definition_snapshot: dict[str, Any] | None
    refresh_deadline_at: datetime | None
    refresh_checked_at: datetime | None
    refresh_completed_at: datetime | None
    next_check_at: datetime | None
    check_attempts: int
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    updated_at: datetime
