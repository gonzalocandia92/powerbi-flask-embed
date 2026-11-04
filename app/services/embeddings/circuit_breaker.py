"""In-process circuit breaker for the query embedding provider.

Report sections run in parallel: when Voyage is down, every section would otherwise spend its full retry
deadline on the same outage. After ``failure_threshold`` failed queries (transient failures only, after
their own retries) inside ``window_seconds`` the breaker opens for ``open_seconds`` and callers fail fast;
then a single probe is let through (half-open) and its outcome closes or reopens the circuit.

Per process, deliberately: no shared storage, and a restart simply starts closed.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Callable, Mapping, Optional

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_WINDOW_SECONDS = 60.0
DEFAULT_OPEN_SECONDS = 30.0

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
                 window_seconds: float = DEFAULT_WINDOW_SECONDS, open_seconds: float = DEFAULT_OPEN_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        # ``failure_threshold <= 0`` disables the breaker (always closed).
        self.failure_threshold = int(failure_threshold)
        self.window_seconds = float(window_seconds)
        self.open_seconds = float(open_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: deque = deque()
        self._state = STATE_CLOSED
        self._opened_at = 0.0
        self._probe_in_flight = False

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "CircuitBreaker":
        env = os.environ if env is None else env

        def number(name, default, kind):
            try:
                return kind(env.get(name) or default)
            except (TypeError, ValueError):
                return default

        return cls(
            failure_threshold=number("QUERY_EMBEDDING_BREAKER_FAILURES", DEFAULT_FAILURE_THRESHOLD, int),
            window_seconds=max(1.0, number("QUERY_EMBEDDING_BREAKER_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS, float)),
            open_seconds=max(1.0, number("QUERY_EMBEDDING_BREAKER_OPEN_SECONDS", DEFAULT_OPEN_SECONDS, float)),
        )

    @property
    def state(self) -> str:
        with self._lock:
            self._refresh()
            return self._state

    def _refresh(self) -> None:
        if self._state == STATE_OPEN and self._clock() - self._opened_at >= self.open_seconds:
            self._state = STATE_HALF_OPEN
            self._probe_in_flight = False

    def allow(self) -> bool:
        if self.failure_threshold <= 0:
            return True
        with self._lock:
            self._refresh()
            if self._state == STATE_CLOSED:
                return True
            if self._state == STATE_HALF_OPEN and not self._probe_in_flight:
                self._probe_in_flight = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures.clear()
            self._state = STATE_CLOSED
            self._probe_in_flight = False

    def release(self) -> None:
        """Outcome that says nothing about provider health (e.g. a bad request): free a half-open probe."""
        with self._lock:
            self._probe_in_flight = False

    def record_failure(self) -> None:
        if self.failure_threshold <= 0:
            return
        with self._lock:
            now = self._clock()
            if self._state == STATE_HALF_OPEN:
                self._open(now)
                return
            self._failures.append(now)
            while self._failures and now - self._failures[0] > self.window_seconds:
                self._failures.popleft()
            if len(self._failures) >= self.failure_threshold:
                self._open(now)

    def _open(self, now: float) -> None:
        self._state = STATE_OPEN
        self._opened_at = now
        self._probe_in_flight = False
        self._failures.clear()


_SHARED_LOCK = threading.Lock()
_SHARED: Optional[CircuitBreaker] = None


def shared_query_breaker() -> CircuitBreaker:
    """Process-wide breaker for query embeddings (lazily configured from the environment)."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = CircuitBreaker.from_env()
        return _SHARED


def reset_shared_query_breaker() -> None:
    """Forget the shared breaker (tests, or after changing its environment configuration)."""
    global _SHARED
    with _SHARED_LOCK:
        _SHARED = None
