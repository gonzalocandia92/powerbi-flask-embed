"""Liveness of the ``report-scheduler`` process (Phase 3, stage 7), so the admin UI can warn when it is down.

The process records a beat at the start and at the end of every poll (``HeartbeatRecorder``, through a store
port); the admin API asks ``assess_health`` whether the most recent beat is recent enough. Staleness is judged
against the poll interval the process itself recorded (``missed_polls`` of them, never less than
``min_stale_seconds``), so the web never needs the scheduler's settings. A poll whose phases failed is
``degraded``: the process is alive but not doing its job.

No Flask, no SQLAlchemy: the store and the clock are injected. Recording never breaks a poll.
"""
from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .schedule_rules import as_utc

LOG = logging.getLogger(__name__)

PROCESS_REPORT_SCHEDULER = "report-scheduler"

# Outcome of the last finished poll (``last_outcome``).
OUTCOME_OK = "ok"
OUTCOME_DEGRADED = "degraded"   # at least one phase raised

# What the admin UI is told. Plain strings: a new one is one more value (and one more row in the JS table).
HEALTH_OK = "ok"
HEALTH_DEGRADED = "degraded"        # alive, but its last poll had failing phases
HEALTH_STALE = "stale"              # no beat for longer than allowed: the process is probably down
HEALTH_NEVER_SEEN = "never_seen"    # no beat at all: the process never ran against this database
HEALTH_UNKNOWN = "unknown"          # the heartbeat could not be read
HEALTH_STATUSES = (HEALTH_OK, HEALTH_DEGRADED, HEALTH_STALE, HEALTH_NEVER_SEEN, HEALTH_UNKNOWN)

DEFAULT_RETENTION = timedelta(days=7)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProcessIdentity:
    process: str
    instance_id: str
    host: str | None
    pid: int | None
    started_at: datetime

    @classmethod
    def current(cls, process: str, *, started_at: datetime | None = None) -> "ProcessIdentity":
        host, pid = socket.gethostname() or None, os.getpid()
        return cls(process=process, instance_id=f"{process}:{host}:{pid}"[:200], host=host, pid=pid,
                   started_at=as_utc(started_at or _utcnow()))


@dataclass(frozen=True)
class Beat:
    identity: ProcessIdentity
    seen_at: datetime
    interval_seconds: int | None
    outcome: str | None = None                       # None: a start-of-poll beat, keeps the previous outcome
    details: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class HeartbeatView:
    instance_id: str
    process: str
    host: str | None
    pid: int | None
    started_at: datetime
    last_seen_at: datetime
    interval_seconds: int | None
    last_outcome: str | None
    details: Mapping[str, Any] = field(default_factory=dict)


def _env_int(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    try:
        value = int(env.get(name) or default)
    except ValueError:
        return default
    return value if value >= minimum else default


@dataclass(frozen=True)
class HealthSettings:
    missed_polls: int = 3
    min_stale_seconds: int = 300   # a poll that renders PDFs may take minutes: do not flap

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "HealthSettings":
        env = os.environ if env is None else env
        return cls(missed_polls=_env_int(env, "REPORT_SCHEDULER_HEARTBEAT_MISSED_POLLS", 3, 1),
                   min_stale_seconds=_env_int(env, "REPORT_SCHEDULER_HEARTBEAT_STALE_SECONDS", 300, 1))

    def stale_after(self, interval_seconds: int | None) -> timedelta:
        return timedelta(seconds=max(self.min_stale_seconds, self.missed_polls * max(0, interval_seconds or 0)))


@dataclass(frozen=True)
class SchedulerHealth:
    status: str
    last_seen_at: datetime | None = None
    age_seconds: int | None = None
    stale_after_seconds: int | None = None
    failed_phases: tuple[str, ...] = ()
    host: str | None = None

    @property
    def stale(self) -> bool:
        return self.status in (HEALTH_STALE, HEALTH_NEVER_SEEN)


def assess_health(latest: HeartbeatView | None, now: datetime, settings: HealthSettings | None = None) -> SchedulerHealth:
    """Health of a process from its most recent beat (any instance). Pure."""
    settings = settings or HealthSettings()
    if latest is None:
        return SchedulerHealth(status=HEALTH_NEVER_SEEN)
    now, seen = as_utc(now), as_utc(latest.last_seen_at)
    limit = settings.stale_after(latest.interval_seconds)
    age = max(timedelta(0), now - seen)
    failed = tuple(str(name) for name in (latest.details or {}).get("failed_phases") or ())
    if age > limit:
        status = HEALTH_STALE
    elif latest.last_outcome == OUTCOME_DEGRADED:
        status = HEALTH_DEGRADED
    else:
        status = HEALTH_OK
    return SchedulerHealth(status=status, last_seen_at=seen, age_seconds=int(age.total_seconds()),
                           stale_after_seconds=int(limit.total_seconds()), failed_phases=failed, host=latest.host)


def poll_outcome(outcome: Mapping[str, object]) -> tuple[str, dict[str, Any]]:
    """``poll_report_schedules`` result (``{phase: result | exception}``) -> (outcome, details)."""
    failed = [name for name, value in outcome.items() if isinstance(value, BaseException)]
    return (OUTCOME_DEGRADED if failed else OUTCOME_OK), {"phases": list(outcome), "failed_phases": failed}


class HeartbeatRecorder:
    """What a poll calls. ``store``: ``record(Beat)`` and ``prune(process, older_than, keep_instance_id)``."""

    def __init__(self, store, identity: ProcessIdentity, *, interval_seconds: int | None,
                 clock: Callable[[], datetime] | None = None, retention: timedelta = DEFAULT_RETENTION):
        self.store = store
        self.identity = identity
        self.interval_seconds = interval_seconds
        self.clock = clock or _utcnow
        self.retention = retention
        self._warned = False

    def beat(self, outcome: Mapping[str, object] | None = None) -> bool:
        now = as_utc(self.clock())
        kind, details = poll_outcome(outcome) if outcome is not None else (None, None)
        try:
            self.store.record(Beat(identity=self.identity, seen_at=now, interval_seconds=self.interval_seconds,
                                   outcome=kind, details=details))
            if outcome is not None:  # old instances (restarts, replaced containers) are not kept forever
                self.store.prune(self.identity.process, older_than=now - self.retention,
                                 keep_instance_id=self.identity.instance_id)
        except Exception:  # noqa: BLE001 - liveness reporting must never stop the scheduler itself
            if not self._warned:
                LOG.warning("[ReportScheduler] heartbeat could not be recorded", exc_info=True)
                self._warned = True
            return False
        self._warned = False
        return True
