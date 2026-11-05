"""Analytical period of a scheduled occurrence (Phase 3): WHICH days a report covers.

Resolved ONCE, when the occurrence is materialised, and frozen in its snapshot: if Power BI refreshes a day
late, "last week" still means the days that were last week when the report was due.

* Intervals are ``[start, end_exclusive)``, built from local midnights in the schedule's timezone and kept
  as aware UTC datetimes; the inclusive local dates travel along for display and for the questions.
* weekly  -> the last COMPLETE week (``week_start`` .. +6 days) before the local week of ``scheduled_for``.
* monthly -> the last COMPLETE calendar month before the local month of ``scheduled_for``.
* ``previous_*`` is the period right before, of the same length/kind, for comparisons.

Pure: no Flask, no SQLAlchemy. A new kind of period is one entry in ``PERIOD_RULES``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .schedule_rules import MONTHLY, WEEKLY, ScheduleRule, as_utc, local_to_utc

PERIOD_PAYLOAD_VERSION = 1

KIND_WEEK = "week"
KIND_MONTH = "month"

_MONTHS_ES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
              "octubre", "noviembre", "diciembre")


@dataclass(frozen=True)
class ResolvedPeriod:
    kind: str
    timezone: str
    # Inclusive local dates (what a person reads).
    local_start: date
    local_end: date
    previous_local_start: date
    previous_local_end: date
    label: str
    comparison_label: str

    # ── UTC instants (what queries and freshness checks compare against) ──────────────────────
    @property
    def start(self) -> datetime:
        return _midnight_utc(self.local_start, self.timezone)

    @property
    def end_exclusive(self) -> datetime:
        return _midnight_utc(self.local_end + timedelta(days=1), self.timezone)

    @property
    def previous_start(self) -> datetime:
        return _midnight_utc(self.previous_local_start, self.timezone)

    @property
    def previous_end_exclusive(self) -> datetime:
        return _midnight_utc(self.previous_local_end + timedelta(days=1), self.timezone)

    # ── snapshot ──────────────────────────────────────────────────────────────────────────────
    def to_payload(self) -> dict[str, Any]:
        return {
            "version": PERIOD_PAYLOAD_VERSION, "kind": self.kind, "timezone": self.timezone,
            "local_start": self.local_start.isoformat(), "local_end": self.local_end.isoformat(),
            "previous_local_start": self.previous_local_start.isoformat(),
            "previous_local_end": self.previous_local_end.isoformat(),
            "start": self.start.isoformat(), "end_exclusive": self.end_exclusive.isoformat(),
            "previous_start": self.previous_start.isoformat(),
            "previous_end_exclusive": self.previous_end_exclusive.isoformat(),
            "label": self.label, "comparison_label": self.comparison_label,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ResolvedPeriod":
        version = payload.get("version") if isinstance(payload, dict) else None
        if version != PERIOD_PAYLOAD_VERSION:
            raise ValueError(f"Unsupported period payload version: {version!r}")
        # Local dates + timezone are the source of truth; the UTC copies in the payload are derived.
        return cls(
            kind=payload["kind"], timezone=payload["timezone"],
            local_start=date.fromisoformat(payload["local_start"]),
            local_end=date.fromisoformat(payload["local_end"]),
            previous_local_start=date.fromisoformat(payload["previous_local_start"]),
            previous_local_end=date.fromisoformat(payload["previous_local_end"]),
            label=payload["label"], comparison_label=payload["comparison_label"],
        )


def _midnight_utc(day: date, tz_name: str) -> datetime:
    return local_to_utc(datetime.combine(day, datetime.min.time()), ZoneInfo(tz_name))


def _fmt(day: date) -> str:
    return day.strftime("%d/%m/%Y")


# ── period rules: local due date -> (kind, start, end, previous start, previous end, labels) ──────

def _previous_week(due: date, week_start: int) -> tuple:
    current_week_start = due - timedelta(days=(due.weekday() - week_start) % 7)
    start = current_week_start - timedelta(days=7)
    end = current_week_start - timedelta(days=1)
    previous_start, previous_end = start - timedelta(days=7), start - timedelta(days=1)
    return (KIND_WEEK, start, end, previous_start, previous_end,
            f"Semana del {_fmt(start)} al {_fmt(end)}",
            f"semana del {_fmt(previous_start)} al {_fmt(previous_end)}")


def _month_before(first_of_month: date) -> tuple[date, date]:
    end = first_of_month - timedelta(days=1)
    return end.replace(day=1), end


def _month_label(day: date) -> str:
    return f"{_MONTHS_ES[day.month - 1]} {day.year}"


def _previous_month(due: date, week_start: int) -> tuple:
    start, end = _month_before(due.replace(day=1))
    previous_start, previous_end = _month_before(start)
    return (KIND_MONTH, start, end, previous_start, previous_end,
            _month_label(start).capitalize(), _month_label(previous_start))


PERIOD_RULES: dict[str, Callable[[date, int], tuple]] = {
    WEEKLY: _previous_week,
    MONTHLY: _previous_month,
}


class ReportingPeriodResolver:
    def __init__(self, *, week_start: int = 0):
        if not 0 <= week_start <= 6:
            raise ValueError("week_start must be 0-6 (0 = Monday)")
        self.week_start = week_start

    def resolve(self, rule: ScheduleRule, scheduled_for: datetime) -> ResolvedPeriod:
        due = as_utc(scheduled_for).astimezone(rule.tz).date()
        kind, start, end, previous_start, previous_end, label, comparison = (
            PERIOD_RULES[rule.frequency](due, self.week_start))
        return ResolvedPeriod(
            kind=kind, timezone=rule.timezone, local_start=start, local_end=end,
            previous_local_start=previous_start, previous_local_end=previous_end,
            label=label, comparison_label=comparison)
