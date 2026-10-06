"""Logical report worker: polls PostgreSQL for ``queued`` runs and executes them.

Deliberately independent of Flask routes and of in-memory state: the only thing it
needs is an ``app`` for its config/app-context. Mutual exclusion comes from the
database claim (see ``ReportRunStore.claim_next_run``), never from a Python lock, so
the same ``poll_report_run_queue`` can later run in a dedicated ``report-worker``
container, or in several, without touching the domain or the pipeline.
"""
from __future__ import annotations

import logging
import os
import socket
import uuid

from sqlalchemy import inspect

from app import db
from app.models import ReportRun

from .pipeline_factory import build_run_pipeline
from .run_executor import ReportRunExecutor, default_heartbeat_seconds
from .run_store import DEFAULT_LEASE_SECONDS, ReportRunStore

LOG = logging.getLogger(__name__)
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def lease_seconds() -> int:
    return max(60, int(os.getenv("REPORT_RUN_LEASE_SECONDS", str(DEFAULT_LEASE_SECONDS))))


def heartbeat_seconds(lease: int) -> float:
    """Heartbeat period: ``REPORT_RUN_HEARTBEAT_SECONDS`` (default: a third of the lease, max 60 s)."""
    raw = os.getenv("REPORT_RUN_HEARTBEAT_SECONDS")
    return default_heartbeat_seconds(lease) if not raw else max(1.0, min(float(raw), lease / 2))


def process_next_report_run(app, *, worker_id: str = WORKER_ID, build_pipeline=build_run_pipeline,
                            store: ReportRunStore | None = None) -> bool:
    """Recover stale runs, claim one queued run and execute it. Returns whether work was claimed."""
    store = store or ReportRunStore(lease_seconds=lease_seconds())
    with app.app_context():
        try:
            # The scheduler may start before migrations ran on a fresh deployment.
            if not inspect(db.engine).has_table(ReportRun.__tablename__):
                return False
            store.recover_stale_runs()
            run = store.claim_next_run(worker_id)
            if run is None:
                return False
            run_id = run.id
            LOG.info("[ReportWorker] Starting run=%s worker=%s", run_id, worker_id)
            ReportRunExecutor(store, build_pipeline, config=dict(app.config),
                              heartbeat_seconds=heartbeat_seconds(store.lease_seconds)).execute(run_id, worker_id)
            LOG.info("[ReportWorker] Finished run=%s", run_id)
            return True
        except Exception:
            db.session.rollback()
            LOG.exception("[ReportWorker] Unexpected worker failure")
            return False
        finally:
            db.session.remove()


def poll_report_run_queue(app) -> None:
    process_next_report_run(app)
