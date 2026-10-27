"""JSON shapes of the review workflow: the ONE place that turns read models into dictionaries.

Pure functions over the dataclasses of ``review_contracts``: no Flask, no SQLAlchemy, no URL building (callers
pass URLs in). Dates are ISO strings and nothing here can carry a BLOB. The review screens (stage 5) and any
other client consume exactly these contracts, so a field is added here and nowhere else.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.persistence.review_states import COMMENTABLE_STATUSES, STATUS_DRAFT, STATUS_PENDING_REVIEW
from app.services.reporting.review_contracts import (
    ACTION_COMMENT, ACTION_DECIDE, ACTION_PREPARE, ACTION_SUBMIT, InboxItem, InboxPage, ReviewActor,
    ReviewEligibility, ReviewEventView, ReviewPolicy, ReviewView, RunInfo,
)

# Names of the buttons a client may offer -> the policy action each one needs.
# A new capability = an entry here + a branch in ``available_actions``.
ACTION_NAMES = {
    "prepare": ACTION_PREPARE, "submit": ACTION_SUBMIT, "approve": ACTION_DECIDE, "reject": ACTION_DECIDE,
    "comment": ACTION_COMMENT,
}


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def review_payload(review: ReviewView, *, pdf_url: str | None = None) -> dict[str, Any]:
    return {
        "id": review.id, "run_id": review.report_run_id, "round": review.round, "status": review.status,
        "has_pdf": review.has_pdf,
        "pdf": None if not review.has_pdf else {
            "artifact_id": review.pdf_artifact_id, "sha256": review.pdf_sha256, "url": pdf_url},
        "submitted_by_user_id": review.submitted_by_user_id, "submitted_at": iso(review.submitted_at),
        "decided_by_user_id": review.decided_by_user_id, "decided_at": iso(review.decided_at),
        "rejection_reason": review.rejection_reason,
        "created_at": iso(review.created_at), "updated_at": iso(review.updated_at),
    }


def summary_payload(review: ReviewView | None) -> dict[str, Any] | None:
    """The few fields another screen needs to know a run is under review (no hashes, no people)."""
    return None if review is None else {"id": review.id, "status": review.status, "has_pdf": review.has_pdf}


def event_payload(event: ReviewEventView) -> dict[str, Any]:
    return {
        "id": event.id, "review_id": event.review_id, "type": event.event_type,
        "actor_user_id": event.actor_user_id, "previous_status": event.previous_status,
        "new_status": event.new_status, "comment": event.comment, "metadata": dict(event.metadata),
        "created_at": iso(event.created_at),
    }


def eligibility_payload(eligibility: ReviewEligibility) -> dict[str, Any]:
    return {"eligible": eligibility.eligible, "code": eligibility.code, "message": eligibility.message,
            "run_status": eligibility.run_status}


def run_info_payload(info: RunInfo | None) -> dict[str, Any] | None:
    """Descriptive fields of the run under review (what the detail screen titles itself with)."""
    if info is None:
        return None
    return {"id": info.run_id, "status": info.status, "name": info.name, "report_id": info.report_id,
            "report_name": info.report_name, "created_at": iso(info.created_at), "requested_by": info.requested_by}


def inbox_item_payload(item: InboxItem, *, pdf_url: str | None = None) -> dict[str, Any]:
    return {
        **review_payload(item.review, pdf_url=pdf_url),
        "run_status": item.run_status, "run_name": item.run_name, "report_id": item.report_id,
        "report_name": item.report_name, "run_created_at": iso(item.run_created_at),
        "requested_by": item.requested_by, "submitted_by": item.submitted_by, "decided_by": item.decided_by,
    }


def inbox_page_payload(page: InboxPage, items: list[dict[str, Any]]) -> dict[str, Any]:
    per_page = max(1, page.limit)
    return {"items": items, "page": page.offset // per_page + 1, "per_page": per_page,
            "total": page.total, "pages": max(1, -(-page.total // per_page))}


def needs_eligibility(review: ReviewView | None) -> bool:
    """Whether eligibility can still change what the caller may do (only before a review is submitted).

    Next to ``available_actions`` so the two cannot disagree: a state that offers prepare/submit needs it.
    """
    return review is None or review.status == STATUS_DRAFT


def available_actions(review: ReviewView | None, eligibility: ReviewEligibility | None, *,
                      actor: ReviewActor, policy: ReviewPolicy) -> list[str]:
    """What THIS actor may do right now, derived from the state, the eligibility and the policy.

    Advisory for the UI only: the service re-checks everything on every call.
    """
    actions: list[str] = []
    status = review.status if review is not None else None
    if needs_eligibility(review) and eligibility is not None and eligibility.eligible:
        actions += ["prepare", "submit"]
    if status == STATUS_PENDING_REVIEW:
        actions += ["approve", "reject"]
    if status in COMMENTABLE_STATUSES:
        actions.append("comment")
    return [name for name in actions if policy.allows(actor, ACTION_NAMES[name])]
