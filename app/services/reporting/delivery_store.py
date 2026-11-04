"""Persistence of report deliveries (the outbox of approved reports).

The only module that talks SQLAlchemy about deliveries. Callers exchange the values of
``delivery_contracts`` (never ORM rows), get aware UTC datetimes back and may pass naive (UTC) or aware ones.

Concurrency without long transactions, so several scheduler replicas (or a restart mid-tick) are safe:

* ``create_pending`` is idempotent: ``UNIQUE(review_id, channel, recipient)`` decides.
* ``claim`` is a compare-and-swap (``pending`` and due, or ``sending`` with an expired lease): only one caller
  sends a delivery at a time. Every ``mark_*`` only applies to a ``sending`` row.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import ReportDelivery, ReportReview, ReportRun, ReportSchedule, ReportScheduleOccurrence
from app.persistence.delivery_states import (
    STATUS_FAILED, STATUS_PENDING, STATUS_SENDING, STATUS_SENT, STATUSES,
)
from app.persistence.review_states import STATUS_APPROVED

from .delivery_contracts import DeliveryCandidate, DeliveryJob, DeliveryView
from .schedule_rules import as_utc

MAX_BATCH = 200
MAX_ERROR_CHARS = 2000


def _naive(value: datetime | None) -> datetime | None:
    return None if value is None else as_utc(value).replace(tzinfo=None)


def _aware(value: datetime | None) -> datetime | None:
    return None if value is None else as_utc(value)


def _limit(value: int) -> int:
    return max(1, min(int(value), MAX_BATCH))


def to_delivery_view(row: ReportDelivery) -> DeliveryView:
    return DeliveryView(
        id=row.id, review_id=row.review_id, report_run_id=row.report_run_id, schedule_id=row.schedule_id,
        channel=row.channel, recipient=row.recipient, status=row.status, attempts=row.attempts or 0,
        next_attempt_at=_aware(row.next_attempt_at), provider_message_id=row.provider_message_id,
        last_error_code=row.last_error_code, last_error_message=row.last_error_message,
        sent_at=_aware(row.sent_at), created_at=_aware(row.created_at), updated_at=_aware(row.updated_at))


class DeliveryStore:
    # ── reads ────────────────────────────────────────────────────────────────────────────────

    def find_undelivered_approved(self, since: datetime, limit: int = 100) -> list[DeliveryCandidate]:
        """Approved reviews of scheduled runs, decided at or after ``since``, without any delivery row."""
        has_delivery = exists().where(ReportDelivery.review_id == ReportReview.id)
        rows = db.session.execute(
            select(ReportReview.id.label("review_id"), ReportRun.id.label("run_id"),
                   ReportSchedule.id.label("schedule_id"), ReportSchedule.name, ReportSchedule.config_json,
                   ReportScheduleOccurrence.period_start)
            .join(ReportRun, ReportRun.id == ReportReview.report_run_id)
            .join(ReportScheduleOccurrence, ReportScheduleOccurrence.id == ReportRun.schedule_occurrence_id)
            .join(ReportSchedule, ReportSchedule.id == ReportScheduleOccurrence.schedule_id)
            .where(ReportReview.status == STATUS_APPROVED, ReportReview.decided_at >= _naive(since), ~has_delivery)
            .order_by(ReportReview.decided_at, ReportReview.id)
            .limit(_limit(limit))).all()
        return [DeliveryCandidate(
            review_id=row.review_id, report_run_id=row.run_id, schedule_id=row.schedule_id,
            schedule_name=row.name, schedule_config=dict(row.config_json or {}),
            period_start=_aware(row.period_start)) for row in rows]

    def list_for_review(self, review_id: int) -> list[DeliveryView]:
        rows = db.session.execute(select(ReportDelivery).where(ReportDelivery.review_id == review_id)
                                  .order_by(ReportDelivery.id)
                                  .execution_options(populate_existing=True)).scalars()
        return [to_delivery_view(row) for row in rows]

    def get(self, delivery_id: int) -> DeliveryView | None:
        row = db.session.execute(select(ReportDelivery).where(ReportDelivery.id == delivery_id)
                                 .execution_options(populate_existing=True)).scalar_one_or_none()
        return None if row is None else to_delivery_view(row)

    # ── outbox ───────────────────────────────────────────────────────────────────────────────

    def create_pending(self, candidate: DeliveryCandidate, channel: str, recipients: Iterable[str],
                       now: datetime) -> int:
        """One ``pending`` row per recipient; rows that already exist are left alone. Returns how many were created."""
        created = 0
        for recipient in recipients:
            db.session.add(ReportDelivery(
                review_id=candidate.review_id, report_run_id=candidate.report_run_id,
                schedule_id=candidate.schedule_id, channel=channel, recipient=recipient, status=STATUS_PENDING,
                attempts=0, next_attempt_at=_naive(now), created_at=_naive(now), updated_at=_naive(now)))
            try:
                db.session.commit()
                created += 1
            except IntegrityError:
                db.session.rollback()
            except Exception:
                db.session.rollback()
                raise
        return created

    def due_ids(self, now: datetime, limit: int) -> list[int]:
        return list(db.session.execute(
            select(ReportDelivery.id).where(self._claimable(_naive(now)))
            .order_by(ReportDelivery.next_attempt_at, ReportDelivery.id).limit(_limit(limit))).scalars())

    def claim(self, delivery_id: int, now: datetime, lease: timedelta) -> DeliveryJob | None:
        """``pending`` (due) or ``sending`` with an expired lease -> ``sending``, one more attempt. ``None`` if lost."""
        moment = _naive(now)
        claimed = self._execute_one(update(ReportDelivery).where(
            ReportDelivery.id == delivery_id, self._claimable(moment)
        ).values(status=STATUS_SENDING, lease_until=moment + lease, attempts=ReportDelivery.attempts + 1,
                 updated_at=moment))
        if not claimed:
            return None
        row = db.session.execute(
            select(ReportDelivery, ReportSchedule.name, ReportScheduleOccurrence.period_start)
            .join(ReportRun, ReportRun.id == ReportDelivery.report_run_id)
            .outerjoin(ReportScheduleOccurrence, ReportScheduleOccurrence.id == ReportRun.schedule_occurrence_id)
            .outerjoin(ReportSchedule, ReportSchedule.id == ReportScheduleOccurrence.schedule_id)
            .where(ReportDelivery.id == delivery_id).execution_options(populate_existing=True)).one()
        return DeliveryJob(delivery=to_delivery_view(row.ReportDelivery), schedule_name=row.name or "",
                           period_start=_aware(row.period_start))

    def mark_sent(self, delivery_id: int, now: datetime, provider_message_id: str | None) -> bool:
        return self._finish(delivery_id, now, status=STATUS_SENT, sent_at=_naive(now),
                            provider_message_id=provider_message_id, last_error_code=None, last_error_message=None)

    def mark_retry(self, delivery_id: int, now: datetime, next_attempt_at: datetime, code: str, message: str) -> bool:
        return self._finish(delivery_id, now, status=STATUS_PENDING, next_attempt_at=_naive(next_attempt_at),
                            last_error_code=code, last_error_message=(message or "")[:MAX_ERROR_CHARS])

    def mark_failed(self, delivery_id: int, now: datetime, code: str, message: str) -> bool:
        return self._finish(delivery_id, now, status=STATUS_FAILED, last_error_code=code,
                            last_error_message=(message or "")[:MAX_ERROR_CHARS])

    # ── internals ────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _claimable(moment: datetime):
        return or_(
            and_(ReportDelivery.status == STATUS_PENDING, ReportDelivery.next_attempt_at <= moment),
            and_(ReportDelivery.status == STATUS_SENDING, ReportDelivery.lease_until < moment))

    def _finish(self, delivery_id: int, now: datetime, *, status: str, **values) -> bool:
        if status not in STATUSES:
            raise ValueError(f"Unknown delivery status: {status!r}")
        return self._execute_one(update(ReportDelivery).where(
            ReportDelivery.id == delivery_id, ReportDelivery.status == STATUS_SENDING
        ).values(status=status, lease_until=None, updated_at=_naive(now), **values))

    @staticmethod
    def _execute_one(statement) -> bool:
        try:
            changed = db.session.execute(statement).rowcount
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return changed == 1
