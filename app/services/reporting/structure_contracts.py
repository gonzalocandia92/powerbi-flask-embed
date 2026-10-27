"""Explicit request/result contracts of structure planning (V1.3).

Nothing here touches the database, an LLM or Flask. ``StructurePlanningRequest`` is the ONLY
thing a planner receives: the prompt plus a reduced view of the questions (no DAX, answers,
skills, schema, tokens, costs, traces or credentials).

Warnings vs errors:

* a **warning** (``StructureWarning``) means the spec is usable but part of the user's intent
  could not be represented (styling, charts, unknown metric...). Never fatal.
* an **error** (``StructurePlannerError`` / ``StructureValidationError``) means no usable spec
  came out; the compiler then degrades to the deterministic default spec and says so
  (``source="fallback"``), it never hides the failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .structure_spec import CURRENT_STRUCTURE_SCHEMA, ReportStructureSpec

# ── warning codes (open vocabulary: add a constant, no schema change) ───────────────────────
WARNING_UNSUPPORTED_PRESENTATION = "unsupported_presentation_instruction"
WARNING_UNKNOWN_REFERENCE = "unknown_question_reference"       # metric/evidence not among the questions
WARNING_AMBIGUOUS_DISCARDED = "ambiguous_instruction_ignored"
# Emitted only by deterministic code, never accepted from a model:
WARNING_QUESTIONS_NOT_PLACED = "questions_not_placed"
WARNING_PLANNER_FAILED = "planner_failed"
WARNING_NOT_COMPILED = "structure_not_compiled"
WARNING_STALE = "structure_stale"

PLANNER_WARNING_CODES = (WARNING_UNSUPPORTED_PRESENTATION, WARNING_UNKNOWN_REFERENCE, WARNING_AMBIGUOUS_DISCARDED)

# Where a spec came from.
SOURCE_INTERPRETED = "interpreted"   # the planner understood the prompt (planner_result = successfully_interpreted)
SOURCE_DEFAULT = "default"           # no prompt, or compilation is pending: deterministic default structure
SOURCE_FALLBACK = "fallback"         # the planner failed: default structure used, failure recorded
STRUCTURE_SOURCES = (SOURCE_INTERPRETED, SOURCE_DEFAULT, SOURCE_FALLBACK)


class StructureWarning(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=500)
    severity: Literal["info", "warning"] = "warning"


@dataclass(frozen=True)
class StructureQuestion:
    """Reduced, planner-facing view of one question. ``position`` is the 1-based order in the definition."""
    key: str
    title: str
    question: str
    position: int


@dataclass(frozen=True)
class StructurePlanningRequest:
    structure_prompt: str
    questions: tuple[StructureQuestion, ...]
    schema_version_requested: str = CURRENT_STRUCTURE_SCHEMA

    def question_keys(self) -> set[str]:
        return {question.key for question in self.questions}


@dataclass(frozen=True)
class FrozenStructure:
    """The structure a run was created with. Never recomputed from the definition afterwards."""
    prompt: str | None
    input_hash: str | None
    schema_version: str
    source: str            # interpreted | default | fallback
    status: str            # definition status when frozen: interpreted|fallback|not_required|not_compiled|stale
    spec: ReportStructureSpec
    warnings: tuple[StructureWarning, ...]


@dataclass
class PlannerAttempt:
    """One provider call (billing/diagnostics). ``usage_event`` is a ready ``AIUsageEvent`` payload."""
    attempt: int
    repair: bool
    valid: bool
    usage_event: dict[str, Any] | None = None
    validation_errors: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class StructurePlanningResult:
    """What compiling a structure produced; the unit that gets persisted and previewed."""
    spec: ReportStructureSpec
    warnings: tuple[StructureWarning, ...] = ()
    source: str = SOURCE_INTERPRETED
    # Planner identity/diagnostics (model key, prompt version, attempts) — never needed to read ``spec``.
    planner: dict[str, Any] = field(default_factory=dict)
    # Why the planner failed (``source == "fallback"``): {"code": ..., "message": ...}.
    error: dict[str, Any] | None = None
    attempts: list[PlannerAttempt] = field(default_factory=list)

    @property
    def fallback_used(self) -> bool:
        return self.source == SOURCE_FALLBACK


# ── errors ───────────────────────────────────────────────────────────────────────────────────
class StructureError(Exception):
    code = "structure_error"

    def __init__(self, message: str, *, code: str | None = None, errors: list[str] | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.errors = list(errors or [])


class StructureValidationError(StructureError):
    """A spec (or planner output) violates the schema or the domain rules."""
    code = "structure_invalid"


class StructurePlannerError(StructureError):
    """The planner could not produce a usable spec (not configured, provider failure, invalid output...)."""
    code = "structure_planner_error"


class StructurePlannerConfigurationError(StructurePlannerError):
    code = "structure_planner_not_configured"


class StructurePlannerExecutionError(StructurePlannerError):
    code = "structure_planner_execution_failed"


class StructurePlannerInvalidOutputError(StructurePlannerError):
    code = "structure_planner_invalid_output"


class StructurePlannerBillingLimitError(StructurePlannerError):
    code = "structure_planner_billing_limit_exceeded"
