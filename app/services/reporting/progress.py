"""Narrow, persistence-agnostic hook the pipeline reports progress through.

The pipeline, generator and coordination runner only know this Protocol: they
never import SQLAlchemy, Flask or the run store. A durable implementation lives
in ``run_executor``; tests (or a CLI) can pass any object with these methods.
Methods may be sync or async. An exception raised by a method is NOT swallowed:
losing a persisted section must be visible, and it is also how a cancellation
request stops the pipeline between steps.
"""
from __future__ import annotations

import inspect
from typing import Any, Protocol

from .contracts import ReportSection

# Pipeline stages, independent from the run's overall status.
STAGE_PREFLIGHT = "preflight"
STAGE_ANALYSIS = "analysis"
STAGE_COORDINATION = "coordination"
STAGE_WRITING = "writing"
STAGE_RENDERING = "rendering"


class ReportProgress(Protocol):
    def stage(self, stage: str) -> Any: ...

    def section(self, section: ReportSection) -> Any: ...


async def notify_stage(progress: ReportProgress | None, stage: str) -> None:
    if progress is not None:
        result = progress.stage(stage)
        if inspect.isawaitable(result):
            await result


async def notify_checkpoint(progress: ReportProgress | None) -> None:
    """Optional ``progress.checkpoint()``: raise (e.g. cancellation) BEFORE a new analysis starts.

    Not part of the Protocol on purpose: progress objects without it simply never stop the
    scheduler early (they still stop it through ``stage``/``section`` raising).
    """
    check = getattr(progress, "checkpoint", None)
    if callable(check):
        result = check()
        if inspect.isawaitable(result):
            await result


async def notify_section(progress: ReportProgress | None, section: ReportSection) -> None:
    if progress is not None:
        result = progress.section(section)
        if inspect.isawaitable(result):
            await result
