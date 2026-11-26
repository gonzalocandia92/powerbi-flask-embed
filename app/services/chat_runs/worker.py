"""The chat worker: ``concurrency`` slot threads that claim and execute chat runs, plus one maintenance thread.

* Each slot loops: claim the oldest queued run -> execute it -> claim again; when the queue is empty it waits
  ``poll_seconds``. Every iteration runs in its own Flask app context (its own SQLAlchemy session), and the
  executor runs the turn with ``asyncio.run`` in the slot's thread, as the web process does per request.
* The maintenance thread closes runs whose worker vanished (``recover_stale``), expires runs that waited too
  long, purges old finished runs and records the process heartbeat (``process_heartbeats``, process
  ``chat-worker``), so an admin view can tell whether the worker is alive.
* ``stop()`` stops CLAIMING; runs in progress finish (a deploy must not cut answers). Whatever is still
  running when the process is killed is closed later as ``worker_lost`` by any worker, never re-run.

Mutual exclusion comes only from the database claim: several worker processes (replicas) are safe.
Runs in the dedicated ``chat-worker`` container (``chat_worker_main``) or, for local development, embedded
in the web process (``start_embedded_worker``, flag ``CHAT_RUN_WORKER_ENABLED``).
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

from sqlalchemy import inspect

from app import db
from app.models import ChatRun

from .executor import ChatRunExecutor
from .settings import ChatWorkerSettings
from .store import ChatRunStore

LOG = logging.getLogger(__name__)

PROCESS_CHAT_WORKER = "chat-worker"
PURGE_EVERY = timedelta(hours=1)
_ERROR_BACKOFF_FACTOR = 5

StoreFactory = Callable[[], ChatRunStore]
ExecutorFactory = Callable[[ChatRunStore], ChatRunExecutor]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_heartbeat_recorder(settings: ChatWorkerSettings):
    """``process_heartbeats`` recorder of this process (the same facility the report-scheduler uses)."""
    from app.services.reporting.heartbeat import HeartbeatRecorder, ProcessIdentity
    from app.services.reporting.heartbeat_store import HeartbeatStore

    return HeartbeatRecorder(HeartbeatStore(), ProcessIdentity.current(PROCESS_CHAT_WORKER),
                             interval_seconds=int(max(1, settings.maintenance_seconds)))


class ChatWorker:
    def __init__(self, app, settings: Optional[ChatWorkerSettings] = None, *,
                 store_factory: Optional[StoreFactory] = None, executor_factory: Optional[ExecutorFactory] = None,
                 heartbeat=None, clock: Callable[[], datetime] = _utcnow, instance_id: Optional[str] = None):
        self.app = app
        self.settings = settings or ChatWorkerSettings()
        self.store_factory = store_factory or (lambda: ChatRunStore(lease_seconds=self.settings.lease_seconds))
        self.executor_factory = executor_factory or self._default_executor
        self.heartbeat = heartbeat
        self.clock = clock
        self.instance_id = instance_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._ready = threading.Event()   # the chat_runs table exists (migrations may run after the worker starts)
        self._threads: list[threading.Thread] = []
        self._last_purge: Optional[datetime] = None
        self._busy = 0
        self._busy_lock = threading.Lock()

    def _default_executor(self, store: ChatRunStore) -> ChatRunExecutor:
        return ChatRunExecutor(store, heartbeat_seconds=self.settings.heartbeat_seconds,
                               cancel_poll_seconds=self.settings.cancel_poll_seconds, app=self.app)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("ChatWorker already started")
        maintenance = threading.Thread(target=self._maintenance_loop, name="chat-worker-maintenance", daemon=True)
        self._threads.append(maintenance)
        for index in range(self.settings.concurrency):
            slot = threading.Thread(target=self._slot_loop, args=(self.worker_id(index),),
                                    name=f"chat-worker-slot-{index}", daemon=True)
            self._threads.append(slot)
        for thread in self._threads:
            thread.start()
        LOG.info("[ChatWorker] started instance=%s slots=%s poll=%ss", self.instance_id,
                 self.settings.concurrency, self.settings.poll_seconds)

    def stop(self) -> None:
        """Stop claiming new runs; runs in progress keep going until they finish."""
        if not self._stop.is_set():
            LOG.info("[ChatWorker] stopping instance=%s busy=%s", self.instance_id, self.busy)
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def busy(self) -> int:
        with self._busy_lock:
            return self._busy

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until ``stop()`` is called (or the timeout passes)."""
        return self._stop.wait(timeout)

    def join(self, timeout: Optional[float] = None) -> bool:
        """Wait for every thread (in-flight runs included). Returns whether all of them finished."""
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in self._threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        alive = [thread.name for thread in self._threads if thread.is_alive()]
        if alive:
            LOG.warning("[ChatWorker] still running after the grace period: %s", alive)
        return not alive

    def worker_id(self, slot: int) -> str:
        return f"{PROCESS_CHAT_WORKER}:{self.instance_id}:s{slot}"[:120]

    # ── slots ──────────────────────────────────────────────────────────────

    def _slot_loop(self, worker_id: str) -> None:
        while not self._stop.is_set():
            if not self._ready.is_set():
                self._stop.wait(self.settings.poll_seconds)
                continue
            try:
                processed = self.process_one(worker_id)
            except Exception:  # noqa: BLE001 - a slot must survive anything (DB restarts, bugs)
                LOG.exception("[ChatWorker] slot %s failed; backing off", worker_id)
                self._stop.wait(self.settings.poll_seconds * _ERROR_BACKOFF_FACTOR)
                continue
            if not processed:
                self._stop.wait(self.settings.poll_seconds)

    def process_one(self, worker_id: str) -> bool:
        """Claim one queued run and execute it to a terminal state. Returns whether a run was claimed."""
        if self._stop.is_set():
            return False
        with self.app.app_context():
            try:
                store = self.store_factory()
                run = store.claim_next(worker_id)
                if run is None:
                    return False
                run_id = run.id
                waited = self._seconds_between(run.created_at, run.claimed_at)
                with self._busy_lock:
                    self._busy += 1
                started = time.monotonic()
                try:
                    status = self.executor_factory(store).execute(run_id, worker_id)
                finally:
                    with self._busy_lock:
                        self._busy -= 1
                LOG.info("[ChatWorker] run=%s status=%s queue_wait_ms=%s run_ms=%s", run_id, status,
                         None if waited is None else int(waited * 1000), int((time.monotonic() - started) * 1000))
                return True
            except Exception:
                db.session.rollback()
                raise
            finally:
                db.session.remove()

    @staticmethod
    def _seconds_between(start, end) -> Optional[float]:
        if start is None or end is None:
            return None
        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        return max(0.0, (end - start).total_seconds())

    # ── maintenance ────────────────────────────────────────────────────────

    def _maintenance_loop(self) -> None:
        if self.heartbeat is None:
            try:
                self.heartbeat = default_heartbeat_recorder(self.settings)
            except Exception:  # noqa: BLE001 - liveness must never stop the worker
                LOG.warning("[ChatWorker] heartbeat unavailable", exc_info=True)
        while not self._stop.is_set():
            self.maintain()
            self._stop.wait(self.settings.maintenance_seconds)

    def maintain(self) -> Dict[str, object]:
        """One maintenance pass: ``{phase: result or exception}``. A failing phase never stops the others."""
        outcome: Dict[str, object] = {}
        with self.app.app_context():
            try:
                if not self._ready.is_set():
                    if not inspect(db.engine).has_table(ChatRun.__tablename__):
                        return outcome
                    self._ready.set()
                if self.heartbeat is not None:
                    self.heartbeat.beat()
                store = self.store_factory()
                now = self.clock()
                phases = [
                    ("recover", lambda: store.recover_stale(now=now)),
                    ("expire", lambda: store.expire_queued(older_than_seconds=self.settings.queue_timeout_seconds,
                                                           now=now)),
                ]
                if self._last_purge is None or now - self._last_purge >= PURGE_EVERY:
                    phases.append(("purge", lambda: store.purge_finished(
                        before=now - timedelta(days=self.settings.retention_days))))
                for name, phase in phases:
                    try:
                        outcome[name] = phase()
                    except Exception as exc:  # noqa: BLE001
                        db.session.rollback()
                        outcome[name] = exc
                        LOG.exception("[ChatWorker] maintenance phase %s failed", name)
                if "purge" in outcome and not isinstance(outcome["purge"], Exception):
                    self._last_purge = now
                if self.heartbeat is not None:
                    self.heartbeat.beat(outcome)
            except Exception:  # noqa: BLE001 - e.g. the database is restarting: try again next pass
                db.session.rollback()
                LOG.exception("[ChatWorker] maintenance pass failed")
            finally:
                db.session.remove()
        return outcome

    @property
    def ready(self) -> bool:
        return self._ready.is_set()


def start_embedded_worker(app, settings: Optional[ChatWorkerSettings] = None) -> ChatWorker:
    """Development convenience: run the worker inside the web process (daemon threads)."""
    import atexit

    worker = ChatWorker(app, settings or ChatWorkerSettings.from_env())
    worker.start()
    atexit.register(worker.stop)
    return worker
