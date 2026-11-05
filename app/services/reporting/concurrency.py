"""Concurrency caps for scheduled runs (Phase 3).

Pure. The dispatcher reads the open scheduled runs once per poll (``{report_id: count}``), asks the caps
before each enqueue and counts what it enqueues itself. A ``ready`` occurrence that does not fit simply stays
``ready`` for the next poll (nothing is skipped or lost).

A new limit (per company, per dataset, a time window...) is one more ``ConcurrencyLimit``; the dispatcher does
not change. ``0`` disables a limit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence


@dataclass
class RunUsage:
    by_report: dict[int, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.by_report.values())

    def add(self, report_id: int) -> None:
        self.by_report[report_id] = self.by_report.get(report_id, 0) + 1


class ConcurrencyLimit(Protocol):
    def admits(self, report_id: int, usage: RunUsage) -> bool: ...


@dataclass(frozen=True)
class GlobalLimit:
    max_active: int

    def admits(self, report_id: int, usage: RunUsage) -> bool:
        return self.max_active <= 0 or usage.total < self.max_active


@dataclass(frozen=True)
class PerReportLimit:
    max_active: int

    def admits(self, report_id: int, usage: RunUsage) -> bool:
        return self.max_active <= 0 or usage.by_report.get(report_id, 0) < self.max_active


class ConcurrencyCaps:
    def __init__(self, limits: Sequence[ConcurrencyLimit] = ()):
        self.limits = list(limits)

    def admits(self, report_id: int, usage: RunUsage) -> bool:
        return all(limit.admits(report_id, usage) for limit in self.limits)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ConcurrencyCaps":
        env = os.environ if env is None else env

        def read(name: str, default: int) -> int:
            try:
                return max(0, int(env.get(name) or default))
            except ValueError:
                return default

        return cls([GlobalLimit(read("REPORT_SCHEDULER_MAX_ACTIVE_RUNS", 2)),
                    PerReportLimit(read("REPORT_SCHEDULER_MAX_ACTIVE_RUNS_PER_REPORT", 1))])
