"""Persistence of process heartbeats (``process_heartbeats``): the only module that talks SQLAlchemy about them.

One row per process instance. ``record`` is an upsert that works the same in SQLite and PostgreSQL without
dialect-specific SQL: UPDATE first, INSERT when the instance is new, and UPDATE again when a concurrent beat of
the same instance inserted first (``IntegrityError`` on the primary key). Each method commits its own work.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import ProcessHeartbeat

from .heartbeat import Beat, HeartbeatView
from .schedule_rules import as_utc


def _naive(value: datetime) -> datetime:
    return as_utc(value).replace(tzinfo=None)


def _view(row: ProcessHeartbeat) -> HeartbeatView:
    return HeartbeatView(
        instance_id=row.instance_id, process=row.process, host=row.host, pid=row.pid,
        started_at=as_utc(row.started_at), last_seen_at=as_utc(row.last_seen_at),
        interval_seconds=row.interval_seconds, last_outcome=row.last_outcome, details=row.details_json or {})


class HeartbeatStore:
    def record(self, beat: Beat) -> None:
        identity = beat.identity
        values = {"last_seen_at": _naive(beat.seen_at), "interval_seconds": beat.interval_seconds,
                  "host": identity.host, "pid": identity.pid}
        if beat.outcome is not None:
            values.update(last_outcome=beat.outcome, details_json=dict(beat.details or {}))
        try:
            if self._update(identity.instance_id, values):
                db.session.commit()
                return
            db.session.add(ProcessHeartbeat(instance_id=identity.instance_id, process=identity.process,
                                            started_at=_naive(identity.started_at), **values))
            db.session.commit()
        except IntegrityError:  # the same instance inserted concurrently: its row exists now
            db.session.rollback()
            self._update(identity.instance_id, values)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    def latest(self, process: str) -> HeartbeatView | None:
        row = db.session.execute(
            select(ProcessHeartbeat).where(ProcessHeartbeat.process == process)
            .order_by(ProcessHeartbeat.last_seen_at.desc()).limit(1)
            .execution_options(populate_existing=True)).scalar_one_or_none()
        view = None if row is None else _view(row)
        db.session.rollback()  # end the read transaction
        return view

    def prune(self, process: str, *, older_than: datetime, keep_instance_id: str) -> int:
        try:
            removed = db.session.execute(
                delete(ProcessHeartbeat).where(ProcessHeartbeat.process == process,
                                               ProcessHeartbeat.last_seen_at < _naive(older_than),
                                               ProcessHeartbeat.instance_id != keep_instance_id)).rowcount
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return removed

    @staticmethod
    def _update(instance_id: str, values: dict) -> bool:
        return db.session.execute(update(ProcessHeartbeat).where(ProcessHeartbeat.instance_id == instance_id)
                                  .values(**values)).rowcount == 1
