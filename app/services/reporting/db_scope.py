"""Own SQLAlchemy session for a unit of concurrent work.

Flask-SQLAlchemy scopes ``db.session`` to the Flask application context, and ``asyncio`` tasks and
``asyncio.to_thread`` copy the current context. Without this, two concurrent analyses (and the
heartbeat) would share ONE ``Session`` across threads, and one's ``commit()``/``rollback()`` would
hit the other's pending work. Entering ``isolated_db_scope()`` inside a task pushes a fresh app
context for that task only (its context copy), so everything it spawns uses its own session.
Outside an app context (CLI, pure unit tests) it does nothing.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from flask import current_app, has_app_context

from app import db


@contextmanager
def isolated_db_scope() -> Iterator[None]:
    if not has_app_context():
        yield
        return
    app = current_app._get_current_object()
    with app.app_context():
        try:
            yield
        finally:
            db.session.remove()
