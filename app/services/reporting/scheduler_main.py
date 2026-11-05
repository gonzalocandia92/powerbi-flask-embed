"""Dedicated ``report-scheduler`` process (Phase 3).

    python -m app.services.reporting.scheduler_main

Builds the Flask app WITHOUT the web pollers (``DISABLE_BACKGROUND_SCHEDULER=1``) and runs the scheduler
phases (``scheduler_factory.build_scheduler_phases``) on a ``BlockingScheduler`` every
``REPORT_SCHEDULER_TICK_SECONDS`` (default 60). ``max_instances=1`` + ``coalesce`` keep one poll at a time per
process; several processes are safe too, because every write is idempotent or a compare-and-swap.

``poll_report_schedules`` is also what the web process registers when ``REPORT_SCHEDULER_ENABLED=1``
(development only; never in production web workers). Every poll records a heartbeat (``process_heartbeats``) at
its start and its end, so the admin UI can warn when this process is down or its phases keep failing.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from datetime import datetime, timezone

from sqlalchemy import inspect

from app import db

LOG = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 60

_HEARTBEAT = None  # this process's recorder: one identity (instance id, start time) per process


def tick_seconds(env=None) -> int:
    env = os.environ if env is None else env
    try:
        return max(5, int(env.get("REPORT_SCHEDULER_TICK_SECONDS") or DEFAULT_TICK_SECONDS))
    except ValueError:
        return DEFAULT_TICK_SECONDS


def process_heartbeat():
    global _HEARTBEAT
    if _HEARTBEAT is None:
        from .scheduler_factory import build_scheduler_heartbeat
        _HEARTBEAT = build_scheduler_heartbeat(interval_seconds=tick_seconds())
    return _HEARTBEAT


def poll_report_schedules(app, *, build_phases=None, heartbeat=None) -> dict[str, object]:
    """One poll: run every phase in order inside an app context. A failing phase is logged and does not
    stop the following ones. Returns ``{phase name: result or exception}``. ``heartbeat``: ``beat(outcome=None)``
    (default: this process's recorder), called before the phases and with their outcome after them."""
    if build_phases is None:
        from .scheduler_factory import build_scheduler_phases as build_phases
    outcome: dict[str, object] = {}
    with app.app_context():
        try:
            # The process may start before migrations ran on a fresh deployment.
            if not inspect(db.engine).has_table("report_schedules"):
                return outcome
            heartbeat = heartbeat if heartbeat is not None else process_heartbeat()
            heartbeat.beat()
            for name, phase in build_phases():
                try:
                    outcome[name] = phase()
                except Exception as exc:  # noqa: BLE001 - the next phase and the next poll must still run
                    db.session.rollback()
                    outcome[name] = exc
                    LOG.exception("[ReportScheduler] phase %s failed", name)
            heartbeat.beat(outcome)
        finally:
            db.session.remove()
    return outcome


def main() -> None:  # pragma: no cover - process wiring, exercised by the container
    os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "1")
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from apscheduler.schedulers.blocking import BlockingScheduler

    from app import create_app

    app = create_app()
    scheduler = BlockingScheduler(timezone=timezone.utc)
    seconds = tick_seconds()
    scheduler.add_job(poll_report_schedules, args=[app], trigger="interval", seconds=seconds,
                      id="report_scheduler", max_instances=1, coalesce=True,
                      next_run_time=datetime.now(timezone.utc))

    def stop(signum, _frame):
        LOG.info("[ReportScheduler] signal %s: shutting down", signum)
        scheduler.shutdown(wait=True)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOG.info("[ReportScheduler] started: tick every %ss", seconds)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
