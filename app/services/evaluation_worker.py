"""Database-backed single-worker queue for model evaluations."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect

from app import db
from app.models import ModelEvaluationCase, ModelEvaluationRun, Report
from app.services.evaluation import build_report_evaluation_runner


LOG = logging.getLogger(__name__)
_WORKER_LOCK = threading.Lock()
_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _utcnow():
    return datetime.now(timezone.utc)


def _recover_stale_runs(now):
    stale = ModelEvaluationRun.query.filter(
        ModelEvaluationRun.status == "running",
        ModelEvaluationRun.lease_expires_at.isnot(None),
        ModelEvaluationRun.lease_expires_at < now,
    ).all()
    for run in stale:
        for case in run.cases.filter_by(status="running").all():
            case.status = "interrupted"
            case.failure_reason = "worker_interrupted"
            case.error_message = "La ejecucion fue interrumpida y no se repitio para evitar doble facturacion."
            case.completed_at = now
        terminal = run.cases.filter(
            ModelEvaluationCase.status.in_(("success", "error", "interrupted", "cancelled"))
        ).count()
        run.completed_cases = terminal
        run.worker_id = None
        run.heartbeat_at = now
        run.lease_expires_at = None
        if run.cases.filter_by(status="pending").count():
            run.status = "queued"
        else:
            run.status = "completed_with_errors"
            run.completed_at = now
    if stale:
        db.session.commit()


def process_next_evaluation(app) -> bool:
    """Claim and execute one queued run. Returns whether work was claimed."""
    if not _WORKER_LOCK.acquire(blocking=False):
        return False
    try:
        with app.app_context():
            run = None
            claimed_run_id = None
            try:
                # The scheduler may start while a new deployment is still waiting
                # for migrations. Stay idle instead of emitting an error every tick.
                if not inspect(db.engine).has_table(ModelEvaluationRun.__tablename__):
                    return False
                now = _utcnow()
                _recover_stale_runs(now)
                run = ModelEvaluationRun.query.filter_by(status="queued").order_by(
                    ModelEvaluationRun.created_at.asc(), ModelEvaluationRun.id.asc()
                ).first()
                if run is None:
                    return False
                claimed_run_id = run.id
                run.status = "running"
                run.worker_id = _WORKER_ID
                run.started_at = run.started_at or now
                run.heartbeat_at = now
                run.lease_expires_at = now + timedelta(hours=1)
                db.session.commit()
                report = db.session.get(Report, run.report_id_fk)
                if report is None:
                    run.status = "failed"
                    run.error_message = "El reporte de la evaluacion ya no existe."
                    run.completed_at = _utcnow()
                    run.worker_id = None
                    run.lease_expires_at = None
                    db.session.commit()
                    return True
                LOG.info("[EvaluationWorker] Starting run_id=%s worker=%s", run.id, _WORKER_ID)
                runner = build_report_evaluation_runner(report, config=dict(app.config))
                asyncio.run(runner.run_existing(run))
                LOG.info("[EvaluationWorker] Finished run_id=%s", run.id)
                return True
            except Exception:
                db.session.rollback()
                LOG.exception("[EvaluationWorker] Failed while processing evaluation")
                if claimed_run_id is not None:
                    failed_run = db.session.get(ModelEvaluationRun, claimed_run_id)
                    if failed_run is not None and failed_run.status in {"queued", "running"}:
                        failed_run.status = "failed"
                        failed_run.error_message = "No se pudo iniciar o completar la evaluacion. Revisa los logs del worker."
                        failed_run.completed_at = _utcnow()
                        failed_run.worker_id = None
                        failed_run.lease_expires_at = None
                        db.session.commit()
                return True
    finally:
        _WORKER_LOCK.release()


def poll_evaluation_queue(app) -> None:
    process_next_evaluation(app)
