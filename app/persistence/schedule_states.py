"""Single source of truth for the report-schedule vocabulary (Phase 3).

Pure constants: no imports, no logic. Deliberately NOT enforced by ``CHECK`` constraints: new statuses,
policies and error codes are expected, and must not need a migration. ``ScheduleStore`` validates every
transition against ``TRANSITIONS``; ``tests/test_report_schedule_store.py`` fails if a status is used
without being declared here.

Lifecycle of an occurrence::

    waiting_data --fresh--> ready --enqueue--> enqueued --run completed--> submitted (pending_review)
         |  \\--deadline--> expired               |      \\--other terminal run status--> run_failed
         \\--permanent error--> blocked            \\--(concurrency caps full: stays ready)
    skipped: missed beyond the misfire window, or the schedule was paused
    expired | blocked | run_failed --explicit admin retry--> waiting_data
"""

# ── occurrence status ────────────────────────────────────────────────────────────────────────
OCCURRENCE_WAITING_DATA = "waiting_data"
OCCURRENCE_READY = "ready"
OCCURRENCE_ENQUEUED = "enqueued"
OCCURRENCE_SUBMITTED = "submitted"
OCCURRENCE_RUN_FAILED = "run_failed"
OCCURRENCE_EXPIRED = "expired"
OCCURRENCE_BLOCKED = "blocked"
OCCURRENCE_SKIPPED = "skipped"

OCCURRENCE_STATUSES = (
    OCCURRENCE_WAITING_DATA, OCCURRENCE_READY, OCCURRENCE_ENQUEUED, OCCURRENCE_SUBMITTED, OCCURRENCE_RUN_FAILED,
    OCCURRENCE_EXPIRED, OCCURRENCE_BLOCKED, OCCURRENCE_SKIPPED,
)

TRANSITIONS = {
    OCCURRENCE_WAITING_DATA: (OCCURRENCE_READY, OCCURRENCE_EXPIRED, OCCURRENCE_BLOCKED, OCCURRENCE_SKIPPED),
    OCCURRENCE_READY: (OCCURRENCE_ENQUEUED, OCCURRENCE_BLOCKED, OCCURRENCE_SKIPPED),
    OCCURRENCE_ENQUEUED: (OCCURRENCE_SUBMITTED, OCCURRENCE_RUN_FAILED),
    OCCURRENCE_SUBMITTED: (),
    OCCURRENCE_SKIPPED: (),
    # Never automatic: only an explicit retry from the admin UI re-opens these.
    OCCURRENCE_RUN_FAILED: (OCCURRENCE_WAITING_DATA,),
    OCCURRENCE_EXPIRED: (OCCURRENCE_WAITING_DATA,),
    OCCURRENCE_BLOCKED: (OCCURRENCE_WAITING_DATA,),
}

# Statuses the scheduler still acts on by itself.
PENDING_OCCURRENCE_STATUSES = (OCCURRENCE_WAITING_DATA, OCCURRENCE_READY, OCCURRENCE_ENQUEUED)
# Finished for the scheduler (some of them may be retried by hand, see ``RETRYABLE_OCCURRENCE_STATUSES``).
SETTLED_OCCURRENCE_STATUSES = tuple(s for s in OCCURRENCE_STATUSES if s not in PENDING_OCCURRENCE_STATUSES)
RETRYABLE_OCCURRENCE_STATUSES = (OCCURRENCE_RUN_FAILED, OCCURRENCE_EXPIRED, OCCURRENCE_BLOCKED)

# ── where an occurrence comes from ───────────────────────────────────────────────────────────
OCCURRENCE_SOURCE_SCHEDULED = "scheduled"  # due by the calendar
OCCURRENCE_SOURCE_MANUAL = "manual"        # "run now" from the admin UI (same flow, scheduled_for = now)
OCCURRENCE_SOURCES = (OCCURRENCE_SOURCE_SCHEDULED, OCCURRENCE_SOURCE_MANUAL)

# ── schedule policies (names only: the behaviour lives in the services) ──────────────────────
MISFIRE_LATEST_WITHIN_GRACE = "latest_within_grace"  # run the most recent due one if within the grace window
MISFIRE_SKIP = "skip"                                # run only if picked up on time (small tolerance); else skip
MISFIRE_POLICIES = (MISFIRE_LATEST_WITHIN_GRACE, MISFIRE_SKIP)
DEFAULT_MISFIRE_POLICY = MISFIRE_LATEST_WITHIN_GRACE

FRESHNESS_NONE = "none"                                             # DirectQuery / Live: no refresh to wait for
FRESHNESS_IMPORT_REFRESH_AFTER_PERIOD_END = "import_refresh_after_period_end"
FRESHNESS_POLICIES = (FRESHNESS_NONE, FRESHNESS_IMPORT_REFRESH_AFTER_PERIOD_END)
DEFAULT_FRESHNESS_POLICY = FRESHNESS_IMPORT_REFRESH_AFTER_PERIOD_END

# ── error codes stored in ``last_error_code`` (not constrained in the database) ───────────────
ERROR_DEFINITION_NOT_RUNNABLE = "definition_not_runnable"
ERROR_MISSED_BEYOND_GRACE = "missed_beyond_grace"
ERROR_SUPERSEDED_BY_LATER = "superseded_by_later_occurrence"
ERROR_REFRESH_DEADLINE_EXCEEDED = "refresh_deadline_exceeded"
ERROR_SNAPSHOT_UNREADABLE = "snapshot_unreadable"
ERROR_RUN_MISSING = "run_missing"
ERROR_REVIEW_SUBMISSION_FAILED = "review_submission_failed"   # gave up retrying; a human may still submit
ERROR_RUN_PREFIX = "run_"                                     # + the run's terminal status, e.g. run_failed
