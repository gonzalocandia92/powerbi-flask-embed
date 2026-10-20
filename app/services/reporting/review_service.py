"""Review workflow: staff approve or reject ONE persisted PDF of a completed report run.

Orchestration only. It never touches Flask, SQLAlchemy, DAX or an LLM: everything it needs arrives through
the ports of ``review_contracts`` (store, artifacts, eligibility rules, policy, clock), so the web layer
today and the scheduler tomorrow drive the same code, and each collaborator is replaceable on its own.

State machine (``app/persistence/review_states.py``, enforced again by database triggers)::

    (no row) --prepare--> draft --submit--> pending_review --approve--> approved
                                                           \\--reject---> rejected

* ``draft`` is the preparation lock, but a row alone does not serialize rendering: the creator (or whoever
  takes over an expired lease) holds the lease and only that caller renders. Others get
  ``preparation_in_progress``. The lease is released as soon as the PDF is pinned or the attempt fails.
* A decision is one atomic ``UPDATE ... WHERE status='pending_review' AND pdf_sha256=<what the reviewer saw>``
  plus its audit event: the loser of two simultaneous reviewers gets a conflict, never a second decision.
* Reading never writes. Reads are not permission-checked here (the web layer decides who may look).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from app.persistence.review_states import (
    COMMENTABLE_STATUSES, EVENT_APPROVED, EVENT_COMMENTED, EVENT_REJECTED, EVENT_SUBMITTED, STATUS_APPROVED,
    STATUS_DRAFT, STATUS_PENDING_REVIEW, STATUS_REJECTED,
)

from .pdf_artifacts import (
    LoadedPdf, PdfArtifactError, PdfIntegrityError, PdfNotFoundError, PdfSourceUnavailableError,
)
from .review_audit import build_event
from .review_contracts import (
    ACTION_COMMENT, ACTION_DECIDE, ACTION_PREPARE, ACTION_SUBMIT, CLAIM_BUSY, CLAIM_SETTLED, COMMENT_REQUIRED,
    INVALID_DECISION, PDF_MISMATCH, PREPARATION_IN_PROGRESS, REASON_REQUIRED, REVIEW_STATE_CHANGED, TEXT_TOO_LONG,
    ReviewActor, ReviewArtifacts, ReviewConflictError, ReviewEligibility, ReviewEventView, ReviewNotEligibleError,
    ReviewNotFoundError, ReviewPdfError, ReviewPermissionError, ReviewPolicy, ReviewQuery, ReviewStore,
    ReviewStoreError, ReviewValidationError, ReviewView, RunView,
)
from .review_eligibility import EligibilityChecker

LOG = logging.getLogger(__name__)

DEFAULT_ROUND = 1  # only the first round exists for now (``UNIQUE(report_run_id, round)`` allows re-review later)
DEFAULT_LEASE_SECONDS = 90.0
MAX_REASON_CHARS = 2000
MAX_COMMENT_CHARS = 4000

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"


@dataclass(frozen=True)
class DecisionSpec:
    """What makes a decision a decision. A new one (e.g. "changes requested") is a row here plus a transition
    in ``review_states``; ``_decide`` does not change."""

    target_status: str
    event_type: str
    requires_reason: bool = False
    requires_eligibility: bool = False   # re-evaluate the eligibility chain at decision time
    requires_intact_pdf: bool = False    # read the stored PDF and verify its SHA-256 at decision time


DECISIONS: dict[str, DecisionSpec] = {
    DECISION_APPROVE: DecisionSpec(STATUS_APPROVED, EVENT_APPROVED, requires_eligibility=True, requires_intact_pdf=True),
    # Rejecting must stay possible when the PDF turns out to be corrupt: that is a reason to reject.
    DECISION_REJECT: DecisionSpec(STATUS_REJECTED, EVENT_REJECTED, requires_reason=True),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_text(value: object, missing_code: str, missing_message: str, limit: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ReviewValidationError(missing_message, code=missing_code)
    if len(text) > limit:
        raise ReviewValidationError(f"El texto supera los {limit} caracteres.", code=TEXT_TOO_LONG)
    return text


class ReviewService:
    def __init__(self, store: ReviewStore, artifacts: ReviewArtifacts, eligibility: EligibilityChecker,
                 policy: ReviewPolicy, *, clock: Callable[[], datetime] | None = None,
                 lease_seconds: float = DEFAULT_LEASE_SECONDS, round: int = DEFAULT_ROUND):
        self.store = store
        self.artifacts = artifacts
        self.eligibility = eligibility
        self.policy = policy
        self.clock = clock or _utcnow
        self.lease_seconds = lease_seconds
        self.round = round

    # ── eligibility ───────────────────────────────────────────────────────────────────────────

    def check_eligibility(self, run_id: str) -> ReviewEligibility:
        """Pure read: never creates a review, so a non-eligible run leaves no trace."""
        return self.eligibility.check(self.store.get_run(run_id), self.artifacts)

    # ── prepare / submit ──────────────────────────────────────────────────────────────────────

    def prepare_review(self, run_id: str, actor: ReviewActor) -> ReviewView:
        """Make sure the run has a draft review with its PDF generated (or reused) and pinned.

        Idempotent: a review that already has a PDF, or has left ``draft``, is returned as it is.
        """
        self._require(actor, ACTION_PREPARE)
        existing = self.store.get_review_for_run(run_id, self.round)
        if existing is not None and existing.status != STATUS_DRAFT:
            return existing
        if existing is not None and existing.has_pdf:
            self._verified_pdf(existing)  # the reviewer is about to look at it: fail early if it is damaged
            return existing
        run = self.store.get_run(run_id)
        eligibility = self.eligibility.check(run, self.artifacts)
        if not eligibility.eligible:
            raise ReviewNotEligibleError(eligibility)
        claim = self.store.claim_preparation(run_id, self.round, now=self.clock(), lease_seconds=self.lease_seconds)
        if claim.outcome == CLAIM_SETTLED:
            return claim.review
        if claim.outcome == CLAIM_BUSY:
            raise ReviewConflictError("El PDF de este informe ya se está preparando.",
                                      code=PREPARATION_IN_PROGRESS, current_status=claim.review.status)
        try:
            return self._prepare_owned(run, claim.review)
        finally:
            self._release(claim.review.id)

    def submit_review(self, run_id: str, actor: ReviewActor) -> ReviewView:
        """``draft`` -> ``pending_review`` (preparing first when needed). Idempotent for a review that left ``draft``."""
        self._require(actor, ACTION_SUBMIT)
        review = self.prepare_review(run_id, actor)
        if review.status != STATUS_DRAFT:
            return review  # double click / a concurrent submit already did it
        run = self._run(review)
        pdf = self._verified_pdf(review)
        now = self.clock()
        event = build_event(EVENT_SUBMITTED, actor=actor, run=run, review=review, pdf=pdf,
                            previous_status=STATUS_DRAFT, new_status=STATUS_PENDING_REVIEW)
        changes = {"status": STATUS_PENDING_REVIEW, "submitted_by_user_id": actor.id,
                   "submitted_at": now, "updated_at": now}
        if not self.store.apply_transition(review.id, expected_status=STATUS_DRAFT, changes=changes, event=event):
            current = self.store.get_review(review.id)
            if current is not None and current.status != STATUS_DRAFT:
                return current
            raise ReviewConflictError(code=REVIEW_STATE_CHANGED, current_status=current.status if current else None)
        return self._review(review.id)

    # ── decisions ─────────────────────────────────────────────────────────────────────────────

    def approve_review(self, review_id: int, actor: ReviewActor, expected_pdf_sha256: str) -> ReviewView:
        return self._decide(DECISION_APPROVE, review_id, actor, expected_pdf_sha256)

    def reject_review(self, review_id: int, actor: ReviewActor, reason: str, expected_pdf_sha256: str) -> ReviewView:
        return self._decide(DECISION_REJECT, review_id, actor, expected_pdf_sha256, reason=reason)

    def add_comment(self, review_id: int, actor: ReviewActor, comment: str) -> None:
        """An observation on a review under review; changes neither the status nor the PDF."""
        self._require(actor, ACTION_COMMENT)
        text = _clean_text(comment, COMMENT_REQUIRED, "Escribí la observación.", MAX_COMMENT_CHARS)
        review = self._review(review_id)
        if review.status not in COMMENTABLE_STATUSES:
            raise ReviewConflictError("Esta revisión ya no admite observaciones.",
                                      code=REVIEW_STATE_CHANGED, current_status=review.status)
        event = build_event(EVENT_COMMENTED, actor=actor, run=self._run(review), review=review, comment=text,
                            previous_status=review.status, new_status=review.status)
        if not self.store.append_event_guarded(review.id, allowed_statuses=COMMENTABLE_STATUSES,
                                               event=event, now=self.clock()):
            current = self.store.get_review(review.id)
            raise ReviewConflictError("Esta revisión ya no admite observaciones.",
                                      code=REVIEW_STATE_CHANGED, current_status=current.status if current else None)

    # ── reads ─────────────────────────────────────────────────────────────────────────────────

    def get_review(self, review_id: int) -> ReviewView:
        return self._review(review_id)

    def find_review_for_run(self, run_id: str) -> ReviewView | None:
        return self.store.get_review_for_run(run_id, self.round)

    def get_review_pdf(self, review_id: int) -> LoadedPdf:
        """The exact PDF this review is about, with its SHA-256 checked against the one fixed in the review.

        Preview and download go through here, so what a reviewer sees is by construction what they approve.
        Raises ``ReviewNotFoundError`` or ``ReviewPdfError`` (``pdf_not_found`` / ``pdf_integrity_failed``).
        """
        return self._verified_pdf(self._review(review_id))

    def list_events(self, review_id: int) -> list[ReviewEventView]:
        self._review(review_id)
        return self.store.list_events(review_id)

    def list_reviews(self, query: ReviewQuery | None = None) -> list[ReviewView]:
        return self.store.list_reviews(query or ReviewQuery())

    # ── internals ─────────────────────────────────────────────────────────────────────────────

    def _decide(self, decision: str, review_id: int, actor: ReviewActor, expected_pdf_sha256: str,
                *, reason: str | None = None) -> ReviewView:
        spec = DECISIONS.get(decision)
        if spec is None:
            raise ReviewValidationError(code=INVALID_DECISION)
        self._require(actor, ACTION_DECIDE)
        if spec.requires_reason:
            reason = _clean_text(reason, REASON_REQUIRED, "Indicá el motivo del rechazo.", MAX_REASON_CHARS)
        review = self._review(review_id)
        if review.status != STATUS_PENDING_REVIEW:
            raise ReviewConflictError("Esta revisión ya fue resuelta o todavía no fue enviada.",
                                      code=REVIEW_STATE_CHANGED, current_status=review.status)
        if not expected_pdf_sha256 or expected_pdf_sha256 != review.pdf_sha256:
            raise ReviewConflictError("El PDF que viste ya no es el de esta revisión.",
                                      code=PDF_MISMATCH, current_status=review.status)
        run = self._run(review)
        if spec.requires_eligibility:
            eligibility = self.eligibility.check(run, self.artifacts)
            if not eligibility.eligible:
                raise ReviewNotEligibleError(eligibility)
        pdf = self._verified_pdf(review) if spec.requires_intact_pdf else None
        now = self.clock()
        event = build_event(spec.event_type, actor=actor, run=run, review=review, pdf=pdf, comment=reason,
                            previous_status=STATUS_PENDING_REVIEW, new_status=spec.target_status)
        changes = {"status": spec.target_status, "decided_by_user_id": actor.id, "decided_at": now, "updated_at": now}
        if reason is not None:
            changes["rejection_reason"] = reason
        applied = self.store.apply_transition(
            review.id, expected_status=STATUS_PENDING_REVIEW, changes=changes, event=event,
            also_match={"pdf_sha256": expected_pdf_sha256})
        if not applied:
            current = self.store.get_review(review.id)
            raise ReviewConflictError("Otra persona resolvió esta revisión antes que vos.",
                                      code=REVIEW_STATE_CHANGED, current_status=current.status if current else None)
        return self._review(review.id)

    def _prepare_owned(self, run: RunView, review: ReviewView) -> ReviewView:
        """Generate (or reuse) the PDF and pin the exact artifacts. Runs only while holding the lease."""
        html = self.artifacts.get_html(run.id, persisted_only=True)
        if html is None or html.artifact is None:
            raise ReviewPdfError(PdfSourceUnavailableError())
        html_id = html.artifact.id
        try:
            pdf = self.artifacts.find_pdf(run.id, html_artifact_id=html_id)  # reuse: PDF bytes are not reproducible
            if pdf is not None:
                artifact, final_id = pdf.artifact, pdf.source_final_report_artifact_id
            else:
                saved = self.artifacts.render_and_save_pdf(run.id, html_artifact_id=html_id)
                artifact, final_id = saved.artifact, saved.source_final_report_artifact_id
        except PdfArtifactError as exc:
            raise ReviewPdfError(exc) from exc
        if final_id is None:
            raise ReviewPdfError(PdfSourceUnavailableError())
        # False = somebody pinned first (our lease had expired): their references stand, ours is an unused PDF.
        self.store.pin_references(review.id, pdf_artifact_id=artifact.id, pdf_sha256=artifact.sha256,
                                  html_artifact_id=html_id, final_report_artifact_id=final_id, now=self.clock())
        return self._review(review.id)

    def _verified_pdf(self, review: ReviewView) -> LoadedPdf:
        """The review's exact PDF, read now with its SHA-256 checked against the one fixed in the review."""
        if not review.has_pdf:
            raise ReviewPdfError(PdfNotFoundError())
        try:
            pdf = self.artifacts.get_pdf(review.report_run_id, review.pdf_artifact_id)
        except PdfArtifactError as exc:
            raise ReviewPdfError(exc) from exc
        if pdf.sha256 != review.pdf_sha256:
            raise ReviewPdfError(PdfIntegrityError())
        return pdf

    def _release(self, review_id: int) -> None:
        try:
            self.store.release_preparation(review_id)
        except ReviewStoreError:
            LOG.warning("[ReportReview] could not release the preparation lease review=%s", review_id)

    def _require(self, actor: ReviewActor, action: str) -> None:
        if not self.policy.allows(actor, action):
            raise ReviewPermissionError()

    def _review(self, review_id: int) -> ReviewView:
        review = self.store.get_review(review_id)
        if review is None:
            raise ReviewNotFoundError()
        return review

    def _run(self, review: ReviewView) -> RunView:
        run = self.store.get_run(review.report_run_id)
        if run is None:
            raise ReviewNotFoundError("La ejecución de esta revisión no existe.", code="run_not_found")
        return run
