"""
Monitor routes — dataset refresh status for all reports.

GET  /monitor/           — Dashboard table view
POST /monitor/reports/<id>/refresh — Force manual refresh for a single report
GET  /monitor/reports/<id>/embed  — Embed config JSON for modal preview
GET  /monitor/status     — JSON snapshot of latest statuses (for front-end polling)
POST /monitor/poll-all   — Trigger an immediate poll of all reports
"""
import json
import logging
import os
from datetime import datetime, timezone

import requests as _req
from flask import Blueprint, render_template, jsonify, current_app
from flask_login import login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app import db
from app.models import Report, Workspace, Tenant, DatasetRefreshLog
from app.utils.powerbi import refresh_dataset, get_embed_for_report
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


def _parse_error(log):
    """
    Parse error_json from a DatasetRefreshLog into (summary, detail).
    Returns (None, None) when there is no error.
    """
    if not log or not log.error_json:
        return None, None
    try:
        err = json.loads(log.error_json)
        summary = err.get("errorDescription") or err.get("message") or str(err)[:120]
        detail = json.dumps(err, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, AttributeError):
        summary = str(log.error_json)[:120]
        detail = str(log.error_json)
    return summary, detail


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

        # Parse error
        error_summary, error_detail = _parse_error(log)

        rows.append({
            'report': report,
            'log': log,
            'classification': classification,
            'error_summary': error_summary,
            'error_detail': error_detail,
        })

    # Sort: errors first, then by tenant name, workspace name, report id
    _cls_order = {'failed_with_retry': 0, 'failed_no_retry': 1, 'unknown': 2, 'completed': 3}
    rows.sort(key=lambda r: (
        _cls_order.get(r['classification'], 99),
        (r['report'].workspace.tenant.name if r['report'].workspace and r['report'].workspace.tenant else ''),
        (r['report'].workspace.name if r['report'].workspace else ''),
        r['report'].id,
    ))

    # Summary counters
    counts = {
        'total': len(rows),
        'completed': sum(1 for r in rows if r['classification'] == 'completed'),
        'failed_with_retry': sum(1 for r in rows if r['classification'] == 'failed_with_retry'),
        'failed_no_retry': sum(1 for r in rows if r['classification'] == 'failed_no_retry'),
        'unknown': sum(1 for r in rows if r['classification'] == 'unknown'),
    }

    interval_hours = int(os.getenv('REFRESH_POLL_INTERVAL_HOURS', 12))

    return render_template(
        'monitor/index.html',
        rows=rows,
        counts=counts,
        interval_hours=interval_hours,
        title='Monitor de Actualizaciones',
    )


@bp.route('/reports/<int:report_id>/poll', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def poll_report_status(report_id):
    """Poll the current refresh status from Power BI for a single report."""
    from app.services.refresh_monitor import poll_report

    report = (
        Report.query
        .options(
            joinedload(Report.workspace).joinedload(Workspace.tenant).joinedload(Tenant.client),
            joinedload(Report.usuario_pbi),
        )
        .get_or_404(report_id)
    )

    try:
        log = poll_report(report)
        classification = _classify(log)

        error_summary, error_detail = _parse_error(log)

        return jsonify({
            'status': 'success',
            'message': f'Estado actualizado para "{report.name}"',
            'classification': classification,
            'log_status': log.status if log else None,
            'polled_at': log.polled_at.isoformat() if log and log.polled_at else None,
            'start_time': log.start_time.isoformat() if log and log.start_time else None,
            'end_time': log.end_time.isoformat() if log and log.end_time else None,
            'refresh_type': log.refresh_type if log else None,
            'retry_attempted': log.retry_attempted if log else False,
            'error_summary': error_summary,
            'error_detail': error_detail,
        }), 200
    except Exception as exc:
        logging.error(f"[Monitor] Poll status failed for report {report_id}: {exc}")
        return jsonify({'status': 'error', 'message': 'Error al consultar el estado del modelo semántico'}), 500


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
        if isinstance(exc, _req.HTTPError) and exc.response is not None:
            if exc.response.status_code == 429:
                return jsonify({'status': 'error', 'message': 'Límite diario de actualizaciones de Power BI alcanzado'}), 429
        logging.error(f"[Monitor] Manual refresh failed for report {report_id}: {exc}")
        return jsonify({'status': 'error', 'message': 'Error al iniciar el refresh del modelo semántico'}), 500


@bp.route('/reports/<int:report_id>/embed')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def embed_config(report_id):
    """Return embed configuration JSON for displaying a report in a modal."""
    report = (
        Report.query
        .options(
            joinedload(Report.workspace).joinedload(Workspace.tenant).joinedload(Tenant.client),
            joinedload(Report.usuario_pbi),
        )
        .get_or_404(report_id)
    )

    try:
        embed_token, embed_url, rid = get_embed_for_report(report)
        return jsonify({
            'status': 'success',
            'embedUrl': embed_url,
            'accessToken': embed_token,
            'reportId': rid,
            'reportName': report.name,
        }), 200
    except Exception as exc:
        logging.error(f"[Monitor] Embed config failed for report {report_id}: {exc}")
        return jsonify({'status': 'error', 'message': 'Error al obtener la configuración de embed del reporte'}), 500


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

        error_summary, error_detail = _parse_error(log)

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
            'error_summary': error_summary,
            'error_detail': error_detail,
        }
        data.append(entry)

    return jsonify({'reports': data, 'generated_at': datetime.now(timezone.utc).isoformat()})


@bp.route('/poll-all', methods=['POST'])
@login_required
def poll_all():
    """Trigger an immediate poll of all reports (runs synchronously in this request)."""
    from app.services.refresh_monitor import poll_all_reports
    try:
        poll_all_reports(current_app._get_current_object())
        return jsonify({'status': 'success', 'message': 'Poll completado'}), 200
    except Exception as exc:
        logging.error(f"[Monitor] poll_all failed: {exc}")
        return jsonify({'status': 'error', 'message': 'Error durante el poll de actualizaciones'}), 500
