"""Compile a ``structure_prompt`` into a validated ``StructurePlanningResult`` (no DB, no HTTP).

Policy, in one place:

* no prompt  -> deterministic default spec; the planner is never resolved nor called (no LLM, no usage);
* prompt     -> planner -> mandatory deterministic validation -> placement info;
* any planner failure (not configured, billing, provider, invalid output, or an output that fails
  domain validation) -> graceful degradation to the default spec with ``source="fallback"``, the error
  recorded and a ``planner_failed`` warning. A failure is never hidden and never fatal here.

The planner is resolved lazily (``resolve_planner``) so a definition without a prompt never touches
model configuration or pricing.
"""
from __future__ import annotations

import inspect
import logging
from typing import Awaitable, Callable, Iterable

from .structure_contracts import (
    SOURCE_DEFAULT, SOURCE_FALLBACK, SOURCE_INTERPRETED, WARNING_PLANNER_FAILED, PlannerAttempt, StructureError, StructureQuestion,
    StructurePlanningRequest, StructurePlanningResult, StructureWarning,
)
from .structure_planner import StructurePlanner
from .structure_spec import CURRENT_STRUCTURE_SCHEMA
from .structure_validation import default_structure_spec, placement_warnings, validate_spec

LOG = logging.getLogger(__name__)

AttemptHook = Callable[[PlannerAttempt], Awaitable[None] | None]


def _dedupe(warnings: Iterable[StructureWarning]) -> tuple[StructureWarning, ...]:
    seen, unique = set(), []
    for warning in warnings:
        marker = (warning.code, warning.message)
        if marker not in seen:
            seen.add(marker)
            unique.append(warning)
    return tuple(unique)


def default_result(questions: Iterable[StructureQuestion], *, warnings: Iterable[StructureWarning] = ()) -> StructurePlanningResult:
    return StructurePlanningResult(
        spec=default_structure_spec(questions), warnings=_dedupe(warnings), source=SOURCE_DEFAULT)


class StructureCompiler:
    def __init__(self, resolve_planner: Callable[[], StructurePlanner]):
        self.resolve_planner = resolve_planner

    async def compile(self, structure_prompt: str | None, questions: Iterable[StructureQuestion], *,
                      schema_version: str = CURRENT_STRUCTURE_SCHEMA,
                      on_attempt: AttemptHook | None = None) -> StructurePlanningResult:
        questions = tuple(sorted(questions, key=lambda q: (q.position, q.key)))
        prompt = (structure_prompt or "").strip()
        if not prompt:
            return default_result(questions)

        attempts: list[PlannerAttempt] = []

        async def collect(attempt: PlannerAttempt) -> None:
            attempts.append(attempt)
            if on_attempt is not None:
                hooked = on_attempt(attempt)
                if inspect.isawaitable(hooked):
                    await hooked

        request = StructurePlanningRequest(
            structure_prompt=prompt, questions=questions, schema_version_requested=schema_version)
        try:
            planner = self.resolve_planner()
            result = await planner.plan(request, on_attempt=collect)
            # The planner (LLM or fake) is never trusted: re-validate against THIS definition.
            spec = validate_spec(result.spec, request.question_keys(), schema_version=schema_version)
        except StructureError as exc:
            return self._fallback(questions, exc.code, str(exc), attempts, errors=exc.errors)
        except Exception as exc:  # noqa: BLE001 - a planner bug must degrade, not break the definition flow
            LOG.exception("Structure planner failed unexpectedly")
            return self._fallback(questions, "structure_planner_unexpected_error",
                                  "El planner falló de forma inesperada.", attempts)
        warnings = _dedupe([*result.warnings, *placement_warnings(spec, questions)])
        return StructurePlanningResult(
            spec=spec, warnings=warnings, source=SOURCE_INTERPRETED, planner=dict(result.planner),
            attempts=attempts or list(result.attempts))

    @staticmethod
    def _fallback(questions, code: str, message: str, attempts: list[PlannerAttempt],
                  *, errors: list[str] | None = None) -> StructurePlanningResult:
        LOG.warning("Structure planning degraded to default structure code=%s", code)
        return StructurePlanningResult(
            spec=default_structure_spec(questions), source=SOURCE_FALLBACK, attempts=attempts,
            warnings=(StructureWarning(
                code=WARNING_PLANNER_FAILED,
                message="No se pudo interpretar el prompt de estructura; se usa la estructura estándar."),),
            error={"code": code, "message": message[:500], "errors": list(errors or [])[:10]})
