"""Single source of truth for the report-review vocabulary.

Pure constants: no imports, no logic. Models (``CHECK`` constraint), the database
triggers and the review service all read from here, so adding a status or a
transition is one edit in one place (plus a migration that regenerates the
constraint/trigger, which ``tests/test_report_review_models.py`` makes impossible
to forget).
"""

STATUS_DRAFT = "draft"                    # row exists, PDF not yet fixed: the preparation lock
STATUS_PENDING_REVIEW = "pending_review"  # PDF fixed, waiting for a staff decision
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

STATUSES = (STATUS_DRAFT, STATUS_PENDING_REVIEW, STATUS_APPROVED, STATUS_REJECTED)
DECIDED_STATUSES = (STATUS_APPROVED, STATUS_REJECTED)

# Allowed status changes. A status that maps to () is terminal.
TRANSITIONS = {
    STATUS_DRAFT: (STATUS_PENDING_REVIEW,),
    STATUS_PENDING_REVIEW: (STATUS_APPROVED, STATUS_REJECTED),
    STATUS_APPROVED: (),
    STATUS_REJECTED: (),
}

# Statuses in which an observation (comment) may be added. A decided review is read-only (consult mode)
# and a draft has not been shown to any reviewer yet.
COMMENTABLE_STATUSES = (STATUS_PENDING_REVIEW,)

# Where a review action comes from. Stored in the audit metadata; a new origin (e.g. an API client)
# is one more constant here, not a new parameter in the service.
SOURCE_MANUAL = "manual"      # a human staff user acting from the application
SOURCE_SCHEDULE = "schedule"  # the scheduler (Phase 3): no human actor
SOURCES = (SOURCE_MANUAL, SOURCE_SCHEDULE)

# Event types are intentionally NOT constrained in the database: a new kind of event must not
# need a migration. The review service validates against this tuple instead.
EVENT_SUBMITTED = "submitted"
EVENT_COMMENTED = "commented"
EVENT_APPROVED = "approved"
EVENT_REJECTED = "rejected"
EVENT_TYPES = (EVENT_SUBMITTED, EVENT_COMMENTED, EVENT_APPROVED, EVENT_REJECTED)
