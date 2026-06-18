"""
Langfuse tracing helpers for the Flask application.

The helpers in this module keep Langfuse optional: if the SDK or credentials
are missing, tracing becomes a no-op and the application continues to work.
"""
from __future__ import annotations

import atexit
import hashlib
import logging
import os
import threading
from contextlib import nullcontext
from typing import Any, Dict, Iterable, Optional

_LOCK = threading.Lock()
_INITIALIZED = False
_LANGFUSE_CLIENT: Any = None
_FLUSH_REGISTERED = False


def _has_langfuse_credentials() -> bool:
    required = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")
    return all(os.getenv(name) for name in required)


def init_langfuse() -> bool:
    """Initialize Langfuse and Anthropic instrumentation once."""
    global _INITIALIZED, _LANGFUSE_CLIENT, _FLUSH_REGISTERED

    with _LOCK:
        if _INITIALIZED:
            return _LANGFUSE_CLIENT is not None

        _INITIALIZED = True

        if not _has_langfuse_credentials():
            logging.info("Langfuse tracing disabled: missing credentials")
            return False

        try:
            from langfuse import get_client
        except ImportError:
            logging.warning("Langfuse tracing disabled: 'langfuse' package is not installed")
            return False

        try:
            _LANGFUSE_CLIENT = get_client()
        except Exception:
            logging.exception("Failed to initialize Langfuse client")
            _LANGFUSE_CLIENT = None
            return False

        try:
            from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
        except ImportError:
            logging.warning(
                "Langfuse initialized but Anthropic auto-instrumentation is unavailable; "
                "install 'opentelemetry-instrumentation-anthropic' for generation traces"
            )
        else:
            try:
                instrumentor = AnthropicInstrumentor()
                if not getattr(instrumentor, "is_instrumented_by_opentelemetry", False):
                    instrumentor.instrument()
            except Exception as exc:
                if "already instrumented" not in str(exc).lower():
                    logging.exception("Failed to instrument Anthropic SDK for Langfuse")

        if not _FLUSH_REGISTERED:
            atexit.register(flush_langfuse)
            _FLUSH_REGISTERED = True

        logging.info("Langfuse tracing enabled")
        return True


def get_langfuse_client() -> Any:
    """Return the Langfuse client or None when tracing is disabled."""
    init_langfuse()
    return _LANGFUSE_CLIENT


def flush_langfuse() -> None:
    client = get_langfuse_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        logging.exception("Failed to flush Langfuse events")


def hash_identifier(value: Optional[str], *, prefix: str = "id", length: int = 12) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[: max(4, length)]
    return f"{prefix}-{digest}"


def trace_user_id(user_key: Optional[str]) -> Optional[str]:
    """Convert raw user identifiers into a stable, non-PII trace user id."""
    return hash_identifier(user_key, prefix="user", length=16)


def sanitize_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Langfuse propagated metadata requires short string values and simple keys."""
    sanitized: Dict[str, str] = {}
    if not metadata:
        return sanitized

    for key, value in metadata.items():
        if value is None:
            continue

        clean_key = "".join(ch for ch in str(key) if ch.isalnum())
        if not clean_key:
            continue

        text = str(value).strip()
        if not text:
            continue

        sanitized[clean_key] = text[:200]

    return sanitized


def sanitize_tags(tags: Optional[Iterable[str]]) -> list[str]:
    if not tags:
        return []
    sanitized: list[str] = []
    for tag in tags:
        text = str(tag).strip()
        if text:
            sanitized.append(text[:200])
    return sanitized


def start_observation(
    *,
    name: str,
    as_type: str = "span",
    input: Any = None,
    **kwargs: Any,
):
    """Start a Langfuse observation if tracing is enabled, else return a no-op context."""
    client = get_langfuse_client()
    if client is None:
        return nullcontext(None)

    payload = {"name": name, "as_type": as_type}
    if input is not None:
        payload["input"] = input

    for key, value in kwargs.items():
        if value is not None:
            payload[key] = value

    return client.start_as_current_observation(**payload)


def propagate_trace_attributes(
    *,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    trace_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    tags: Optional[Iterable[str]] = None,
    version: Optional[str] = None,
):
    """Propagate Langfuse trace attributes when tracing is enabled."""
    client = get_langfuse_client()
    if client is None:
        return nullcontext()

    from langfuse import propagate_attributes

    payload: Dict[str, Any] = {}
    if user_id:
        payload["user_id"] = user_id
    if session_id:
        payload["session_id"] = session_id
    if trace_name:
        payload["trace_name"] = trace_name

    sanitized_metadata = sanitize_metadata(metadata)
    if sanitized_metadata:
        payload["metadata"] = sanitized_metadata

    sanitized_tags = sanitize_tags(tags)
    if sanitized_tags:
        payload["tags"] = sanitized_tags

    if version:
        payload["version"] = str(version)[:200]

    if not payload:
        return nullcontext()

    return propagate_attributes(**payload)


def observation_preview(value: Any, *, max_length: int = 1000) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}... [truncated]"
