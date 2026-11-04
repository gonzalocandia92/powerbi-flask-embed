"""Calendar rules of a report schedule (Phase 3): WHEN an occurrence is due.

Pure functions over values: no Flask, no SQLAlchemy, no clock. The scheduler asks "what is the next
occurrence after this instant?" and gets an aware UTC datetime; everything local (weekday, day of month,
time of day) is evaluated in the schedule's IANA timezone.

Daylight saving time, decided once here:

* a local time that does not exist (spring forward gap) is shifted forward by the gap (02:30 -> 03:30);
* a local time that happens twice (fall back) uses its FIRST instance (``fold=0``).

A new frequency is one entry in ``FREQUENCIES`` (a generator of candidate local dates); nothing else changes.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKLY = "weekly"
MONTHLY = "monthly"

# Guard against scanning forever (e.g. an absurd window): far above any real misfire window.
MAX_SCAN_OCCURRENCES = 10_000


class ScheduleRuleError(ValueError):
    """The rule is not valid (unknown frequency, timezone, weekday or day of month)."""


def as_utc(value: datetime) -> datetime:
    """Aware UTC datetime. Naive values are UTC (that is how the database stores them)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def local_to_utc(local: datetime, tz: ZoneInfo) -> datetime:
    """Naive local wall time -> aware UTC, applying the DST decisions of this module."""
    # fold=0: first instance of an ambiguous time; for a non-existent time zoneinfo uses the offset before
    # the transition, which lands exactly ``gap`` later on the wall clock (the shift-forward rule).
    return local.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)


@dataclass(frozen=True)
class ScheduleRule:
    frequency: str
    local_time: time
    timezone: str
    weekday: int | None = None        # weekly: 0 = Monday ... 6 = Sunday
    day_of_month: int | None = None   # monthly: 1-31; months without that day use their last day

    def __post_init__(self) -> None:
        if self.frequency not in FREQUENCIES:
            raise ScheduleRuleError(f"Unknown frequency: {self.frequency!r}")
        if not isinstance(self.local_time, time) or self.local_time.tzinfo is not None:
            raise ScheduleRuleError("local_time must be a naive datetime.time")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ScheduleRuleError(f"Unknown timezone: {self.timezone!r}") from exc
        if self.frequency == WEEKLY and not _int_in(self.weekday, 0, 6):
            raise ScheduleRuleError("weekly schedules need weekday 0-6 (0 = Monday)")
        if self.frequency == MONTHLY and not _int_in(self.day_of_month, 1, 31):
            raise ScheduleRuleError("monthly schedules need day_of_month 1-31")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _int_in(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


# ── frequencies: candidate local dates, ascending, starting at ``first`` (inclusive) ──────────────

def _weekly_dates(rule: ScheduleRule, first: date) -> Iterator[date]:
    current = first + timedelta(days=(rule.weekday - first.weekday()) % 7)
    while True:
        yield current
        current += timedelta(days=7)


def _monthly_dates(rule: ScheduleRule, first: date) -> Iterator[date]:
    year, month = first.year, first.month
    while True:
        day = min(rule.day_of_month, calendar.monthrange(year, month)[1])
        candidate = date(year, month, day)
        if candidate >= first:
            yield candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


FREQUENCIES: dict[str, Callable[[ScheduleRule, date], Iterator[date]]] = {
    WEEKLY: _weekly_dates,
    MONTHLY: _monthly_dates,
}


# ── queries ──────────────────────────────────────────────────────────────────────────────────

def _instants_from(rule: ScheduleRule, start: datetime) -> Iterator[datetime]:
    """Every occurrence (aware UTC) at or after ``start``, ascending."""
    start = as_utc(start)
    tz = rule.tz
    # One day earlier: the local date of ``start`` may still hold an occurrence later in the UTC day.
    first = start.astimezone(tz).date() - timedelta(days=1)
    for local_date in FREQUENCIES[rule.frequency](rule, first):
        instant = local_to_utc(datetime.combine(local_date, rule.local_time), tz)
        if instant >= start:
            yield instant


def next_occurrence(rule: ScheduleRule, after: datetime) -> datetime:
    """First occurrence strictly AFTER ``after`` (aware UTC)."""
    after = as_utc(after)
    for instant in _instants_from(rule, after):
        if instant > after:
            return instant
    raise AssertionError("unreachable: frequencies are infinite")  # pragma: no cover


def occurrences_between(rule: ScheduleRule, start: datetime, end: datetime) -> list[datetime]:
    """Occurrences with ``start <= t <= end`` (aware UTC, ascending).

    Raises ``ScheduleRuleError`` if the window holds more than ``MAX_SCAN_OCCURRENCES``: callers bound the
    window (misfire grace) instead of materialising an unlimited backlog.
    """
    start, end = as_utc(start), as_utc(end)
    found: list[datetime] = []
    if end < start:
        return found
    for instant in _instants_from(rule, start):
        if instant > end:
            break
        found.append(instant)
        if len(found) > MAX_SCAN_OCCURRENCES:
            raise ScheduleRuleError("Window too large: bound it before asking for its occurrences")
    return found


def upcoming_occurrences(rule: ScheduleRule, after: datetime, count: int) -> list[datetime]:
    """The next ``count`` occurrences strictly after ``after`` (for previews in the admin UI)."""
    found: list[datetime] = []
    current = as_utc(after)
    for _ in range(max(0, count)):
        current = next_occurrence(rule, current)
        found.append(current)
    return found
