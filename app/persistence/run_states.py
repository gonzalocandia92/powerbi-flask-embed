"""Single source of truth for the ``report_runs.status`` vocabulary.

Pure constants: no imports, no logic. ``run_store`` re-exports them (its public names did not change);
the model reads them to build the partial index that allows one open run per schedule occurrence.

    queued -> running -> completed | completed_with_errors | failed
    queued | running -> cancel_requested -> cancelled
"""

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_COMPLETED_WITH_ERRORS = "completed_with_errors"
STATUS_FAILED = "failed"
STATUS_CANCEL_REQUESTED = "cancel_requested"
STATUS_CANCELLED = "cancelled"

STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED, STATUS_COMPLETED_WITH_ERRORS, STATUS_FAILED,
            STATUS_CANCEL_REQUESTED, STATUS_CANCELLED)
# Owned by a worker (lease/heartbeat apply).
ACTIVE_STATUSES = (STATUS_RUNNING, STATUS_CANCEL_REQUESTED)
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_COMPLETED_WITH_ERRORS, STATUS_FAILED, STATUS_CANCELLED)
# Not finished yet: may still spend tokens.
OPEN_STATUSES = (STATUS_QUEUED,) + ACTIVE_STATUSES
# Finished with a report a person can review (and approve or reject). A degraded run is reviewable: the
# reviewer sees its status and decides; a run without a persisted report is still blocked by eligibility.
REVIEWABLE_STATUSES = (STATUS_COMPLETED, STATUS_COMPLETED_WITH_ERRORS)

# Who created a run. A run created by the scheduler flow (including "run now" from a schedule) is
# ``scheduled`` and points at its occurrence; everything else is ``manual``.
TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULED = "scheduled"
TRIGGER_SOURCES = (TRIGGER_MANUAL, TRIGGER_SCHEDULED)
