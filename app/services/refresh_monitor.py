"""
Refresh monitor service for Power BI semantic models.

Polls all reports and records their last refresh status.
Automatically retries failed refreshes once per polling cycle.
"""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import joinedload

from app import db
from app.models import Report, Workspace, Tenant, Client, DatasetRefreshLog
from app.utils.powerbi import get_refresh_history, refresh_dataset


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_iso(value):
    """Parse an ISO-8601 datetime string returned by Power BI, tolerating various formats."""
    if not value:
        return None
    # Power BI may return strings like "2024-01-15T10:00:00Z" or with milliseconds
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logging.warning(f"Could not parse datetime: {value!r}")
    return None


def _latest_retry_attempted(report_id):
    """
    Return True if the most-recent DatasetRefreshLog for this report already
    has retry_attempted=True. Used to ensure at most one automatic retry per
    Failed cycle.
    """
    latest = (
        DatasetRefreshLog.query
        .filter_by(report_id_fk=report_id)
        .order_by(DatasetRefreshLog.polled_at.desc())
        .first()
    )
    return latest is not None and latest.retry_attempted


def poll_report(report):
    """
    Poll the refresh history for a single report and persist the result.

    If the latest refresh status is 'Failed' and no retry has been attempted yet
    for this report's most recent log entry, triggers one automatic refresh and
    marks retry_attempted=True.

    Args:
        report: Report model instance with all relationships loaded.

    Returns:
        DatasetRefreshLog: The newly created log entry.
    """
    now = _utcnow()
    log = DatasetRefreshLog(
        report_id_fk=report.id,
        polled_at=now,
        status='Unknown',
    )

    try:
        entries = get_refresh_history(report, top=1)
    except Exception as exc:
        logging.error(f"[RefreshMonitor] Failed to poll report id={report.id} ({report.name}): {exc}")
        log.status = 'Unknown'
        db.session.add(log)
        db.session.commit()
        return log

    if entries:
        entry = entries[0]
        log.dataset_id = entry.get("datasetId")
        log.status = entry.get("status", "Unknown")
        log.start_time = _parse_iso(entry.get("startTime"))
        log.end_time = _parse_iso(entry.get("endTime"))
        log.refresh_type = entry.get("refreshType")

        service_exception = entry.get("serviceExceptionJson")
        if service_exception:
            log.error_json = (
                service_exception
                if isinstance(service_exception, str)
                else json.dumps(service_exception)
            )

    # Check retry eligibility BEFORE adding the new log to the session so that
    # _latest_retry_attempted only sees previously committed entries, avoiding
    # any false negative from the unflushed current log.
    should_retry = log.status == "Failed" and not _latest_retry_attempted(report.id)

    db.session.add(log)

    # Automatic retry: once, if status is Failed and no prior retry for this report
    if should_retry:
        logging.info(f"[RefreshMonitor] Auto-retrying failed dataset for report id={report.id} ({report.name})")
        try:
            refresh_dataset(report)
            log.retry_attempted = True
            log.retry_triggered_at = _utcnow()
        except Exception as exc:
            logging.error(f"[RefreshMonitor] Auto-retry failed for report id={report.id}: {exc}")
            # Still mark as attempted so we don't loop
            log.retry_attempted = True
            log.retry_triggered_at = _utcnow()

    db.session.commit()
    return log


def poll_all_reports(app):
    """
    Poll refresh history for every registered report.

    Designed to be called by APScheduler inside an application context.

    Args:
        app: Flask application instance.
    """
    with app.app_context():
        logging.info("[RefreshMonitor] Starting scheduled poll of all reports")
        reports = (
            Report.query
            .options(
                joinedload(Report.workspace)
                .joinedload(Workspace.tenant)
                .joinedload(Tenant.client),
                joinedload(Report.usuario_pbi),
            )
            .all()
        )

        success = 0
        errors = 0
        for report in reports:
            try:
                poll_report(report)
                success += 1
            except Exception as exc:
                errors += 1
                logging.error(
                    f"[RefreshMonitor] Unhandled error polling report id={report.id}: {exc}"
                )
                db.session.rollback()

        logging.info(
            f"[RefreshMonitor] Poll complete — {success} succeeded, {errors} failed"
        )
