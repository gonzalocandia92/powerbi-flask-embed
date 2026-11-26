"""Configuration of the chat worker: defaults in code, overridable by environment variables.

Invalid values fall back to the default with a warning (a tuning knob must never stop the worker), and
out-of-range values are clamped. See the table in ``docs/klara-chat-asincrono.md``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from .store import DEFAULT_LEASE_SECONDS

LOG = logging.getLogger(__name__)

ENV_CONCURRENCY = "CHAT_WORKER_CONCURRENCY"
ENV_POLL_SECONDS = "CHAT_RUN_POLL_SECONDS"
ENV_LEASE_SECONDS = "CHAT_RUN_LEASE_SECONDS"
ENV_HEARTBEAT_SECONDS = "CHAT_RUN_HEARTBEAT_SECONDS"
ENV_CANCEL_POLL_SECONDS = "CHAT_RUN_CANCEL_POLL_SECONDS"
ENV_QUEUE_TIMEOUT_SECONDS = "CHAT_RUN_QUEUE_TIMEOUT_SECONDS"
ENV_RETENTION_DAYS = "CHAT_RUN_RETENTION_DAYS"
ENV_MAINTENANCE_SECONDS = "CHAT_WORKER_MAINTENANCE_SECONDS"
ENV_SHUTDOWN_GRACE_SECONDS = "CHAT_WORKER_SHUTDOWN_GRACE_SECONDS"
ENV_MAX_OPEN_PER_OWNER = "CHAT_RUN_MAX_OPEN_PER_OWNER"
ENV_MAX_QUEUED = "CHAT_RUN_MAX_QUEUED"
ENV_POLL_AFTER_MS = "CHAT_RUN_POLL_AFTER_MS"
ENV_RATE_LIMIT = "CHAT_RUN_RATE_LIMIT"
ENV_MAX_QUESTION_CHARS = "CHAT_RUN_MAX_QUESTION_CHARS"

DEFAULT_RATE_LIMIT = "20 per minute"


def _parse(env: Mapping[str, str], name: str, kind: Callable, default, minimum, maximum):
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = kind(str(raw).strip())
    except ValueError:
        LOG.warning("Invalid %s=%r ignored; using %s", name, raw, default)
        return default
    return min(max(value, minimum), maximum)


@dataclass(frozen=True)
class ChatWorkerSettings:
    concurrency: int = 4                    # runs executed at the same time by one worker process
    poll_seconds: float = 1.0               # wait when the queue is empty (a chat is interactive)
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    heartbeat_seconds: Optional[float] = None   # None: min(20 s, lease / 3)
    cancel_poll_seconds: float = 2.0        # how fast a cancellation stops a running turn
    queue_timeout_seconds: int = 120        # a run queued longer than this expires (its user gave up)
    retention_days: int = 7                 # finished runs are purged after this (the chat stays in chat_messages)
    maintenance_seconds: float = 15.0       # recovery / expiry / purge cadence
    shutdown_grace_seconds: float = 110.0   # wait for in-flight turns on SIGTERM (compose grants 120 s)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ChatWorkerSettings":
        env = os.environ if env is None else env
        lease = _parse(env, ENV_LEASE_SECONDS, int, DEFAULT_LEASE_SECONDS, 30, 3600)
        raw_heartbeat = env.get(ENV_HEARTBEAT_SECONDS)
        heartbeat = None
        if raw_heartbeat is not None and str(raw_heartbeat).strip():
            heartbeat = _parse(env, ENV_HEARTBEAT_SECONDS, float, None, 1.0, lease / 2)
        return cls(
            concurrency=_parse(env, ENV_CONCURRENCY, int, 4, 1, 32),
            poll_seconds=_parse(env, ENV_POLL_SECONDS, float, 1.0, 0.1, 30.0),
            lease_seconds=lease,
            heartbeat_seconds=heartbeat,
            cancel_poll_seconds=_parse(env, ENV_CANCEL_POLL_SECONDS, float, 2.0, 0.1, 30.0),
            queue_timeout_seconds=_parse(env, ENV_QUEUE_TIMEOUT_SECONDS, int, 120, 10, 3600),
            retention_days=_parse(env, ENV_RETENTION_DAYS, int, 7, 1, 365),
            maintenance_seconds=_parse(env, ENV_MAINTENANCE_SECONDS, float, 15.0, 1.0, 600.0),
            shutdown_grace_seconds=_parse(env, ENV_SHUTDOWN_GRACE_SECONDS, float, 110.0, 0.0, 3600.0),
        )


@dataclass(frozen=True)
class ChatRunApiSettings:
    """What the web process enforces when it enqueues and what it tells the browser."""

    max_open_per_owner: int = 1     # 409 beyond this: a double click / second tab resumes the open run instead
    max_queued: int = 50            # 503 beyond this: back-pressure on the whole queue
    poll_after_ms: int = 1000       # suggested polling interval for the browser
    rate_limit: str = DEFAULT_RATE_LIMIT   # POST /chat/runs per client IP (Flask-Limiter syntax)
    max_question_chars: int = 4000

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ChatRunApiSettings":
        env = os.environ if env is None else env
        rate_limit = str(env.get(ENV_RATE_LIMIT) or "").strip() or DEFAULT_RATE_LIMIT
        return cls(
            max_open_per_owner=_parse(env, ENV_MAX_OPEN_PER_OWNER, int, 1, 1, 20),
            max_queued=_parse(env, ENV_MAX_QUEUED, int, 50, 1, 100000),
            poll_after_ms=_parse(env, ENV_POLL_AFTER_MS, int, 1000, 250, 30000),
            rate_limit=rate_limit,
            max_question_chars=_parse(env, ENV_MAX_QUESTION_CHARS, int, 4000, 100, 100000),
        )
