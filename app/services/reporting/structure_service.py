"""Explicit "compile the structure of a definition" operation (the only place that can cost tokens).

Orchestrates store + compiler + usage ledger; it contains neither planning logic nor SQL nor HTTP.

When the planner runs:   only here, only when the caller asks (``compile-structure``), the definition
                         HAS a prompt, and its saved spec is not an up-to-date interpretation
                         (or ``force=True``).
When it does NOT run:    no prompt; spec already interpreted for the same prompt+questions (idempotent);
                         saving a definition; creating a run; the worker. Those only READ the store.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from app import db
from app.models import AnalyticalReportDefinition

from .structure_compiler import StructureCompiler
from .structure_contracts import PlannerAttempt, StructureError
from .structure_spec import CURRENT_STRUCTURE_SCHEMA
from .structure_store import STATUS_INTERPRETED, STATUS_NOT_REQUIRED, StructureState, StructureStore
from .usage import record_structure_planner_usage

LOG = logging.getLogger(__name__)


class StructureDefinitionNotFoundError(StructureError):
    code = "definition_not_found"


@dataclass(frozen=True)
class CompileOutcome:
    state: StructureState
    compiled: bool   # the planner path ran and its result was saved
    reused: bool     # an up-to-date interpretation already existed; nothing was called


class StructureService:
    def __init__(self, compiler_factory: Callable[[int], StructureCompiler], *, store: StructureStore | None = None,
                 record_usage: Callable[..., None] = record_structure_planner_usage):
        self.compiler_factory = compiler_factory
        self.store = store or StructureStore()
        self.record_usage = record_usage

    def get_state(self, definition_id: int) -> StructureState:
        return self.store.state(self._definition(definition_id))

    def compile_definition(self, definition_id: int, *, force: bool = False) -> CompileOutcome:
        model = self._definition(definition_id)
        state = self.store.state(model)
        if state.status == STATUS_NOT_REQUIRED:
            return CompileOutcome(state, compiled=False, reused=False)
        if state.status == STATUS_INTERPRETED and not force:
            return CompileOutcome(state, compiled=False, reused=True)

        report_id = model.report_id_fk
        compiler = self.compiler_factory(report_id)
        result = asyncio.run(compiler.compile(state.prompt, state.questions, schema_version=CURRENT_STRUCTURE_SCHEMA))
        self._record_usage(report_id, definition_id, result.attempts)
        self.store.save_compiled(definition_id, result, state.input_hash)
        return CompileOutcome(self.store.state(self._definition(definition_id)), compiled=True, reused=False)

    def _record_usage(self, report_id: int, definition_id: int, attempts: list[PlannerAttempt]) -> None:
        for attempt in attempts:
            if not attempt.usage_event:
                continue
            try:
                self.record_usage(report_id, attempt.usage_event, definition_id=definition_id)
            except Exception:  # noqa: BLE001 - mirror coordinator: the failure is logged, never hidden by a crash
                LOG.exception("Structure planner usage could not be recorded definition=%s", definition_id)

    @staticmethod
    def _definition(definition_id: int) -> AnalyticalReportDefinition:
        model = db.session.get(AnalyticalReportDefinition, definition_id)
        if model is None:
            raise StructureDefinitionNotFoundError(f"Definition not found: {definition_id}")
        return model
