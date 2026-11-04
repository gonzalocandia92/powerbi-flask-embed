"""Occurrence finalizer (Phase 3): closes ``enqueued`` occurrences once their run has finished.

* run still open (queued/running/cancel_requested) -> nothing (no write);
* run ``completed`` / ``completed_with_errors`` (``REVIEWABLE_STATUSES``) -> submitted to human review as the
  SYSTEM actor (``ReviewActor.system()``: it may prepare and submit, never decide) -> ``submitted`` (the review is
  ``pending_review``; a degraded run shows its status to the reviewer, who approves or rejects it);
* run ``failed`` / ``cancelled`` -> ``run_failed`` (``last_error_code`` =
  ``run_<status>``). Never relaunched automatically: the analysis may already have spent tokens; only an
  explicit retry from the admin UI re-opens it.

Submitting renders the PDF, which can fail transiently (renderer down, storage): the occurrence stays
``enqueued`` with the reason in ``last_error_code`` and is retried every ``retry_minutes``, until
``submit_timeout_hours`` after the run completed; then ``run_failed`` / ``review_submission_failed`` (the run is
still there and a person can submit it from the review UI). A run that is not eligible for review is permanent.

No Flask, no SQLAlchemy: the store, the run reader and ``submit_review(run_id)`` are injected.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from app.persistence.run_states import OPEN_STATUSES, REVIEWABLE_STATUSES
from app.persistence.schedule_states import (
    ERROR_REVIEW_SUBMISSION_FAILED, ERROR_RUN_MISSING, ERROR_RUN_PREFIX, OCCURRENCE_ENQUEUED, OCCURRENCE_RUN_FAILED,
    OCCURRENCE_SUBMITTED,
)

from .review_contracts import ReviewNotEligibleError
from .schedule_contracts import OccurrenceView
from .schedule_rules import as_utc

LOG = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FinalizerSettings:
    retry_minutes: int = 5
    submit_timeout_hours: int = 24
    max_occurrences_per_tick: int = 50

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FinalizerSettings":
        env = os.environ if env is None else env

        def read(name: str, default: int) -> int:
            try:
                value = int(env.get(name) or default)
            except ValueError:
                return default
            return value if value >= 1 else default

        return cls(retry_minutes=read("REPORT_SCHEDULE_REVIEW_RETRY_MINUTES", 5),
                   submit_timeout_hours=read("REPORT_SCHEDULE_REVIEW_SUBMIT_TIMEOUT_HOURS", 24),
                   max_occurrences_per_tick=read("REPORT_SCHEDULER_MAX_OCCURRENCES_PER_TICK", 50))


@dataclass
class FinalizeResult:
    submitted: list[int] = field(default_factory=list)
    run_failed: list[int] = field(default_factory=list)
    retrying: list[int] = field(default_factory=list)
    lost: list[int] = field(default_factory=list)
    errors: list[int] = field(default_factory=list)


class OccurrenceFinalizer:
    def __init__(self, store, runs, submit_review: Callable[[str], object], *,
                 settings: FinalizerSettings | None = None, clock: Callable[[], datetime] | None = None):
        """``runs.latest_occurrence_run(occurrence_id) -> {id, status, error_code, error_message, completed_at}``."""
        self.store = store
        self.runs = runs
        self.submit_review = submit_review
        self.settings = settings or FinalizerSettings()
        self.clock = clock or _utcnow

    def tick(self, now: datetime | None = None) -> FinalizeResult:
        now = as_utc(now or self.clock())
        result = FinalizeResult()
        for occurrence in self.store.list_occurrences_in(
                [OCCURRENCE_ENQUEUED], check_due_at=now, limit=self.settings.max_occurrences_per_tick):
            try:
                self.finalize(occurrence, now, result)
            except Exception:  # noqa: BLE001 - one occurrence must not stop the others
                result.errors.append(occurrence.id)
                LOG.exception("[ReportScheduler] finalizing occurrence=%s failed", occurrence.id)
        if result.submitted or result.run_failed or result.errors:
            LOG.info("[ReportScheduler] finalize submitted=%s run_failed=%s retrying=%s errors=%s",
                     result.submitted, result.run_failed, result.retrying, result.errors)
        return result

    def finalize(self, occurrence: OccurrenceView, now: datetime, result: FinalizeResult) -> None:
        run = self.runs.latest_occurrence_run(occurrence.id)
        if run is None:
            self._move(occurrence, OCCURRENCE_RUN_FAILED, result.run_failed, result,
                       {"next_check_at": None, "last_error_code": ERROR_RUN_MISSING,
                        "last_error_message": "La ocurrencia no tiene una ejecución asociada."})
            return
        status = run["status"]
        if status in OPEN_STATUSES:
            return
        if status not in REVIEWABLE_STATUSES:
            self._move(occurrence, OCCURRENCE_RUN_FAILED, result.run_failed, result, {
                "next_check_at": None, "last_error_code": f"{ERROR_RUN_PREFIX}{status}",
                "last_error_message": " - ".join(filter(None, (run.get("error_code"), run.get("error_message"))))[:2000]
                or None})
            return
        try:
            self.submit_review(run["id"])
        except ReviewNotEligibleError as exc:
            self._move(occurrence, OCCURRENCE_RUN_FAILED, result.run_failed, result, {
                "next_check_at": None, "last_error_code": exc.code, "last_error_message": str(exc)[:2000]})
            return
        except Exception as exc:  # noqa: BLE001 - PDF/store/conflict: transient until the timeout
            code = getattr(exc, "code", None) or type(exc).__name__
            completed_at = run.get("completed_at")
            if completed_at is not None and now - as_utc(completed_at) >= timedelta(hours=self.settings.submit_timeout_hours):
                self._move(occurrence, OCCURRENCE_RUN_FAILED, result.run_failed, result, {
                    "next_check_at": None, "last_error_code": ERROR_REVIEW_SUBMISSION_FAILED,
                    "last_error_message": f"{code}: {exc}"[:2000]})
            else:
                LOG.warning("[ReportScheduler] review submission of run=%s failed (%s); retrying", run["id"], code)
                self._move(occurrence, None, result.retrying, result, {
                    "next_check_at": now + timedelta(minutes=self.settings.retry_minutes),
                    "last_error_code": code, "last_error_message": str(exc)[:2000] or None})
            return
        self._move(occurrence, OCCURRENCE_SUBMITTED, result.submitted, result,
                   {"next_check_at": None, "last_error_code": None, "last_error_message": None})

    def _move(self, occurrence: OccurrenceView, new_status: str | None, bucket: list[int], result: FinalizeResult,
              changes: dict) -> None:
        if self.store.transition(occurrence.id, expected_status=OCCURRENCE_ENQUEUED, new_status=new_status,
                                 changes=changes):
            bucket.append(occurrence.id)
        else:
            result.lost.append(occurrence.id)
