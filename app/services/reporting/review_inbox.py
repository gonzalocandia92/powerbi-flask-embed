"""SQLAlchemy read model of the review inbox (``ReviewInbox`` port).

Joins what the transactional ``SqlReviewStore`` does not own (report name, run description, user names) so
screens need no per-row queries. It selects COLUMNS only: it never touches ``ReportRun.artifacts`` or
``content_bytes`` (a listing must not drag PDF/HTML BLOBs into memory), and it never writes.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app import db
from app.models import Report, ReportReview, ReportRun, User

from .review_contracts import MAX_PAGE_SIZE, InboxItem, InboxPage, ReviewQuery, RunInfo
from .review_store import naive_utc, to_view

_REQUESTER = aliased(User)
_SUBMITTER = aliased(User)
_DECIDER = aliased(User)


class SqlReviewInbox:
    def list_items(self, query: ReviewQuery) -> InboxPage:
        filters = self._filters(query)
        total = db.session.execute(
            select(func.count(ReportReview.id)).join(ReportRun, ReportRun.id == ReportReview.report_run_id)
            .where(*filters)).scalar_one()
        limit = max(1, min(int(query.limit), MAX_PAGE_SIZE))
        offset = max(0, int(query.offset))
        rows = db.session.execute(
            self._base().where(*filters).order_by(ReportReview.id.desc()).limit(limit).offset(offset)).all()
        return InboxPage(items=[self._item(row) for row in rows], total=int(total), limit=limit, offset=offset)

    def get_item(self, review_id: int) -> InboxItem | None:
        row = db.session.execute(self._base().where(ReportReview.id == review_id)).one_or_none()
        return None if row is None else self._item(row)

    def describe_run(self, run_id: str) -> RunInfo | None:
        """Descriptive columns of one run (works before any review exists); ``None`` when the run does not exist."""
        row = db.session.execute(
            select(ReportRun.id, ReportRun.status, ReportRun.definition_snapshot_json.label("snapshot"),
                   ReportRun.report_id_fk.label("report_id"), ReportRun.created_at, Report.name.label("report_name"),
                   _REQUESTER.username.label("requested_by"))
            .outerjoin(Report, Report.id == ReportRun.report_id_fk)
            .outerjoin(_REQUESTER, _REQUESTER.id == ReportRun.requested_by_user_id)
            .where(ReportRun.id == run_id)).one_or_none()
        if row is None:
            return None
        snapshot = row.snapshot if isinstance(row.snapshot, dict) else {}
        return RunInfo(run_id=row.id, status=row.status, name=snapshot.get("name"), report_id=row.report_id,
                       report_name=row.report_name, created_at=row.created_at, requested_by=row.requested_by)

    # ── internals ─────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _filters(query: ReviewQuery) -> list:
        filters = []
        if query.report_id is not None:
            filters.append(ReportRun.report_id_fk == query.report_id)
        if query.statuses:
            filters.append(ReportReview.status.in_(list(query.statuses)))
        if query.submitted_from is not None:
            filters.append(ReportReview.submitted_at >= naive_utc(query.submitted_from))
        if query.submitted_to is not None:
            filters.append(ReportReview.submitted_at < naive_utc(query.submitted_to))
        return filters

    @staticmethod
    def _base():
        return (
            select(ReportReview, ReportRun.status.label("run_status"),
                   ReportRun.definition_snapshot_json.label("snapshot"), ReportRun.report_id_fk.label("report_id"),
                   ReportRun.created_at.label("run_created_at"), Report.name.label("report_name"),
                   _REQUESTER.username.label("requested_by"), _SUBMITTER.username.label("submitted_by"),
                   _DECIDER.username.label("decided_by"))
            .join(ReportRun, ReportRun.id == ReportReview.report_run_id)
            .outerjoin(Report, Report.id == ReportRun.report_id_fk)
            .outerjoin(_REQUESTER, _REQUESTER.id == ReportRun.requested_by_user_id)
            .outerjoin(_SUBMITTER, _SUBMITTER.id == ReportReview.submitted_by_user_id)
            .outerjoin(_DECIDER, _DECIDER.id == ReportReview.decided_by_user_id)
            .execution_options(populate_existing=True))

    @staticmethod
    def _item(row) -> InboxItem:
        snapshot = row.snapshot if isinstance(row.snapshot, dict) else {}
        return InboxItem(
            review=to_view(row.ReportReview), run_status=row.run_status, run_name=snapshot.get("name"),
            report_id=row.report_id, report_name=row.report_name, run_created_at=row.run_created_at,
            requested_by=row.requested_by, submitted_by=row.submitted_by, decided_by=row.decided_by)
