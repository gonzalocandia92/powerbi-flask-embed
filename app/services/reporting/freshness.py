"""Is the Power BI data of an occurrence ready? (Phase 3, data freshness)

Pure: no Flask, no SQLAlchemy, no HTTP. Three pieces, each replaceable on its own:

* a ``RefreshHistorySource`` returns the most recent refreshes of a report's dataset (newest first) or raises
  ``RefreshLookupError`` saying whether the failure is permanent (credentials, missing report/dataset) or
  transient (timeouts, throttling, 5xx). Concrete sources live in ``freshness_sources``.
* a policy (``FRESHNESS_POLICIES``, keyed by the names in ``schedule_states``) decides from those records
  whether the occurrence may run. ``none`` (DirectQuery/Live) never waits; ``import_refresh_after_period_end``
  (default) needs a ``Completed`` refresh that ENDED at or after the end of the analysed period. A semantic
  watermark (e.g. a DAX measure) will be one more policy here.
* ``DataFreshnessService`` asks the sources IN ORDER (cheap persisted log first, live API last) and stops at
  the first one that makes the data fresh, so the API is only called when the log is not enough.

A ``Completed`` refresh does not prove every business record is loaded: that is what a watermark policy is for.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

from app.persistence.schedule_states import FRESHNESS_IMPORT_REFRESH_AFTER_PERIOD_END, FRESHNESS_NONE

from .period_resolver import ResolvedPeriod
from .schedule_rules import as_utc

FRESH = "fresh"
WAIT = "wait"
BLOCKED = "blocked"

# Power BI refresh statuses (``Unknown`` = still running).
PBI_COMPLETED = "Completed"
PBI_FAILED = "Failed"
PBI_IN_PROGRESS = "Unknown"

# Codes stored in ``last_error_code`` while waiting / when blocked.
CODE_NO_REFRESH_AFTER_PERIOD_END = "refresh_not_after_period_end"
CODE_REFRESH_IN_PROGRESS = "refresh_in_progress"
CODE_REFRESH_FAILED = "refresh_failed"
CODE_NO_REFRESH_HISTORY = "refresh_history_empty"
CODE_UNKNOWN_POLICY = "unknown_freshness_policy"
CODE_LOOKUP_FAILED = "refresh_lookup_failed"


@dataclass(frozen=True)
class RefreshRecord:
    status: str
    end_time: datetime | None = None
    start_time: datetime | None = None
    refresh_type: str | None = None
    source: str = ""

    def __post_init__(self) -> None:
        for name in ("end_time", "start_time"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, as_utc(value))


class RefreshLookupError(Exception):
    """The history could not be read. ``permanent``: retrying cannot help (the occurrence is blocked)."""

    def __init__(self, message: str, *, code: str = CODE_LOOKUP_FAILED, permanent: bool = False):
        super().__init__(message)
        self.code = code
        self.permanent = permanent


class RefreshHistorySource(Protocol):
    name: str

    def latest_refreshes(self, report_id: int, *, top: int) -> list[RefreshRecord]: ...


@dataclass(frozen=True)
class FreshnessContext:
    report_id: int
    period: ResolvedPeriod
    now: datetime


@dataclass(frozen=True)
class FreshnessVerdict:
    kind: str                                   # fresh | wait | blocked
    refresh_completed_at: datetime | None = None
    code: str | None = None
    message: str | None = None
    source: str | None = None                   # which source settled it (observability)


# ── policies ─────────────────────────────────────────────────────────────────────────────────

class FreshnessPolicy(Protocol):
    requires_history: bool

    def evaluate(self, context: FreshnessContext, records: Sequence[RefreshRecord]) -> FreshnessVerdict: ...


class NoFreshnessPolicy:
    """DirectQuery / Live connection: the data is read at query time, there is no refresh to wait for."""

    requires_history = False

    def evaluate(self, context: FreshnessContext, records: Sequence[RefreshRecord]) -> FreshnessVerdict:
        return FreshnessVerdict(FRESH)


class ImportRefreshAfterPeriodEnd:
    """Import datasets: a successful refresh that ended at or after the end of the period."""

    requires_history = True

    def evaluate(self, context: FreshnessContext, records: Sequence[RefreshRecord]) -> FreshnessVerdict:
        cutoff = context.period.end_exclusive
        valid = [r.end_time for r in records
                 if r.status == PBI_COMPLETED and r.end_time is not None and r.end_time >= cutoff]
        if valid:
            return FreshnessVerdict(FRESH, refresh_completed_at=max(valid))
        if not records:
            return FreshnessVerdict(WAIT, code=CODE_NO_REFRESH_HISTORY, message="Sin historial de refresh.")
        newest = records[0]
        if newest.status == PBI_IN_PROGRESS:
            return FreshnessVerdict(WAIT, code=CODE_REFRESH_IN_PROGRESS, message="Hay un refresh en curso.")
        if newest.status == PBI_FAILED:
            # Transient for us: Power BI (or the refresh monitor's auto-retry) may still succeed before the deadline.
            return FreshnessVerdict(WAIT, code=CODE_REFRESH_FAILED, message="El último refresh falló.")
        return FreshnessVerdict(WAIT, code=CODE_NO_REFRESH_AFTER_PERIOD_END,
                                message=f"Todavía no hay un refresh exitoso posterior a {cutoff.isoformat()}.")


FRESHNESS_POLICIES: dict[str, FreshnessPolicy] = {
    FRESHNESS_NONE: NoFreshnessPolicy(),
    FRESHNESS_IMPORT_REFRESH_AFTER_PERIOD_END: ImportRefreshAfterPeriodEnd(),
}


# ── service ──────────────────────────────────────────────────────────────────────────────────

class DataFreshnessService:
    def __init__(self, sources: Sequence[RefreshHistorySource], *,
                 policies: dict[str, FreshnessPolicy] | None = None, history_top: int = 5):
        self.sources = list(sources)
        self.policies = FRESHNESS_POLICIES if policies is None else policies
        self.history_top = history_top

    def check(self, policy_name: str, context: FreshnessContext) -> FreshnessVerdict:
        policy = self.policies.get(policy_name)
        if policy is None:
            return FreshnessVerdict(BLOCKED, code=CODE_UNKNOWN_POLICY,
                                    message=f"Política de frescura desconocida: {policy_name!r}")
        if not policy.requires_history:
            return policy.evaluate(context, [])
        verdict = FreshnessVerdict(WAIT, code=CODE_NO_REFRESH_HISTORY, message="Sin fuentes de historial.")
        for index, source in enumerate(self.sources):
            last = index == len(self.sources) - 1
            try:
                records = source.latest_refreshes(context.report_id, top=self.history_top)
            except Exception as exc:  # noqa: BLE001 - classified below; an unlabelled failure is transient
                if not last:
                    continue  # a cheaper source failing is not a verdict: ask the next one
                error = exc if isinstance(exc, RefreshLookupError) else RefreshLookupError(
                    f"{type(exc).__name__}: {exc}")
                return FreshnessVerdict(BLOCKED if error.permanent else WAIT, code=error.code,
                                        message=str(error)[:2000], source=source.name)
            verdict = policy.evaluate(context, records)
            verdict = FreshnessVerdict(verdict.kind, verdict.refresh_completed_at, verdict.code, verdict.message,
                                       source=source.name)
            if verdict.kind != WAIT:
                return verdict
        return verdict
