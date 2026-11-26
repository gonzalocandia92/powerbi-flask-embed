"""Durable queue of KLARA chat questions (``chat_runs``): the only module that talks SQLAlchemy about them.

Same guarantees as the report queue (``app/services/reporting/run_store.py``), re-implemented on its own
table so the two queues evolve independently:

* **Atomic claim**: ``SELECT ... FOR UPDATE SKIP LOCKED`` picks a candidate and a compare-and-swap
  ``UPDATE ... WHERE status = 'queued'`` takes it, so two workers never own the same run (the CAS also
  protects databases that ignore row locks, such as the SQLite used in tests).
* **Fenced writes**: every write after the claim requires ``worker_id`` to still own an active run, so a
  worker that lost its lease can never overwrite the outcome.
* **No automatic re-run**: an interrupted run is closed as ``failed`` / ``worker_lost``, never re-queued,
  because running it again would repeat paid model calls.
* **Owner-scoped reads**: a run is only returned to (or cancelled by) the owner that created it.

The session is injectable (``session=``); by default the Flask-SQLAlchemy scoped session is used.
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, select, text, update

from app.models import ChatRun
from app.persistence.chat_run_states import (
    ACTIVE_STATUSES, ERROR_CANCELLED, ERROR_CODES, ERROR_EXPIRED, ERROR_WORKER_LOST, OPEN_STATUSES,
    STAGE_PREPARING, STAGE_QUEUED, STAGES, STATUS_CANCEL_REQUESTED, STATUS_CANCELLED, STATUS_EXPIRED,
    STATUS_FAILED, STATUS_QUEUED, STATUS_RUNNING, TERMINAL_STATUSES, WORKER_FINAL_STATUSES,
)

from .contracts import (
    LIMIT_OWNER_OPEN_RUNS, LIMIT_QUEUE_FULL, ChatRunCancelledError, ChatRunLeaseLostError, ChatRunLimitError,
    ChatRunRequest,
)

LOG = logging.getLogger(__name__)

# A chat turn heartbeats every ~20 s; 90 s without one means the worker is gone.
DEFAULT_LEASE_SECONDS = 90
DEFAULT_PURGE_BATCH_SIZE = 500

WORKER_LOST_MESSAGE = ("Se interrumpió el proceso que estaba respondiendo. La consulta no se repitió "
                       "para evitar un doble cobro; podés volver a enviarla.")
EXPIRED_MESSAGE = "La consulta esperó demasiado en la cola y no se procesó. Volvé a enviarla en unos instantes."
CANCELLED_MESSAGE = "La consulta fue cancelada."


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChatRunStore:
    def __init__(self, *, lease_seconds: int = DEFAULT_LEASE_SECONDS, session=None):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.lease_seconds = lease_seconds
        self._session = session

    @property
    def session(self):
        if self._session is not None:
            return self._session
        from app import db  # lazy: the store stays importable (and injectable) without the Flask app
        return db.session

    # ── enqueue ────────────────────────────────────────────────────────────

    def enqueue(self, request: ChatRunRequest, *, max_open_per_owner: Optional[int] = None,
                max_queued: Optional[int] = None) -> ChatRun:
        """Create a ``queued`` run, unless a limit refuses it (``ChatRunLimitError``).

        ``max_open_per_owner`` caps the owner's open runs (double click, several tabs); the refusal carries
        the open run so the caller can resume it instead of paying twice. ``max_queued`` is back-pressure on
        the whole queue. On PostgreSQL the owner check and the insert are serialized per owner with a
        transaction-scoped advisory lock, so two simultaneous requests cannot both pass the check.
        """
        for name, value in (("max_open_per_owner", max_open_per_owner), ("max_queued", max_queued)):
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1")
        session = self.session
        try:
            self._lock_owner(request.owner_hash)
            if max_open_per_owner is not None:
                open_ids = session.execute(
                    select(ChatRun.id).where(ChatRun.owner_hash == request.owner_hash,
                                             ChatRun.status.in_(OPEN_STATUSES))
                    .order_by(ChatRun.created_at.asc(), ChatRun.id.asc())
                ).scalars().all()
                if len(open_ids) >= max_open_per_owner:
                    raise ChatRunLimitError(LIMIT_OWNER_OPEN_RUNS, "Ya hay una consulta en curso.",
                                            open_run_id=open_ids[0])
            if max_queued is not None and self._count_queued(session) >= max_queued:
                raise ChatRunLimitError(LIMIT_QUEUE_FULL, "Hay demasiadas consultas en espera.")
            run = ChatRun(
                slug=request.slug.strip(), question=request.question.strip(), owner_hash=request.owner_hash,
                conversation_id_in=request.conversation_id, model_key_in=request.model_key,
                reset_history=bool(request.reset_history), is_staff=bool(request.is_staff),
                user_key=request.user_key, source=request.source, report_id_fk=request.report_id,
                status=STATUS_QUEUED, stage=STAGE_QUEUED, created_at=_utcnow(),
            )
            session.add(run)
            session.commit()
            return run
        except Exception:
            session.rollback()
            raise

    def _lock_owner(self, owner_hash: str) -> None:
        session = self.session
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"chat_runs:{owner_hash}"})

    @staticmethod
    def request_from_run(run: ChatRun) -> ChatRunRequest:
        """The frozen request of a run, as the worker needs it."""
        return ChatRunRequest(
            slug=run.slug, question=run.question, owner_hash=run.owner_hash,
            conversation_id=run.conversation_id_in, model_key=run.model_key_in,
            reset_history=bool(run.reset_history), is_staff=bool(run.is_staff),
            user_key=run.user_key, source=run.source, report_id=run.report_id_fk,
        )

    # ── claim / lease ──────────────────────────────────────────────────────

    def claim_next(self, worker_id: str, *, now: Optional[datetime] = None, attempts: int = 3) -> Optional[ChatRun]:
        """Atomically move the oldest ``queued`` run to ``running`` for ``worker_id``."""
        session = self.session
        for _ in range(attempts):
            current = now or _utcnow()
            try:
                candidate = session.execute(
                    select(ChatRun.id).where(ChatRun.status == STATUS_QUEUED)
                    .order_by(ChatRun.created_at.asc(), ChatRun.id.asc())
                    .limit(1).with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if candidate is None:
                    session.rollback()
                    return None
                claimed = session.execute(
                    update(ChatRun)
                    .where(ChatRun.id == candidate, ChatRun.status == STATUS_QUEUED)
                    .values(status=STATUS_RUNNING, stage=STAGE_PREPARING, worker_id=worker_id,
                            claimed_at=current, heartbeat_at=current,
                            lease_expires_at=current + timedelta(seconds=self.lease_seconds))
                ).rowcount
                session.commit()
            except Exception:
                session.rollback()
                raise
            if claimed == 1:
                session.expire_all()
                return session.get(ChatRun, candidate)
        return None

    def _fenced_update(self, run_id: str, worker_id: str, **values) -> None:
        """UPDATE only while ``worker_id`` still owns an active run; otherwise the worker is told to stop."""
        session = self.session
        now = _utcnow()
        try:
            changed = session.execute(
                update(ChatRun)
                .where(ChatRun.id == run_id, ChatRun.worker_id == worker_id, ChatRun.status.in_(ACTIVE_STATUSES))
                .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=self.lease_seconds), **values)
            ).rowcount
            session.commit()
        except Exception:
            session.rollback()
            raise
        if changed != 1:
            raise ChatRunLeaseLostError(f"Worker {worker_id} no longer owns chat run {run_id}")

    def heartbeat(self, run_id: str, worker_id: str) -> None:
        self._fenced_update(run_id, worker_id)

    def set_stage(self, run_id: str, worker_id: str, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"Unknown chat run stage: {stage!r}")
        self._fenced_update(run_id, worker_id, stage=stage)

    def finish(self, run_id: str, worker_id: str, *, status: str, result: Optional[dict] = None,
               error_code: Optional[str] = None, error_message: Optional[str] = None,
               diagnostic_id: Optional[str] = None) -> None:
        """Close a run the worker owns. ``result`` must be the PUBLIC payload only."""
        if status not in WORKER_FINAL_STATUSES:
            raise ValueError(f"Not a status a worker can finish with: {status!r}")
        if error_code is not None and error_code not in ERROR_CODES:
            raise ValueError(f"Unknown chat run error code: {error_code!r}")
        session = self.session
        now = _utcnow()
        try:
            changed = session.execute(
                update(ChatRun)
                .where(ChatRun.id == run_id, ChatRun.worker_id == worker_id, ChatRun.status.in_(ACTIVE_STATUSES))
                .values(status=status, result_json=result, error_code=error_code, error_message=error_message,
                        diagnostic_id=diagnostic_id, finished_at=now, heartbeat_at=now, lease_expires_at=None)
            ).rowcount
            session.commit()
        except Exception:
            session.rollback()
            raise
        if changed != 1:
            raise ChatRunLeaseLostError(f"Worker {worker_id} no longer owns chat run {run_id}")

    # ── cancellation ───────────────────────────────────────────────────────

    def request_cancel(self, run_id: str, owner_hash: str) -> Optional[str]:
        """Cancel a queued run at once, or flag a running one. Only the owner can.

        Returns the resulting status, or ``None`` when the run does not exist, belongs to someone else or
        has already finished.
        """
        if not run_id or not owner_hash:
            return None
        session = self.session
        now = _utcnow()
        mine = (ChatRun.id == run_id, ChatRun.owner_hash == owner_hash)
        try:
            if session.execute(
                update(ChatRun).where(*mine, ChatRun.status == STATUS_QUEUED)
                .values(status=STATUS_CANCELLED, cancel_requested_at=now, finished_at=now,
                        error_code=ERROR_CANCELLED, error_message=CANCELLED_MESSAGE)
            ).rowcount:
                session.commit()
                return STATUS_CANCELLED
            if session.execute(
                update(ChatRun).where(*mine, ChatRun.status == STATUS_RUNNING)
                .values(status=STATUS_CANCEL_REQUESTED, cancel_requested_at=now)
            ).rowcount:
                session.commit()
                return STATUS_CANCEL_REQUESTED
            status = session.execute(select(ChatRun.status).where(*mine)).scalar_one_or_none()
            session.rollback()
        except Exception:
            session.rollback()
            raise
        return STATUS_CANCEL_REQUESTED if status == STATUS_CANCEL_REQUESTED else None

    def raise_if_cancel_requested(self, run_id: str) -> None:
        """Cooperative stop point for the worker (between agent rounds)."""
        session = self.session
        status = session.execute(select(ChatRun.status).where(ChatRun.id == run_id)).scalar_one_or_none()
        session.rollback()  # end the read transaction so the next check sees fresh data
        if status == STATUS_CANCEL_REQUESTED:
            raise ChatRunCancelledError(run_id)

    # ── maintenance ────────────────────────────────────────────────────────

    def recover_stale(self, *, now: Optional[datetime] = None) -> list[str]:
        """Close runs whose worker disappeared (expired lease). Deliberately NOT re-queued."""
        session = self.session
        now = now or _utcnow()
        try:
            stale = session.execute(
                select(ChatRun.id, ChatRun.status).where(
                    ChatRun.status.in_(ACTIVE_STATUSES), ChatRun.lease_expires_at.isnot(None),
                    ChatRun.lease_expires_at < now)
            ).all()
            recovered = []
            for run_id, status in stale:
                cancelled = status == STATUS_CANCEL_REQUESTED
                changed = session.execute(
                    update(ChatRun)
                    .where(ChatRun.id == run_id, ChatRun.status == status, ChatRun.lease_expires_at < now)
                    .values(status=STATUS_CANCELLED if cancelled else STATUS_FAILED,
                            error_code=ERROR_CANCELLED if cancelled else ERROR_WORKER_LOST,
                            error_message=CANCELLED_MESSAGE if cancelled else WORKER_LOST_MESSAGE,
                            finished_at=now, lease_expires_at=None)
                ).rowcount
                if changed:
                    recovered.append(run_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        if recovered:
            LOG.warning("[ChatRunStore] Closed interrupted chat runs: %s", recovered)
        return recovered

    def expire_queued(self, *, older_than_seconds: float, now: Optional[datetime] = None) -> list[str]:
        """Close ``queued`` runs that waited longer than ``older_than_seconds``: their user already gave up."""
        if older_than_seconds <= 0:
            raise ValueError("older_than_seconds must be positive")
        session = self.session
        now = now or _utcnow()
        cutoff = now - timedelta(seconds=older_than_seconds)
        try:
            candidates = session.execute(
                select(ChatRun.id).where(ChatRun.status == STATUS_QUEUED, ChatRun.created_at < cutoff)
            ).scalars().all()
            expired = []
            for run_id in candidates:
                if session.execute(
                    update(ChatRun).where(ChatRun.id == run_id, ChatRun.status == STATUS_QUEUED)
                    .values(status=STATUS_EXPIRED, error_code=ERROR_EXPIRED, error_message=EXPIRED_MESSAGE,
                            finished_at=now)
                ).rowcount:
                    expired.append(run_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        if expired:
            LOG.warning("[ChatRunStore] Expired queued chat runs: %s", expired)
        return expired

    def purge_finished(self, *, before: datetime, batch_size: int = DEFAULT_PURGE_BATCH_SIZE) -> int:
        """Delete terminal runs finished before ``before``, in batches. The conversation stays in chat_messages."""
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        session = self.session
        total = 0
        while True:
            try:
                ids = session.execute(
                    select(ChatRun.id).where(ChatRun.status.in_(TERMINAL_STATUSES), ChatRun.finished_at < before)
                    .limit(batch_size)
                ).scalars().all()
                if ids:
                    session.execute(delete(ChatRun).where(ChatRun.id.in_(ids)))
                session.commit()
            except Exception:
                session.rollback()
                raise
            total += len(ids)
            if len(ids) < batch_size:
                return total

    # ── reads ──────────────────────────────────────────────────────────────

    def get(self, run_id: str) -> Optional[ChatRun]:
        return self.session.get(ChatRun, run_id) if run_id else None

    def get_for_owner(self, run_id: str, owner_hash: Optional[str]) -> Optional[ChatRun]:
        """The run only when ``owner_hash`` is the one that created it (else ``None``, as if it did not exist)."""
        if not run_id or not owner_hash:
            return None
        run = self.session.get(ChatRun, run_id)
        if run is None or not hmac.compare_digest(str(run.owner_hash), str(owner_hash)):
            return None
        return run

    def open_count_for_owner(self, owner_hash: str) -> int:
        return int(self.session.execute(
            select(func.count()).select_from(ChatRun)
            .where(ChatRun.owner_hash == owner_hash, ChatRun.status.in_(OPEN_STATUSES))
        ).scalar_one())

    def queued_count(self) -> int:
        return self._count_queued(self.session)

    @staticmethod
    def _count_queued(session) -> int:
        return int(session.execute(
            select(func.count()).select_from(ChatRun).where(ChatRun.status == STATUS_QUEUED)
        ).scalar_one())
