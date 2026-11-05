"""Concrete refresh-history sources for ``freshness`` (the only ones that touch the database or Power BI).

* ``DatasetRefreshLogHistory``: what the refresh monitor already persisted (``DatasetRefreshLog``). Free, but
  it is only as recent as the monitor's last poll.
* ``PowerBiRefreshHistory``: a READ-ONLY call to the Power BI refresh history (``get_refresh_history``). Unlike
  ``refresh_monitor.poll_report`` it never triggers a refresh retry, never writes a log row and never queues
  embeddings: checking freshness must not have side effects.

Failures are classified for the dispatcher: credentials / missing report or dataset / 400-401-403-404 are
permanent (the occurrence is blocked); timeouts, connection errors, 408/429/5xx are transient (keep waiting).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import requests
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app import db
from app.models import DatasetRefreshLog, Report, Tenant, Workspace

from .freshness import RefreshLookupError, RefreshRecord

CODE_REPORT_NOT_FOUND = "report_not_found"
CODE_CREDENTIALS_MISSING = "powerbi_credentials_missing"
CODE_UNAUTHORIZED = "powerbi_unauthorized"
CODE_NOT_FOUND = "powerbi_not_found"
CODE_BAD_REQUEST = "powerbi_bad_request"
CODE_DATASET_UNRESOLVED = "powerbi_dataset_unresolved"
CODE_THROTTLED = "powerbi_throttled"
CODE_UNAVAILABLE = "powerbi_unavailable"

_PERMANENT_HTTP = {400: CODE_BAD_REQUEST, 401: CODE_UNAUTHORIZED, 403: CODE_UNAUTHORIZED, 404: CODE_NOT_FOUND}
_FRACTION = re.compile(r"\.(\d+)")


def parse_powerbi_datetime(value) -> datetime | None:
    """ISO-8601 from Power BI (``Z``, 0-7 fractional digits, or no offset = UTC); None if absent/unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value.strip().replace("Z", "+00:00"), 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def classify_powerbi_error(exc: Exception) -> RefreshLookupError:
    """Map what ``app.utils.powerbi`` raises to a permanent/transient lookup error."""
    if isinstance(exc, RefreshLookupError):
        return exc
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        if status in _PERMANENT_HTTP:
            return RefreshLookupError(f"Power BI HTTP {status}", code=_PERMANENT_HTTP[status], permanent=True)
        code = CODE_THROTTLED if status == 429 else CODE_UNAVAILABLE
        return RefreshLookupError(f"Power BI HTTP {status}", code=code, permanent=False)
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return RefreshLookupError(f"Power BI no respondió: {type(exc).__name__}", code=CODE_UNAVAILABLE)
    if isinstance(exc, KeyError):
        return RefreshLookupError("El report no tiene datasetId", code=CODE_DATASET_UNRESOLVED, permanent=True)
    if isinstance(exc, RuntimeError):  # raised by ``_get_access_token`` when credentials are missing
        return RefreshLookupError(str(exc), code=CODE_CREDENTIALS_MISSING, permanent=True)
    return RefreshLookupError(f"{type(exc).__name__}: {exc}", code=CODE_UNAVAILABLE)


class DatasetRefreshLogHistory:
    name = "refresh_log"

    def latest_refreshes(self, report_id: int, *, top: int) -> list[RefreshRecord]:
        rows = db.session.execute(
            select(DatasetRefreshLog).where(DatasetRefreshLog.report_id_fk == report_id)
            .order_by(DatasetRefreshLog.polled_at.desc(), DatasetRefreshLog.id.desc()).limit(max(1, top))).scalars()
        return [RefreshRecord(status=row.status, end_time=row.end_time, start_time=row.start_time,
                              refresh_type=row.refresh_type, source=self.name) for row in rows]


class PowerBiRefreshHistory:
    name = "powerbi_api"

    def __init__(self, fetch=None):
        if fetch is None:
            from app.utils.powerbi import get_refresh_history as fetch
        self.fetch = fetch

    def latest_refreshes(self, report_id: int, *, top: int) -> list[RefreshRecord]:
        report = db.session.execute(
            select(Report).where(Report.id == report_id).options(
                joinedload(Report.workspace).joinedload(Workspace.tenant).joinedload(Tenant.client),
                joinedload(Report.usuario_pbi))).unique().scalar_one_or_none()
        if report is None:
            raise RefreshLookupError(f"Report not found: {report_id}", code=CODE_REPORT_NOT_FOUND, permanent=True)
        try:
            entries = self.fetch(report, top=max(1, top))
        except Exception as exc:  # noqa: BLE001 - every failure is classified, none escapes unlabelled
            raise classify_powerbi_error(exc) from exc
        return [RefreshRecord(status=entry.get("status") or "Unknown",
                              end_time=parse_powerbi_datetime(entry.get("endTime")),
                              start_time=parse_powerbi_datetime(entry.get("startTime")),
                              refresh_type=entry.get("refreshType"), source=self.name)
                for entry in entries]
