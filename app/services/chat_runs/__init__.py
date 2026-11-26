"""Asynchronous KLARA chat: a durable queue of chat questions (``chat_runs``) executed by a dedicated worker.

Plan and stages: ``docs/klara-chat-asincrono.md``. Vocabulary: ``app/persistence/chat_run_states.py``.

The executor (``.executor``) is not imported here on purpose: it pulls in ``chatbot_service``, while the
store and contracts stay light for the web process.
"""
from .contracts import (
    ChatRunCancelledError, ChatRunError, ChatRunLeaseLostError, ChatRunLimitError, ChatRunRequest, ChatTurnHandler,
    LIMIT_OWNER_OPEN_RUNS, LIMIT_QUEUE_FULL, NullProgressSink, ProgressSink,
)
from .store import DEFAULT_LEASE_SECONDS, ChatRunStore

__all__ = [
    "ChatRunCancelledError", "ChatRunError", "ChatRunLeaseLostError", "ChatRunLimitError", "ChatRunRequest",
    "ChatRunStore", "ChatTurnHandler", "DEFAULT_LEASE_SECONDS", "LIMIT_OWNER_OPEN_RUNS", "LIMIT_QUEUE_FULL",
    "NullProgressSink", "ProgressSink",
]
