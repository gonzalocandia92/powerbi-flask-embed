"""Audit events of the review workflow: the ``metadata_json`` contract in ONE place.

Pure functions. The service decides WHAT happened; this module decides how it is recorded, so the shape
of the trail (snapshot of the actor, self-review flag, run status, the exact PDF, versions, source) can
evolve without touching the workflow. ``metadata["v"]`` versions that shape.

    {"v": 1,
     "actor": {"kind": "user", "id": 12, "username": "...", "email": "..."},   # snapshot: survives user deletion
     "source": "manual" | "schedule",
     "self_review": true,
     "run_status": "completed",
     "pdf": {"artifact_id": 345, "sha256": "...", "size_bytes": 182340},
     "versions": {"schema": "1.3.1", "html_renderer": "html-v4", "pdf_engine": "weasyprint",
                  "pdf_engine_version": "70.0", "release": "..."}}

Engine identity is generic (``pdf_engine`` / ``pdf_engine_version``, as the PDF artifact records it), so
changing the PDF engine does not change this contract.
"""
from __future__ import annotations

from typing import Any

from app.persistence.review_states import EVENT_TYPES

from .pdf_artifacts import LoadedPdf
from .review_contracts import NewEvent, ReviewActor, ReviewView, RunView

METADATA_VERSION = 1


def is_self_review(actor: ReviewActor, run: RunView, review: ReviewView) -> bool:
    """The actor generated the report or submitted it for review (a system actor never reviews itself)."""
    if actor.id is None:
        return False
    return actor.id in {run.requested_by_user_id, review.submitted_by_user_id}


def pdf_snapshot(review: ReviewView, pdf: LoadedPdf | None) -> dict[str, Any]:
    return {"artifact_id": review.pdf_artifact_id, "sha256": review.pdf_sha256,
            "size_bytes": pdf.size_bytes if pdf is not None else None}


def versions_snapshot(pdf: LoadedPdf) -> dict[str, Any]:
    meta = pdf.artifact.metadata
    return {"schema": pdf.artifact.schema_version, "html_renderer": meta.get("html_renderer_version"),
            "pdf_engine": meta.get("pdf_engine"), "pdf_engine_version": meta.get("pdf_engine_version"),
            "release": meta.get("release")}


def build_metadata(*, actor: ReviewActor, run: RunView, review: ReviewView,
                   pdf: LoadedPdf | None = None) -> dict[str, Any]:
    """``pdf`` is the verified PDF when the caller read it; without it only ids and hash are recorded."""
    metadata: dict[str, Any] = {
        "v": METADATA_VERSION,
        "actor": actor.snapshot(),
        "source": actor.source,
        "self_review": is_self_review(actor, run, review),
        "run_status": run.status,
        "pdf": pdf_snapshot(review, pdf),
    }
    if pdf is not None:
        metadata["versions"] = versions_snapshot(pdf)
    return metadata


def build_event(event_type: str, *, actor: ReviewActor, run: RunView, review: ReviewView,
                previous_status: str | None, new_status: str | None, comment: str | None = None,
                pdf: LoadedPdf | None = None) -> NewEvent:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown review event type: {event_type!r}")
    return NewEvent(event_type=event_type, actor_user_id=actor.id, previous_status=previous_status,
                    new_status=new_status, comment=comment,
                    metadata=build_metadata(actor=actor, run=run, review=review, pdf=pdf))
