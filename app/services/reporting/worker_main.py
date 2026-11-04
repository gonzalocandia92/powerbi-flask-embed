"""Dedicated ``report-worker`` process.

    python -m app.services.reporting.worker_main

Runs ``run_worker.poll_report_run_queue`` (DAX / LLM / document) every ``REPORT_RUN_POLL_SECONDS`` (default 3) on
a ``BlockingScheduler``, outside the web process. Mutual exclusion comes from the database claim, so several
worker containers are safe. The web process must then set ``REPORT_RUN_WORKER_ENABLED=0``.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from datetime import datetime, timezone

LOG = logging.getLogger(__name__)


def poll_seconds(env=None) -> int:
    env = os.environ if env is None else env
    try:
        return max(1, int(env.get("REPORT_RUN_POLL_SECONDS") or 3))
    except ValueError:
        return 3


def main() -> None:  # pragma: no cover - process wiring, exercised by the container
    os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "1")
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from apscheduler.schedulers.blocking import BlockingScheduler

    from app import create_app
    from app.services.reporting.run_worker import WORKER_ID, poll_report_run_queue

    app = create_app()
    scheduler = BlockingScheduler(timezone=timezone.utc)
    seconds = poll_seconds()
    scheduler.add_job(poll_report_run_queue, args=[app], trigger="interval", seconds=seconds,
                      id="report_run_worker", max_instances=1, coalesce=True,
                      next_run_time=datetime.now(timezone.utc))

    def stop(signum, _frame):
        # wait=True: a run in progress keeps its lease until it finishes or the container is killed; a killed
        # run is recovered (lease expiry) without relaunching an analysis that may have spent tokens.
        LOG.info("[ReportWorker] signal %s: shutting down", signum)
        scheduler.shutdown(wait=True)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOG.info("[ReportWorker] started worker=%s poll every %ss", WORKER_ID, seconds)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
