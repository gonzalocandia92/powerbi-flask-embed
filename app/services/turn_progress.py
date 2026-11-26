"""What an analytical turn is doing right now, for a consumer that wants to show it (e.g. the async chat).

A leaf module (standard library only) so the agent can report progress without importing any consumer, and
without changing the ``AnalyticsExecutor`` boundary: the consumer opens a ``progress_scope(sink)`` around the
turn and the agent calls ``report_stage(code)``. The sink travels in a ``ContextVar``, which asyncio tasks and
``asyncio.to_thread`` copy, so every step of THIS turn reaches it and concurrent turns never see each other's.

Best effort by contract: without a scope ``report_stage`` does nothing (synchronous chat, WhatsApp,
reports, evaluations), and a failing sink is logged and ignored, so progress can never break a turn.

The codes are the turn's own vocabulary; consumers that persist them (``chat_run_states.STAGES``) must
accept every one of them (guarded by a drift test).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional, Protocol

LOG = logging.getLogger(__name__)

STAGE_PREPARING = "preparing"
STAGE_SELECTING_SKILLS = "selecting_skills"
STAGE_READING_SCHEMA = "reading_schema"
STAGE_QUERYING_DATA = "querying_data"
STAGE_WRITING_ANSWER = "writing_answer"

TURN_STAGES = (STAGE_PREPARING, STAGE_SELECTING_SKILLS, STAGE_READING_SCHEMA, STAGE_QUERYING_DATA,
               STAGE_WRITING_ANSWER)


class StageSink(Protocol):
    def stage(self, code: str) -> None: ...


_current_sink: ContextVar[Optional[StageSink]] = ContextVar("klara_turn_progress_sink", default=None)


@contextmanager
def progress_scope(sink: Optional[StageSink]) -> Iterator[None]:
    """Route ``report_stage`` calls of the enclosed turn to ``sink`` (``None`` keeps them silent)."""
    token = _current_sink.set(sink)
    try:
        yield
    finally:
        _current_sink.reset(token)


def report_stage(code: str) -> None:
    sink = _current_sink.get()
    if sink is None:
        return
    try:
        sink.stage(code)
    except Exception:  # noqa: BLE001 - progress must never break a turn
        LOG.warning("[TurnProgress] stage sink failed for %s", code, exc_info=True)
