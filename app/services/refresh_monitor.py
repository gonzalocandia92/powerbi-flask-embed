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
from app.models import Report, Workspace, Tenant, Client, DatasetRefreshLog, SchemaEmbedding
from app.services.vector_service import trigger_schema_embedding_update
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


def _normalize_utc(dt_value):
    """Normalize datetimes before comparing values across DB backends."""
    if dt_value is None:
        return None
    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=timezone.utc)
    return dt_value.astimezone(timezone.utc)


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


def _latest_refresh_log(report_id):
    """Return the most recent persisted refresh log for a report."""
    return (
        DatasetRefreshLog.query
        .filter_by(report_id_fk=report_id)
        .order_by(DatasetRefreshLog.polled_at.desc())
        .first()
    )


def _has_schema_embeddings(report_id):
    """Return True when the report already has persisted embeddings."""
    return (
        SchemaEmbedding.query
        .filter_by(report_id_fk=report_id)
        .limit(1)
        .first()
        is not None
    )


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
    previous_log = _latest_refresh_log(report.id)
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
    should_trigger_embeddings = (
        log.status == "Completed"
        and bool(log.dataset_id)
        and log.end_time is not None
        and report.chatbot_enabled
        and (
            previous_log is None
            or _normalize_utc(previous_log.end_time) != _normalize_utc(log.end_time)
            or previous_log.status != "Completed"
            or not _has_schema_embeddings(report.id)
        )
    )

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
    if should_trigger_embeddings:
        logging.info(
            "[RefreshMonitor] New completed refresh detected for report id=%s dataset_id=%s. Triggering embeddings.",
            report.id,
            log.dataset_id,
        )
        trigger_schema_embedding_update(report.id, log.dataset_id)
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
