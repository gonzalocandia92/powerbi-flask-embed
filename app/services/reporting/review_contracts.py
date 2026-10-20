"""Vocabulary and ports of the report-review workflow.

Pure module: no Flask, SQLAlchemy or model imports. ``ReviewService`` and everything that reacts to it
(HTTP layer, scheduler, notifications) depend on these names and on the stable ``code`` of every error,
never on how reviews are stored or how PDFs are produced. The only collaborators of the service are the
``Protocol`` ports below, so each can be replaced (or faked in a test) on its own:

    ReviewStore      reviews, events and their atomic transitions      (``review_store.SqlReviewStore``)
    ReviewArtifacts  persisted FinalReport / HTML / PDF                (``ArtifactService`` satisfies it as is)
    EligibilityRule  one reason a run cannot be reviewed               (``review_eligibility``)
    ReviewPolicy     who may do what                                   (``review_policy``)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from app.persistence.review_states import SOURCE_MANUAL, SOURCE_SCHEDULE

from .artifacts import ArtifactRecord
from .pdf_artifacts import LoadedPdf, PdfArtifactError, SavedPdf

# ── actors ────────────────────────────────────────────────────────────────────────────────────

KIND_USER = "user"      # a person acting from the application
KIND_SYSTEM = "system"  # an automated process (the Phase 3 scheduler); never has a user row


@dataclass(frozen=True)
class ReviewActor:
    """Who performs an action, as a value (never an ORM ``User``), so the service stays web/ORM free.

    The audit trail stores ``snapshot()``: it survives the user being deleted.
    """

    kind: str
    id: int | None
    username: str | None = None
    email: str | None = None
    is_staff: bool = False
    is_active: bool = True

    @classmethod
    def from_user(cls, user: Any) -> "ReviewActor":
        """Build from any user-like object (Flask-Login ``current_user``, an ORM ``User``) by duck typing."""
        active = bool(getattr(user, "is_active", False)) and bool(getattr(user, "is_authenticated", True))
        return cls(kind=KIND_USER, id=getattr(user, "id", None), username=getattr(user, "username", None),
                   email=getattr(user, "email", None), is_staff=bool(getattr(user, "is_staff", False)),
                   is_active=active)

    @classmethod
    def system(cls, label: str = "scheduler") -> "ReviewActor":
        return cls(kind=KIND_SYSTEM, id=None, username=label)

    @property
    def source(self) -> str:
        return SOURCE_SCHEDULE if self.kind == KIND_SYSTEM else SOURCE_MANUAL

    def snapshot(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "username": self.username, "email": self.email}


# Actions a policy is asked about. A new capability = a constant here + a branch in a policy.
ACTION_PREPARE = "prepare"
ACTION_SUBMIT = "submit"
ACTION_DECIDE = "decide"
ACTION_COMMENT = "comment"


class ReviewPolicy(Protocol):
    def allows(self, actor: ReviewActor, action: str) -> bool: ...


# ── read / write models ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunView:
    """The few columns of a ``ReportRun`` the review workflow cares about (no sections, no BLOBs)."""

    id: str
    status: str
    report_id: int | None = None
    requested_by_user_id: int | None = None


@dataclass(frozen=True)
class ReviewView:
    id: int
    report_run_id: str
    round: int
    status: str
    pdf_artifact_id: int | None = None
    pdf_sha256: str | None = None
    final_report_artifact_id: int | None = None
    html_artifact_id: int | None = None
    submitted_by_user_id: int | None = None
    submitted_at: datetime | None = None
    decided_by_user_id: int | None = None
    decided_at: datetime | None = None
    rejection_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def has_pdf(self) -> bool:
        return self.pdf_artifact_id is not None and self.pdf_sha256 is not None


@dataclass(frozen=True)
class ReviewEventView:
    id: int
    review_id: int
    event_type: str
    actor_user_id: int | None = None
    previous_status: str | None = None
    new_status: str | None = None
    comment: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(frozen=True)
class NewEvent:
    """An audit event to append; written in the SAME transaction as the change that caused it."""

    event_type: str
    actor_user_id: int | None
    previous_status: str | None
    new_status: str | None
    comment: str | None
    metadata: Mapping[str, Any]


MAX_PAGE_SIZE = 100  # the one cap every listing adapter and the HTTP layer apply to ``ReviewQuery.limit``


@dataclass(frozen=True)
class ReviewQuery:
    """Filters of a review listing (the inbox). Every field is optional; ``limit`` is capped at ``MAX_PAGE_SIZE``."""

    statuses: Sequence[str] = ()
    report_id: int | None = None
    submitted_from: datetime | None = None
    submitted_to: datetime | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True)
class RunInfo:
    """What a review screen shows about the run under review (descriptive columns only, never BLOBs)."""

    run_id: str
    status: str | None = None
    name: str | None = None
    report_id: int | None = None
    report_name: str | None = None
    created_at: datetime | None = None
    requested_by: str | None = None


@dataclass(frozen=True)
class InboxItem:
    """One row of the review inbox: the review plus the descriptive columns of its run (never BLOBs)."""

    review: ReviewView
    run_status: str | None = None
    run_name: str | None = None
    report_id: int | None = None
    report_name: str | None = None
    run_created_at: datetime | None = None
    requested_by: str | None = None
    submitted_by: str | None = None
    decided_by: str | None = None


@dataclass(frozen=True)
class InboxPage:
    items: Sequence[InboxItem]
    total: int
    limit: int
    offset: int


# Outcomes of ``ReviewStore.claim_preparation``.
CLAIM_OWNED = "owned"      # the caller holds the preparation lease and must do the work
CLAIM_BUSY = "busy"        # another preparer holds a live lease: do not render a second PDF
CLAIM_SETTLED = "settled"  # the review already left ``draft``: nothing to prepare


@dataclass(frozen=True)
class PreparationClaim:
    review: ReviewView
    outcome: str


# ── eligibility ──────────────────────────────────────────────────────────────────────────────

# Values owned by ``run_store`` / ``artifact_service``, repeated here only because importing them would drag
# SQLAlchemy into this pure module. ``tests/test_report_review_service.py`` fails if they ever drift apart.
STATUS_COMPLETED = "completed"
STATUS_COMPLETED_WITH_ERRORS = "completed_with_errors"
ORIGIN_ARTIFACT = "artifact"

ELIGIBLE = "eligible"
RUN_NOT_FOUND = "run_not_found"
RUN_NOT_COMPLETED = "run_not_completed"
RUN_COMPLETED_WITH_ERRORS = "run_completed_with_errors"
FINAL_REPORT_MISSING = "final_report_missing"
HTML_UNAVAILABLE = "html_unavailable"


@dataclass(frozen=True)
class ReviewEligibility:
    """Typed answer to "can this run be put under review?". ``message`` is public-safe."""

    eligible: bool
    code: str
    message: str = ""
    run_status: str | None = None

    @classmethod
    def ok(cls, run_status: str | None = None) -> "ReviewEligibility":
        return cls(True, ELIGIBLE, "", run_status)

    @classmethod
    def blocked(cls, code: str, message: str, run_status: str | None = None) -> "ReviewEligibility":
        return cls(False, code, message, run_status)


class EligibilityRule(Protocol):
    def check(self, run: RunView, artifacts: "ReviewArtifacts") -> ReviewEligibility | None:
        """``None`` = this rule has no objection; a blocked ``ReviewEligibility`` = stop here."""
        ...


# ── errors (stable ``code``; the HTTP mapping belongs to the web layer) ──────────────────────

class ReviewError(Exception):
    code = "review_failed"
    message = "No se pudo completar la operación de revisión."  # public-safe; never built from internals

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        super().__init__(message or self.message)
        if code is not None:
            self.code = code


class ReviewPermissionError(ReviewError):
    code = "review_forbidden"
    message = "No tenés permiso para realizar esta acción."


class ReviewNotFoundError(ReviewError):
    code = "review_not_found"
    message = "La revisión solicitada no existe."


class ReviewValidationError(ReviewError):
    code = "review_invalid"
    message = "Los datos enviados no son válidos."


class ReviewConflictError(ReviewError):
    """The request is valid but the review is not in the state it assumes (or someone else got there first)."""

    code = "review_conflict"
    message = "La revisión cambió de estado; recargá e intentá de nuevo."

    def __init__(self, message: str | None = None, *, code: str | None = None,
                 current_status: str | None = None) -> None:
        super().__init__(message, code=code)
        self.current_status = current_status


class ReviewNotEligibleError(ReviewError):
    """The run cannot be reviewed; ``code`` is the eligibility code and ``eligibility`` carries the detail."""

    code = "review_not_eligible"
    message = "El informe no es apto para revisión."

    def __init__(self, eligibility: ReviewEligibility) -> None:
        super().__init__(eligibility.message or None, code=eligibility.code)
        self.eligibility = eligibility


class ReviewStoreError(ReviewError):
    """The database refused or failed an operation that was not a plain "someone else won" (details in ``__cause__``)."""

    code = "review_store_failed"
    message = "No se pudo guardar la revisión."


class ReviewPdfError(ReviewError):
    """The PDF behind a review could not be produced or verified; ``code`` is the ``PdfArtifactError`` one."""

    code = "pdf_artifact_failed"
    message = "No se pudo preparar o verificar el PDF de la revisión."

    def __init__(self, cause: PdfArtifactError) -> None:
        super().__init__(str(cause), code=cause.code)
        self.__cause__ = cause


# Error codes raised as ``ReviewConflictError`` / ``ReviewValidationError``.
PREPARATION_IN_PROGRESS = "preparation_in_progress"
REVIEW_STATE_CHANGED = "review_state_changed"
PDF_MISMATCH = "pdf_mismatch"
REASON_REQUIRED = "reason_required"
COMMENT_REQUIRED = "comment_required"
TEXT_TOO_LONG = "text_too_long"
INVALID_DECISION = "invalid_decision"


# ── ports ────────────────────────────────────────────────────────────────────────────────────

class _HasArtifact(Protocol):
    artifact: ArtifactRecord | None


class ReviewArtifacts(Protocol):
    """What the workflow needs from persisted artifacts. ``ArtifactService`` satisfies it unchanged."""

    def get_final_report(self, report_run_id: str) -> _HasArtifact | None: ...

    def get_html(self, report_run_id: str, *, revision: int | None = None,
                 persisted_only: bool = False) -> _HasArtifact | None: ...

    def find_pdf(self, report_run_id: str, *, html_artifact_id: int) -> LoadedPdf | None: ...

    def render_and_save_pdf(self, report_run_id: str, *, html_artifact_id: int | None = None,
                            final_report_artifact_id: int | None = None,
                            extra_metadata: Mapping[str, Any] | None = None) -> SavedPdf: ...

    def get_pdf(self, report_run_id: str, artifact_id: int) -> LoadedPdf: ...


class ReviewStore(Protocol):
    """Persistence of reviews. Every write is one transaction; ``False`` means "someone else won"."""

    def get_run(self, run_id: str) -> RunView | None: ...

    def get_review(self, review_id: int) -> ReviewView | None: ...

    def get_review_for_run(self, run_id: str, round: int = 1) -> ReviewView | None: ...

    def list_events(self, review_id: int) -> list[ReviewEventView]: ...

    def list_reviews(self, query: ReviewQuery) -> list[ReviewView]: ...

    def claim_preparation(self, run_id: str, round: int, *, now: datetime,
                          lease_seconds: float) -> PreparationClaim: ...

    def release_preparation(self, review_id: int) -> None: ...

    def pin_references(self, review_id: int, *, pdf_artifact_id: int, pdf_sha256: str,
                       html_artifact_id: int, final_report_artifact_id: int, now: datetime) -> bool: ...

    def apply_transition(self, review_id: int, *, expected_status: str, changes: Mapping[str, Any],
                         event: NewEvent, also_match: Mapping[str, Any] | None = None) -> bool: ...

    def append_event_guarded(self, review_id: int, *, allowed_statuses: Sequence[str], event: NewEvent,
                             now: datetime) -> bool: ...


class ReviewInbox(Protocol):
    """Read side of the workflow: listings and summaries that join data the write side does not own.

    Kept apart from ``ReviewStore`` so the transactional port does not grow with every screen, and so a
    different backend (a materialized view, a search index) can serve the inbox without touching the rest.
    Reads never write and never load artifact contents.
    """

    def list_items(self, query: ReviewQuery) -> InboxPage: ...

    def get_item(self, review_id: int) -> InboxItem | None: ...

    def describe_run(self, run_id: str) -> RunInfo | None: ...
