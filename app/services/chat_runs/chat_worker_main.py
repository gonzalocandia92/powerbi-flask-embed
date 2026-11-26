"""Dedicated ``chat-worker`` process.

    python -m app.services.chat_runs.chat_worker_main

Runs ``ChatWorker`` (``CHAT_WORKER_CONCURRENCY`` slots, default 4) outside the web process. On SIGTERM/SIGINT
it stops claiming and waits up to ``CHAT_WORKER_SHUTDOWN_GRACE_SECONDS`` (default 110 s; compose grants 120 s)
for the answers in progress, so a deploy does not cut them. Several containers are safe: mutual exclusion comes
from the database claim. The web process must then set ``CHAT_RUN_WORKER_ENABLED=0``.
"""
from __future__ import annotations

import logging
import os
import signal
import sys

LOG = logging.getLogger(__name__)


def main() -> None:  # pragma: no cover - process wiring, exercised by the container
    os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "1")
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from app import create_app
    from app.services.chat_runs.settings import ChatWorkerSettings
    from app.services.chat_runs.worker import ChatWorker

    app = create_app()
    settings = ChatWorkerSettings.from_env()
    worker = ChatWorker(app, settings)

    def stop(signum, _frame):
        LOG.info("[ChatWorker] signal %s: no new runs; waiting up to %ss for %s in progress",
                 signum, settings.shutdown_grace_seconds, worker.busy)
        worker.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGBREAK"):  # Windows (local development): CTRL_BREAK_EVENT is its graceful stop
        signal.signal(signal.SIGBREAK, stop)
    worker.start()
    while not worker.wait(1.0):
        pass
    finished = worker.join(settings.shutdown_grace_seconds)
    LOG.info("[ChatWorker] exited (all runs finished=%s)", finished)
    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
