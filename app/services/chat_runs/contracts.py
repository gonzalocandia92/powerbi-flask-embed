"""Plain contracts of the asynchronous chat queue: the frozen request and the store errors.

No Flask, no SQLAlchemy: the web layer builds a ``ChatRunRequest`` after resolving access and owner, the
store persists it, and the worker rebuilds it from the row to run the turn.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

from app.persistence.chat_run_states import SOURCE_CHAT

# Which limit refused an enqueue (``ChatRunLimitError.kind``).
LIMIT_OWNER_OPEN_RUNS = "owner_open_runs"
LIMIT_QUEUE_FULL = "queue_full"


@dataclass(frozen=True)
class ChatRunRequest:
    """One chat question, already authorized: everything the worker needs, without a request context."""

    slug: str
    question: str
    owner_hash: str
    conversation_id: Optional[str] = None
    model_key: Optional[str] = None
    reset_history: bool = False
    is_staff: bool = False
    user_key: Optional[str] = None
    source: str = SOURCE_CHAT
    # Report behind the slug when it was enqueued (audit / per-report views). The worker re-resolves the slug.
    report_id: Optional[int] = None

    def __post_init__(self):
        for name in ("slug", "question", "owner_hash"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} is required")


class ProgressSink(Protocol):
    """Receives what the agent is doing (a code of ``chat_run_states.STAGES``).

    Best effort by contract: an implementation must never raise into the agent, so reporting progress can
    never break a turn. Cancellation is NOT signalled through here (the worker cancels the turn's task).
    """

    def stage(self, code: str) -> None: ...


class NullProgressSink:
    """Default sink: progress is ignored (synchronous chat, WhatsApp, reports)."""

    def stage(self, code: str) -> None:
        return None


class ChatTurnHandler(Protocol):
    """Runs one chat turn and returns the service result (the caller extracts the public payload)."""

    async def handle(self, request: ChatRunRequest, progress: ProgressSink) -> Dict[str, Any]: ...


class ChatRunError(Exception):
    """Base error of the chat queue."""


class ChatRunLeaseLostError(ChatRunError):
    """The worker no longer owns the run (lease expired, run recovered or finished): it must stop writing."""


class ChatRunCancelledError(ChatRunError):
    """Cancellation was requested: the worker should stop at the next safe point."""


class ChatRunLimitError(ChatRunError):
    """An enqueue was refused by a limit. ``open_run_id`` is the owner's run already in progress, if any."""

    def __init__(self, kind: str, message: str, *, open_run_id: Optional[str] = None):
        super().__init__(message)
        self.kind = kind
        self.open_run_id = open_run_id
