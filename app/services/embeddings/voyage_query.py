"""Resilient Voyage query embeddings shared by schema retrieval and skill routing.

One query embedding = at most ``max_attempts`` provider calls inside a bounded total ``deadline``.
Only transient failures (timeouts, connection errors, 429, 5xx) are retried, with exponential
backoff plus jitter; authentication or invalid-request errors fail at once.

The per-attempt timeout lives in the Voyage client itself: ``asyncio.wait_for`` over
``asyncio.to_thread`` stops waiting but does not cancel the worker thread, so without a client
timeout an abandoned call keeps a thread busy. ``wait_for`` stays only as the outer bound.

A process-wide circuit breaker (``circuit_breaker.py``) makes callers fail fast while the provider is down.

Callers keep their own usage accounting and bill a query once (``total_tokens`` on success, an
estimate on failure), never once per attempt.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from app.services.observability import start_observation

from .circuit_breaker import CircuitBreaker, shared_query_breaker

LOG = logging.getLogger(__name__)

DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 6.0
DEFAULT_DEADLINE_SECONDS = 12.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_BACKOFF_MAX_SECONDS = 2.0
# Below this remaining budget another attempt cannot realistically succeed.
MIN_ATTEMPT_SECONDS = 0.5

_TRANSIENT_ERROR_NAMES = frozenset({
    "APIConnectionError", "ServerError", "ServiceUnavailableError", "RateLimitError", "Timeout", "TryAgain",
})
_PERMANENT_ERROR_NAMES = frozenset({
    "AuthenticationError", "InvalidRequestError", "MalformedRequestError",
})


def _env_float(env: Mapping[str, str], name: str, default: float, minimum: float) -> float:
    try:
        value = float(env.get(name) or default)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


@dataclass(frozen=True)
class QueryEmbeddingSettings:
    attempt_timeout_seconds: float = DEFAULT_ATTEMPT_TIMEOUT_SECONDS
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "QueryEmbeddingSettings":
        env = os.environ if env is None else env
        return cls(
            attempt_timeout_seconds=_env_float(env, "QUERY_EMBEDDING_ATTEMPT_TIMEOUT_SECONDS",
                                               DEFAULT_ATTEMPT_TIMEOUT_SECONDS, 1.0),
            deadline_seconds=_env_float(env, "QUERY_EMBEDDING_DEADLINE_SECONDS", DEFAULT_DEADLINE_SECONDS, 1.0),
            max_attempts=int(_env_float(env, "QUERY_EMBEDDING_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, 1)),
        )


@dataclass(frozen=True)
class QueryEmbedding:
    vector: list
    total_tokens: int
    attempts: int


class QueryEmbeddingError(RuntimeError):
    """The query could not be embedded; ``error_type`` is a safe class name, never provider text."""

    def __init__(self, message: str, *, attempts: int, error_type: str, transient: bool):
        super().__init__(message)
        self.attempts = attempts
        self.error_type = error_type
        self.transient = transient

    def metadata(self) -> dict:
        return {"attempts": self.attempts, "error_type": self.error_type, "transient": self.transient}


def is_transient_embedding_error(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__
    if name in _PERMANENT_ERROR_NAMES:
        return False
    if name in _TRANSIENT_ERROR_NAMES:
        return True
    status = getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    # Voyage's generic APIError without a status is a server-side failure.
    return name == "APIError"


_CLIENT_LOCK = threading.Lock()
_CLIENTS: dict = {}


def get_voyage_query_client(*, timeout: Optional[float] = None):
    """Process-wide lazy Voyage client: no SDK retries (we own them) and a real per-call timeout."""
    api_key = os.getenv("VOYAGE_API_KEY")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY is required")
    if timeout is None:
        timeout = QueryEmbeddingSettings.from_env().attempt_timeout_seconds
    key = (api_key, float(timeout))
    with _CLIENT_LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            try:
                import voyageai
            except ImportError as exc:  # pragma: no cover - dependency missing in test env
                raise RuntimeError("The 'voyageai' package is required for query embeddings.") from exc
            client = voyageai.Client(api_key=api_key, max_retries=0, timeout=float(timeout))
            _CLIENTS.clear()  # a rotated key or new timeout replaces the previous client
            _CLIENTS[key] = client
        return client


# Default for ``embed_query(breaker=...)``: the process-wide breaker. Pass ``None`` to disable it.
SHARED_BREAKER: Any = object()


def _backoff_seconds(attempt: int, settings: QueryEmbeddingSettings, rng: Callable[[], float]) -> float:
    base = min(settings.backoff_max_seconds, settings.backoff_base_seconds * (2 ** (attempt - 1)))
    return base * (0.5 + rng() / 2)  # jitter in [50%, 100%]


async def embed_query(
    text: str,
    *,
    model: str,
    client_factory: Optional[Callable[[], Any]] = None,
    settings: Optional[QueryEmbeddingSettings] = None,
    deadline_seconds: Optional[float] = None,
    component: str = "query_embedding",
    breaker: Any = SHARED_BREAKER,
    sleep: Callable[[float], Any] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    rng: Callable[[], float] = random.random,
) -> QueryEmbedding:
    """Embed one query with bounded retries. ``deadline_seconds`` can only shorten the configured deadline."""
    settings = settings or QueryEmbeddingSettings.from_env()
    circuit: Optional[CircuitBreaker] = shared_query_breaker() if breaker is SHARED_BREAKER else breaker
    deadline = settings.deadline_seconds
    if deadline_seconds is not None and deadline_seconds > 0:
        deadline = min(deadline, float(deadline_seconds))
    started = clock()
    attempts = 0
    last_error: Optional[BaseException] = None
    with start_observation(name="voyage-query-embedding-attempts", as_type="span",
                           metadata={"component": component, "model": model}) as span:
        if circuit is not None and not circuit.allow():
            if span is not None:
                span.update(output={"attempts": 0, "status": "error", "error_type": "CircuitOpen", "transient": True})
            raise QueryEmbeddingError("Query embedding circuit is open", attempts=0, error_type="CircuitOpen",
                                      transient=True)
        while attempts < max(1, settings.max_attempts):
            remaining = deadline - (clock() - started)
            if attempts and remaining < MIN_ATTEMPT_SECONDS:
                break
            attempts += 1
            try:
                client = client_factory() if client_factory is not None else get_voyage_query_client(
                    timeout=settings.attempt_timeout_seconds)
                response = await asyncio.wait_for(
                    asyncio.to_thread(lambda: client.embed([text], model=model, input_type="query")),
                    timeout=max(MIN_ATTEMPT_SECONDS, min(settings.attempt_timeout_seconds, remaining)),
                )
                vector = list(response.embeddings[0])
            except Exception as exc:
                last_error = exc
                transient = is_transient_embedding_error(exc)
                LOG.warning("Query embedding attempt failed component=%s attempt=%s error_type=%s transient=%s",
                            component, attempts, type(exc).__name__, transient)
                if not transient:
                    break
                remaining = deadline - (clock() - started)
                if attempts < settings.max_attempts and remaining > MIN_ATTEMPT_SECONDS:
                    await sleep(min(_backoff_seconds(attempts, settings, rng), remaining - MIN_ATTEMPT_SECONDS))
                continue
            if circuit is not None:
                circuit.record_success()
            if span is not None:
                span.update(output={"attempts": attempts, "status": "success"})
            return QueryEmbedding(vector=vector, total_tokens=int(getattr(response, "total_tokens", None) or 0),
                                  attempts=attempts)

        error_type = type(last_error).__name__ if last_error is not None else "DeadlineExceeded"
        transient = last_error is None or is_transient_embedding_error(last_error)
        if circuit is not None:
            # Only provider-health failures trip the breaker; a bad request just frees a half-open probe.
            if transient:
                circuit.record_failure()
            else:
                circuit.release()
        if span is not None:
            span.update(output={"attempts": attempts, "status": "error", "error_type": error_type,
                                "transient": transient})
        raise QueryEmbeddingError("Query embedding failed", attempts=attempts, error_type=error_type,
                                  transient=transient) from last_error
