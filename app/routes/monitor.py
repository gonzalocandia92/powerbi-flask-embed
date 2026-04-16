"""
Monitor routes — dataset refresh status for all reports.

GET  /monitor/           — Dashboard table view
POST /monitor/reports/<id>/refresh — Force manual refresh for a single report
GET  /monitor/status     — JSON snapshot of latest statuses (for front-end polling)
POST /monitor/poll-all   — Trigger an immediate poll of all reports
"""
import json
import logging
from datetime import datetime, timezone

from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app import db
from app.models import Report, Workspace, Tenant, DatasetRefreshLog
from app.utils.powerbi import refresh_dataset
from app.utils.decorators import retry_on_db_error

bp = Blueprint('monitor', __name__, url_prefix='/monitor')


def _get_latest_logs_by_report():
    """
    Return a dict {report_id: DatasetRefreshLog} containing the most-recent
    log entry per report (based on polled_at).
    """
    # Subquery: MAX polled_at per report
    subq = (
        db.session.query(
            DatasetRefreshLog.report_id_fk,
            func.max(DatasetRefreshLog.polled_at).label("max_polled_at"),
        )
        .group_by(DatasetRefreshLog.report_id_fk)
        .subquery()
    )

    latest_logs = (
        db.session.query(DatasetRefreshLog)
        .join(
            subq,
            (DatasetRefreshLog.report_id_fk == subq.c.report_id_fk)
            & (DatasetRefreshLog.polled_at == subq.c.max_polled_at),
        )
        .all()
    )

    return {log.report_id_fk: log for log in latest_logs}


def _classify(log):
    """
    Return a classification string for a given log entry (or None).

    Values: 'completed', 'failed_with_retry', 'failed_no_retry', 'unknown'
    """
    if log is None:
        return 'unknown'
    status = (log.status or '').lower()
    if status == 'completed':
        return 'completed'
    if status == 'failed':
        return 'failed_with_retry' if log.retry_attempted else 'failed_no_retry'
    return 'unknown'


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def index():
    """Render the monitoring dashboard."""
    reports = (
        Report.query
        .options(
            joinedload(Report.workspace).joinedload(Workspace.tenant),
            joinedload(Report.usuario_pbi),
        )
        .order_by(Report.name)
        .all()
    )

    latest_logs = _get_latest_logs_by_report()

    # Build rows with classification
    rows = []
    for report in reports:
        log = latest_logs.get(report.id)
        classification = _classify(log)

        # Parse error summary for tooltip
        error_summary = None
        if log and log.error_json:
            try:
                err = json.loads(log.error_json)
                error_summary = err.get("errorDescription") or err.get("message") or str(err)[:120]
            except (json.JSONDecodeError, AttributeError):
                error_summary = str(log.error_json)[:120]

        rows.append({
            'report': report,
            'log': log,
            'classification': classification,
            'error_summary': error_summary,
        })

    # Summary counters
    counts = {
        'total': len(rows),
        'completed': sum(1 for r in rows if r['classification'] == 'completed'),
        'failed_with_retry': sum(1 for r in rows if r['classification'] == 'failed_with_retry'),
        'failed_no_retry': sum(1 for r in rows if r['classification'] == 'failed_no_retry'),
        'unknown': sum(1 for r in rows if r['classification'] == 'unknown'),
    }

    return render_template(
        'monitor/index.html',
        rows=rows,
        counts=counts,
        title='Monitor de Actualizaciones',
    )


@bp.route('/reports/<int:report_id>/refresh', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def force_refresh(report_id):
    """Force a manual dataset refresh for a specific report."""
    report = (
        Report.query
        .options(
            joinedload(Report.workspace).joinedload(Workspace.tenant).joinedload(Tenant.client),
            joinedload(Report.usuario_pbi),
        )
        .get_or_404(report_id)
    )

    try:
        result = refresh_dataset(report)
        dataset_id = result.get("dataset_id")

        log = DatasetRefreshLog(
            report_id_fk=report.id,
            dataset_id=dataset_id,
            status='Unknown',
            refresh_type='ManualForced',
            polled_at=datetime.now(timezone.utc),
            retry_attempted=False,
        )
        db.session.add(log)
        db.session.commit()

        return jsonify({
            'status': 'success',
            'message': f'Refresh iniciado para "{report.name}"',
            'dataset_id': dataset_id,
        }), 202

    except Exception as exc:
        import requests as _req
        if isinstance(exc, _req.HTTPError) and exc.response is not None:
            if exc.response.status_code == 429:
                return jsonify({'status': 'error', 'message': 'Límite diario de actualizaciones de Power BI alcanzado'}), 429
        logging.error(f"[Monitor] Manual refresh failed for report {report_id}: {exc}")
        return jsonify({'status': 'error', 'message': f'Error al iniciar refresh: {exc}'}), 500


@bp.route('/status')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def status():
    """
    Return a JSON snapshot of the latest refresh status for all reports.
    Used by the front-end for live auto-refresh without a full page reload.
    """
    latest_logs = _get_latest_logs_by_report()

    reports = Report.query.options(joinedload(Report.workspace)).order_by(Report.name).all()

    data = []
    for report in reports:
        log = latest_logs.get(report.id)
        classification = _classify(log)

        entry = {
            'report_id': report.id,
            'report_name': report.name,
            'workspace': report.workspace.name if report.workspace else None,
            'status': log.status if log else None,
            'classification': classification,
            'polled_at': log.polled_at.isoformat() if log and log.polled_at else None,
            'start_time': log.start_time.isoformat() if log and log.start_time else None,
            'end_time': log.end_time.isoformat() if log and log.end_time else None,
            'refresh_type': log.refresh_type if log else None,
            'retry_attempted': log.retry_attempted if log else False,
        }
        data.append(entry)

    return jsonify({'reports': data, 'generated_at': datetime.now(timezone.utc).isoformat()})


@bp.route('/poll-all', methods=['POST'])
@login_required
def poll_all():
    """Trigger an immediate poll of all reports (runs synchronously in this request)."""
    from app.services.refresh_monitor import poll_all_reports
    from flask import current_app
    try:
        poll_all_reports(current_app._get_current_object())
        return jsonify({'status': 'success', 'message': 'Poll completado'}), 200
    except Exception as exc:
        logging.error(f"[Monitor] poll_all failed: {exc}")
        return jsonify({'status': 'error', 'message': str(exc)}), 500
