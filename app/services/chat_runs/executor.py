"""Executes one claimed chat run: the turn, its lease, its cancellation and its public outcome.

Orchestration only. The turn itself comes from an injected ``ChatTurnHandler`` (by default the same
``chatbot_service`` turn the synchronous route runs) and its outcome is mapped by ``chat_public``, the
single source shared with ``POST /chat``. It knows nothing about routes, polling or the worker loop.

Lease and cancellation live in a watcher THREAD, not in the event loop: a turn that blocks its loop (a
synchronous call) can never starve the heartbeat. When the lease is lost or the owner cancels, the watcher
cancels the turn's asyncio task (``CancelledError`` is not an ``Exception``, so the service's own error
handling does not swallow it and no error turn is recorded):

* lease lost  -> the outcome is discarded; the run belongs to whoever closed it (never overwritten);
* cancelled   -> the run ends ``cancelled``.

Must run inside a Flask app context (or receive ``app=``) when the handler needs one.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from flask import current_app, has_app_context

from app.persistence.chat_run_states import (
    ACTIVE_STATUSES, ERROR_CANCELLED, STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED,
)
from app.services.chat_public import public_error, public_payload

from .contracts import ChatRunCancelledError, ChatRunLeaseLostError, ChatRunRequest, ChatTurnHandler
from .handler import ChatbotServiceTurnHandler
from .store import CANCELLED_MESSAGE, DEFAULT_LEASE_SECONDS, ChatRunStore

LOG = logging.getLogger(__name__)

DEFAULT_CANCEL_POLL_SECONDS = 2.0
_WATCHER_JOIN_SECONDS = 5.0

STOP_LEASE_LOST = "lease_lost"
STOP_CANCELLED = "cancelled"


def default_heartbeat_seconds(lease_seconds: float) -> float:
    """Renew well inside the lease: a third of it, at most 20 s."""
    return max(0.05, min(20.0, lease_seconds / 3))


def _remove_session(store: ChatRunStore) -> None:
    remove = getattr(store.session, "remove", None)  # scoped session (Flask-SQLAlchemy); plain sessions have none
    if callable(remove):
        remove()


class _AppScope:
    """Run a block in its own app context (own scoped session), or as-is without an app."""

    def __init__(self, app, store: ChatRunStore):
        self.app, self.store, self._ctx = app, store, None

    def __enter__(self):
        if self.app is not None:
            self._ctx = self.app.app_context()
            self._ctx.push()
        return self

    def __exit__(self, *exc):
        if self._ctx is not None:
            try:
                _remove_session(self.store)
            finally:
                self._ctx.pop()
        return False


class DurableProgressSink:
    """``ProgressSink`` backed by the run row. Writes only on change and never raises into the agent."""

    def __init__(self, store: ChatRunStore, run_id: str, worker_id: str, *, app=None):
        self.store, self.run_id, self.worker_id, self.app = store, run_id, worker_id, app
        self._last: Optional[str] = None
        self._lock = threading.Lock()

    def stage(self, code: str) -> None:
        with self._lock:
            if code == self._last:
                return
            try:
                # Own app context = own session: never commits or rolls back the turn's pending work.
                with _AppScope(self.app, self.store):
                    self.store.set_stage(self.run_id, self.worker_id, code)
                self._last = code
            except Exception:  # noqa: BLE001 - progress is best effort; the watcher handles a lost lease
                LOG.warning("[ChatRun] run=%s could not record stage %s", self.run_id, code, exc_info=True)


class _LeaseWatcher(threading.Thread):
    """Heartbeats the lease and polls for cancellation; on either stop condition calls ``on_stop`` once."""

    def __init__(self, *, store: ChatRunStore, run_id: str, worker_id: str, app, heartbeat_seconds: float,
                 cancel_poll_seconds: float, on_stop: Callable[[], None]):
        super().__init__(name=f"chat-run-watcher-{run_id[:8]}", daemon=True)
        self.store, self.run_id, self.worker_id, self.app = store, run_id, worker_id, app
        self.heartbeat_seconds, self.cancel_poll_seconds, self.on_stop = heartbeat_seconds, cancel_poll_seconds, on_stop
        self.reason: Optional[str] = None
        self._halt = threading.Event()

    def halt(self) -> None:
        self._halt.set()

    def run(self) -> None:
        tick = min(self.heartbeat_seconds, self.cancel_poll_seconds)
        next_beat = time.monotonic() + self.heartbeat_seconds
        with _AppScope(self.app, self.store):
            while not self._halt.wait(tick):
                try:
                    self.store.raise_if_cancel_requested(self.run_id)
                    if time.monotonic() >= next_beat:
                        self.store.heartbeat(self.run_id, self.worker_id)
                        next_beat = time.monotonic() + self.heartbeat_seconds
                except ChatRunCancelledError:
                    self._signal_stop(STOP_CANCELLED)
                    return
                except ChatRunLeaseLostError:
                    self._signal_stop(STOP_LEASE_LOST)
                    return
                except Exception:  # noqa: BLE001 - a transient DB error must not kill the watcher
                    LOG.warning("[ChatRun] run=%s heartbeat failed; will retry", self.run_id, exc_info=True)

    def _signal_stop(self, reason: str) -> None:
        self.reason = reason
        LOG.warning("[ChatRun] run=%s stopping turn: %s", self.run_id, reason)
        try:
            self.on_stop()
        except RuntimeError:  # the event loop already finished: nothing left to cancel
            pass


class _TurnStopped(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ChatRunExecutor:
    def __init__(self, store: ChatRunStore, handler: Optional[ChatTurnHandler] = None, *,
                 heartbeat_seconds: Optional[float] = None,
                 cancel_poll_seconds: float = DEFAULT_CANCEL_POLL_SECONDS, app=None):
        self.store = store
        self.handler = handler or ChatbotServiceTurnHandler()
        self.heartbeat_seconds = heartbeat_seconds if heartbeat_seconds is not None else (
            default_heartbeat_seconds(getattr(store, "lease_seconds", DEFAULT_LEASE_SECONDS)))
        self.cancel_poll_seconds = cancel_poll_seconds
        self.app = app

    def execute(self, run_id: str, worker_id: str) -> Optional[str]:
        """Run a claimed run to a terminal state. Returns that status, or ``None`` when it was not ours."""
        run = self.store.get(run_id)
        if run is None or run.worker_id != worker_id or run.status not in ACTIVE_STATUSES:
            return None
        request = self.store.request_from_run(run)
        app = self.app or (current_app._get_current_object() if has_app_context() else None)
        progress = DurableProgressSink(self.store, run_id, worker_id, app=app)
        try:
            result = asyncio.run(self._run_turn(app, request, run_id, worker_id, progress))
            payload = public_payload(result)
        except _TurnStopped as stopped:
            if stopped.reason == STOP_LEASE_LOST:
                LOG.warning("[ChatRun] run=%s lease lost; outcome discarded", run_id)
                return None
            return self._finish(run_id, worker_id, status=STATUS_CANCELLED, error_code=ERROR_CANCELLED,
                                error_message=CANCELLED_MESSAGE)
        except Exception as exc:  # noqa: BLE001 - every failure must end in a terminal state
            error = public_error(exc)
            if error.unexpected:
                LOG.exception("[ChatRun] run=%s failed code=%s", run_id, error.code)
            else:
                LOG.info("[ChatRun] run=%s refused code=%s", run_id, error.code)
            return self._finish(run_id, worker_id, status=STATUS_FAILED, error_code=error.code,
                                error_message=error.message, diagnostic_id=error.diagnostic_id)
        return self._finish(run_id, worker_id, status=STATUS_COMPLETED, result=payload,
                            diagnostic_id=payload.get("diagnostic_id"))

    async def _run_turn(self, app, request: ChatRunRequest, run_id: str, worker_id: str,
                        progress: DurableProgressSink) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(self.handler.handle(request, progress))
        watcher = _LeaseWatcher(
            store=self.store, run_id=run_id, worker_id=worker_id, app=app,
            heartbeat_seconds=self.heartbeat_seconds, cancel_poll_seconds=self.cancel_poll_seconds,
            on_stop=lambda: loop.call_soon_threadsafe(task.cancel))
        watcher.start()
        try:
            return await task
        except asyncio.CancelledError:
            if watcher.reason is not None:
                raise _TurnStopped(watcher.reason) from None
            raise
        finally:
            watcher.halt()
            watcher.join(_WATCHER_JOIN_SECONDS)

    def _finish(self, run_id: str, worker_id: str, *, status: str, **outcome) -> Optional[str]:
        try:
            self.store.finish(run_id, worker_id, status=status, **outcome)
        except ChatRunLeaseLostError:
            LOG.warning("[ChatRun] run=%s lease lost before finishing; outcome discarded", run_id)
            return None
        return status
