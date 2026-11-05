"""Read model of the schedules admin screen (Phase 3): what a table shows, in a few queries.

Read-only; never writes and holds no rule. For each occurrence it gathers its runs (attempts and the most recent
one with its timings), the review of that run and, optionally, the run's AI cost (``cost_of(run_id, report_id)``,
injected so the admin screen can show it and tests can stub it; a failure there degrades to ``None``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import func, select

from app import db
from app.models import AnalyticalReportDefinition, Report, ReportReview, ReportRun, ReportSchedule, \
    ReportScheduleOccurrence

from .schedule_contracts import OccurrenceView
from .schedule_store import to_occurrence_view

LOG = logging.getLogger(__name__)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RunInfo:
    id: str
    status: str
    error_code: str | None
    created_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    review_id: int | None = None
    review_status: str | None = None
    cost_usd: str | None = None

    @property
    def duration_seconds(self) -> int | None:
        if self.started_at is None or self.completed_at is None:
            return None
        return max(0, int((self.completed_at - self.started_at).total_seconds()))


@dataclass(frozen=True)
class HistoryItem:
    occurrence: OccurrenceView
    run: RunInfo | None = None
    attempts: int = 0


@dataclass(frozen=True)
class HistoryPage:
    items: list[HistoryItem]
    total: int
    status_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ScheduleLabels:
    definition_name: str | None
    report_id: int | None
    report_name: str | None


class ScheduleHistory:
    def __init__(self, cost_of: Callable[[str, int], str | None] | None = None):
        self.cost_of = cost_of

    def page(self, schedule_id: int, *, limit: int, offset: int) -> HistoryPage:
        base = select(ReportScheduleOccurrence).where(ReportScheduleOccurrence.schedule_id == schedule_id)
        total = db.session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
        rows = db.session.execute(
            base.order_by(ReportScheduleOccurrence.scheduled_for.desc(), ReportScheduleOccurrence.id.desc())
            .limit(limit).offset(offset).execution_options(populate_existing=True)).scalars().all()
        occurrences = [to_occurrence_view(row) for row in rows]
        runs = self._runs([o.id for o in occurrences])
        counts = dict(db.session.execute(
            select(ReportScheduleOccurrence.status, func.count(ReportScheduleOccurrence.id))
            .where(ReportScheduleOccurrence.schedule_id == schedule_id)
            .group_by(ReportScheduleOccurrence.status)).all())
        items = [HistoryItem(occurrence, *runs.get(occurrence.id, (None, 0))) for occurrence in occurrences]
        return HistoryPage(items=items, total=int(total), status_counts={k: int(v) for k, v in counts.items()})

    def labels(self, schedule_ids: list[int]) -> dict[int, ScheduleLabels]:
        if not schedule_ids:
            return {}
        rows = db.session.execute(
            select(ReportSchedule.id, AnalyticalReportDefinition.name, Report.id, Report.name)
            .join(AnalyticalReportDefinition, AnalyticalReportDefinition.id == ReportSchedule.definition_id)
            .outerjoin(Report, Report.id == AnalyticalReportDefinition.report_id_fk)
            .where(ReportSchedule.id.in_(schedule_ids))).all()
        return {row[0]: ScheduleLabels(row[1], row[2], row[3]) for row in rows}

    def definition_options(self) -> list[dict[str, Any]]:
        """Active definitions a schedule can point at (the create form)."""
        rows = db.session.execute(
            select(AnalyticalReportDefinition.id, AnalyticalReportDefinition.name, Report.id, Report.name)
            .outerjoin(Report, Report.id == AnalyticalReportDefinition.report_id_fk)
            .where(AnalyticalReportDefinition.is_active.is_(True))
            .order_by(AnalyticalReportDefinition.name, AnalyticalReportDefinition.id).limit(500)).all()
        return [{"id": r[0], "name": r[1], "report_id": r[2], "report_name": r[3]} for r in rows]

    # ── internals ──────────────────────────────────────────────────────────────────────────────

    def _runs(self, occurrence_ids: list[int]) -> dict[int, tuple[RunInfo, int]]:
        if not occurrence_ids:
            return {}
        rows = db.session.execute(
            select(ReportRun.schedule_occurrence_id, ReportRun.id, ReportRun.status, ReportRun.error_code,
                   ReportRun.created_at, ReportRun.started_at, ReportRun.completed_at, ReportRun.report_id_fk)
            .where(ReportRun.schedule_occurrence_id.in_(occurrence_ids))
            .order_by(ReportRun.created_at, ReportRun.id)).all()
        latest: dict[int, Any] = {}
        attempts: dict[int, int] = {}
        for row in rows:  # ascending: the last one seen is the most recent
            latest[row[0]] = row
            attempts[row[0]] = attempts.get(row[0], 0) + 1
        reviews = {r[0]: (r[1], r[2]) for r in db.session.execute(
            select(ReportReview.report_run_id, ReportReview.id, ReportReview.status)
            .where(ReportReview.report_run_id.in_([row[1] for row in latest.values()]),
                   ReportReview.round == 1)).all()}
        result = {}
        for occurrence_id, row in latest.items():
            review_id, review_status = reviews.get(row[1], (None, None))
            result[occurrence_id] = (RunInfo(
                id=row[1], status=row[2], error_code=row[3], created_at=_aware(row[4]), started_at=_aware(row[5]),
                completed_at=_aware(row[6]), review_id=review_id, review_status=review_status,
                cost_usd=self._cost(row[1], row[7]) if row[6] is not None else None), attempts[occurrence_id])
        return result

    def _cost(self, run_id: str, report_id: int) -> str | None:
        if self.cost_of is None:
            return None
        try:
            return self.cost_of(run_id, report_id)
        except Exception:  # noqa: BLE001 - a cost lookup must never break the history
            LOG.warning("[ReportScheduler] cost lookup failed run=%s", run_id, exc_info=True)
            return None


def ledger_cost(run_id: str, report_id: int) -> str | None:
    """Total AI cost of a run from the usage ledger, as a decimal string (``ReportCostService``)."""
    from .cost import ReportCostService
    summary = ReportCostService().summarize(run_id, report_id=report_id)
    return str(summary.total_cost_usd)
