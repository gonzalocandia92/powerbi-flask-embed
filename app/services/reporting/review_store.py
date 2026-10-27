"""SQLAlchemy persistence of reviews and their audit events.

The only reporting module that talks SQLAlchemy about reviews. Like ``ArtifactStore`` it owns its commits
and rolls back on failure. It knows columns and statuses, not the workflow: there is one generic
``apply_transition`` instead of one method per transition, so a new transition (a new decision, a new
status) needs no change here. What must never change is enforced by the database triggers of
``app/persistence/review_triggers.py``; this store never relies on being the only writer.

Concurrency uses conditional ``UPDATE ... WHERE`` (compare-and-swap) and ``rowcount``, which behaves the
same on PostgreSQL and on the SQLite used in tests. A lost race is reported as ``False`` / ``busy``, never
as an exception. The preparation "lease" lives in ``updated_at`` (editable until a decision), so it needs
no extra column.

Datetimes are bound as naive UTC: the columns are ``DateTime`` without time zone.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import ReportReview, ReportReviewEvent, ReportRun
from app.persistence import review_triggers
from app.persistence.review_states import STATUS_DRAFT

from .review_contracts import (
    CLAIM_BUSY, CLAIM_OWNED, CLAIM_SETTLED, MAX_PAGE_SIZE, NewEvent, PreparationClaim, ReviewEventView,
    ReviewQuery, ReviewStoreError, ReviewView, RunView,
)

LOG = logging.getLogger(__name__)

_LEASE_RELEASED = datetime(1970, 1, 1)  # ``updated_at`` of a draft nobody is preparing: any lease has expired
_UPDATABLE = frozenset(ReportReview.__table__.columns.keys()) - set(review_triggers.REVIEW_IDENTITY)


def naive_utc(value: datetime | None) -> datetime | None:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def to_view(row: ReportReview) -> ReviewView:
    """Row -> read model. Public: the read-side adapter (``review_inbox``) maps rows exactly the same way."""
    return ReviewView(
        id=row.id, report_run_id=row.report_run_id, round=row.round, status=row.status,
        pdf_artifact_id=row.pdf_artifact_id, pdf_sha256=row.pdf_sha256,
        final_report_artifact_id=row.final_report_artifact_id, html_artifact_id=row.html_artifact_id,
        submitted_by_user_id=row.submitted_by_user_id, submitted_at=row.submitted_at,
        decided_by_user_id=row.decided_by_user_id, decided_at=row.decided_at,
        rejection_reason=row.rejection_reason, created_at=row.created_at, updated_at=row.updated_at)


def _event_row(review_id: int, event: NewEvent) -> ReportReviewEvent:
    return ReportReviewEvent(
        review_id=review_id, actor_user_id=event.actor_user_id, event_type=event.event_type,
        previous_status=event.previous_status, new_status=event.new_status, comment=event.comment,
        metadata_json=dict(event.metadata) or None)


class SqlReviewStore:
    # ── reads (fresh from the database: Core updates bypass the session identity map) ─────────

    def get_run(self, run_id: str) -> RunView | None:
        row = db.session.execute(select(
            ReportRun.id, ReportRun.status, ReportRun.report_id_fk, ReportRun.requested_by_user_id
        ).where(ReportRun.id == run_id)).one_or_none()
        return None if row is None else RunView(row.id, row.status, row.report_id_fk, row.requested_by_user_id)

    def get_review(self, review_id: int) -> ReviewView | None:
        return self._one(ReportReview.id == review_id)

    def get_review_for_run(self, run_id: str, round: int = 1) -> ReviewView | None:
        return self._one(ReportReview.report_run_id == run_id, ReportReview.round == round)

    def list_events(self, review_id: int) -> list[ReviewEventView]:
        rows = db.session.execute(select(ReportReviewEvent).where(ReportReviewEvent.review_id == review_id)
                                  .order_by(ReportReviewEvent.id).execution_options(populate_existing=True)).scalars()
        return [ReviewEventView(
            id=r.id, review_id=r.review_id, event_type=r.event_type, actor_user_id=r.actor_user_id,
            previous_status=r.previous_status, new_status=r.new_status, comment=r.comment,
            metadata=dict(r.metadata_json or {}), created_at=r.created_at) for r in rows]

    def list_reviews(self, query: ReviewQuery) -> list[ReviewView]:
        stmt = select(ReportReview).execution_options(populate_existing=True)
        if query.report_id is not None:
            stmt = stmt.join(ReportRun, ReportRun.id == ReportReview.report_run_id).where(
                ReportRun.report_id_fk == query.report_id)
        if query.statuses:
            stmt = stmt.where(ReportReview.status.in_(list(query.statuses)))
        if query.submitted_from is not None:
            stmt = stmt.where(ReportReview.submitted_at >= naive_utc(query.submitted_from))
        if query.submitted_to is not None:
            stmt = stmt.where(ReportReview.submitted_at < naive_utc(query.submitted_to))
        limit = max(1, min(int(query.limit), MAX_PAGE_SIZE))
        rows = db.session.execute(stmt.order_by(ReportReview.id.desc()).limit(limit)
                                  .offset(max(0, int(query.offset)))).scalars()
        return [to_view(r) for r in rows]

    # ── preparation lease (the draft is the lock) ─────────────────────────────────────────────

    def claim_preparation(self, run_id: str, round: int, *, now: datetime,
                          lease_seconds: float) -> PreparationClaim:
        """Create the draft (owner) or, if it exists, take over its lease when it has expired.

        ``owned``: the caller must prepare the PDF. ``busy``: another preparer holds a live lease.
        ``settled``: the review already left ``draft``. At most one caller is ever ``owned`` per lease.
        """
        moment = naive_utc(now)
        try:
            row = ReportReview(report_run_id=run_id, round=round, status=STATUS_DRAFT,
                               created_at=moment, updated_at=moment)
            db.session.add(row)
            db.session.commit()
            return PreparationClaim(to_view(row), CLAIM_OWNED)
        except IntegrityError:
            db.session.rollback()  # unique (run, round) already taken, or the run does not exist
        except Exception as exc:
            db.session.rollback()
            raise ReviewStoreError() from exc
        existing = self.get_review_for_run(run_id, round)
        if existing is None:
            raise ReviewStoreError("The review could not be created")
        if existing.status != STATUS_DRAFT:
            return PreparationClaim(existing, CLAIM_SETTLED)
        if existing.updated_at is not None and existing.updated_at > moment - timedelta(seconds=lease_seconds):
            return PreparationClaim(existing, CLAIM_BUSY)
        taken = self._execute_one(update(ReportReview).where(
            ReportReview.id == existing.id, ReportReview.status == STATUS_DRAFT,
            ReportReview.updated_at == existing.updated_at).values(updated_at=moment))
        current = self.get_review(existing.id) or existing
        return PreparationClaim(current, CLAIM_OWNED if taken else CLAIM_BUSY)

    def release_preparation(self, review_id: int) -> None:
        """Give the lease back so the next caller can prepare immediately (``updated_at`` far in the past)."""
        self._execute_one(update(ReportReview).where(
            ReportReview.id == review_id, ReportReview.status == STATUS_DRAFT
        ).values(updated_at=_LEASE_RELEASED))

    def pin_references(self, review_id: int, *, pdf_artifact_id: int, pdf_sha256: str, html_artifact_id: int,
                       final_report_artifact_id: int, now: datetime) -> bool:
        """Fix the artifacts of a draft ONCE (the triggers forbid changing them afterwards)."""
        return self._execute_one(update(ReportReview).where(
            ReportReview.id == review_id, ReportReview.status == STATUS_DRAFT,
            ReportReview.pdf_artifact_id.is_(None)
        ).values(pdf_artifact_id=pdf_artifact_id, pdf_sha256=pdf_sha256, html_artifact_id=html_artifact_id,
                 final_report_artifact_id=final_report_artifact_id, updated_at=naive_utc(now)))

    # ── transitions and audit ─────────────────────────────────────────────────────────────────

    def apply_transition(self, review_id: int, *, expected_status: str, changes: Mapping[str, Any],
                         event: NewEvent, also_match: Mapping[str, Any] | None = None) -> bool:
        """Change a review AND append its event in one transaction, only if it is still in ``expected_status``.

        ``also_match`` adds equality conditions (e.g. ``pdf_sha256``) to that same atomic check.
        Returns ``False`` (nothing written) when the conditions no longer hold: the loser of a race.
        """
        values = self._checked(changes)
        conditions = [ReportReview.id == review_id, ReportReview.status == expected_status,
                      *(getattr(ReportReview, self._column(name)) == naive_utc(value)
                        for name, value in (also_match or {}).items())]
        try:
            if db.session.execute(update(ReportReview).where(*conditions).values(**values)).rowcount != 1:
                db.session.rollback()
                return False
            db.session.add(_event_row(review_id, event))
            db.session.commit()
            return True
        except Exception as exc:
            db.session.rollback()
            LOG.exception("[ReportReview] transition failed review=%s", review_id)
            raise ReviewStoreError() from exc

    def append_event_guarded(self, review_id: int, *, allowed_statuses: Sequence[str], event: NewEvent,
                             now: datetime) -> bool:
        """Append an event without changing the status, only while the review is in ``allowed_statuses``.

        The guard is an ``UPDATE`` of ``updated_at``: on PostgreSQL it takes the row lock, so it serializes
        against a concurrent decision instead of racing with it.
        """
        try:
            guard = update(ReportReview).where(
                ReportReview.id == review_id, ReportReview.status.in_(list(allowed_statuses))
            ).values(updated_at=naive_utc(now))
            if db.session.execute(guard).rowcount != 1:
                db.session.rollback()
                return False
            db.session.add(_event_row(review_id, event))
            db.session.commit()
            return True
        except Exception as exc:
            db.session.rollback()
            LOG.exception("[ReportReview] event append failed review=%s", review_id)
            raise ReviewStoreError() from exc

    # ── internals ─────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _one(*conditions) -> ReviewView | None:
        row = db.session.execute(select(ReportReview).where(*conditions).execution_options(
            populate_existing=True)).scalar_one_or_none()
        return None if row is None else to_view(row)

    @staticmethod
    def _column(name: str) -> str:
        if name not in ReportReview.__table__.columns:
            raise ValueError(f"report_reviews has no column {name!r}")
        return name

    def _checked(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(changes) - _UPDATABLE
        if unknown:
            raise ValueError(f"Not updatable review columns: {sorted(unknown)}")
        return {name: naive_utc(value) if isinstance(value, datetime) else value for name, value in changes.items()}

    @staticmethod
    def _execute_one(statement) -> bool:
        """Run a single-row conditional UPDATE in its own transaction; ``True`` when it matched a row."""
        try:
            matched = db.session.execute(statement).rowcount == 1
            db.session.commit()
            return matched
        except Exception as exc:
            db.session.rollback()
            LOG.exception("[ReportReview] update failed")
            raise ReviewStoreError() from exc
