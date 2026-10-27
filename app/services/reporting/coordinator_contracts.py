"""Compact, coordinator-facing contracts.

These are the ONLY types the ``ReportCoordinator`` ever sees or produces. They
are deliberately blind to Power BI, DAX, schema, skills, routing, tokens,
pricing, traces, recovered technical errors and credentials: a
``CoordinatorSection`` for a failed analysis carries no more than its
key/title/question and a "failed" status. The coordinator decides WHAT to
investigate; ``AnalyticsExecutor`` (via ``run_analysis``) decides HOW.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Hard, non-recursive limits for V1.1 (see ``coordination.py``).
MAX_EXTRA_ANALYSES = 2
MAX_COORDINATOR_ROUNDS = 1

SectionKey = Annotated[str, Field(min_length=1, max_length=80)]
ShortText = Annotated[str, Field(min_length=1, max_length=1000)]
MediumText = Annotated[str, Field(min_length=1, max_length=500)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CoordinatorSection(_Strict):
    """One ``ReportDraft`` section, redacted down to what a coordinator may see.

    A ``failed`` section never carries ``answer`` or ``semantic_notes``: there is
    nothing analytically useful to hand the coordinator about a failure, and no
    internal error detail or DAX may leak through this contract.
    """
    key: SectionKey
    title: Annotated[str, Field(min_length=1, max_length=200)]
    question: ShortText
    status: Literal["ok", "failed"]
    answer: Annotated[str, Field(max_length=8000)] | None = None
    semantic_notes: Annotated[list[Annotated[str, Field(max_length=1000)]], Field(max_length=20)] = Field(
        default_factory=list)

    @model_validator(mode="after")
    def _failed_carries_no_analytical_content(self) -> "CoordinatorSection":
        if self.status == "failed" and (self.answer is not None or self.semantic_notes):
            raise ValueError("a failed CoordinatorSection cannot carry answer/semantic_notes")
        return self


class CoordinatorSemanticNote(_Strict):
    """A curated semantic constraint the coordinator must treat as already-explained context."""
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    source_section_keys: Annotated[list[SectionKey], Field(max_length=50)] = Field(default_factory=list)


class CoordinatorConstraints(_Strict):
    """Hard limits and already-answered questions, so the coordinator cannot repeat itself."""
    max_extra_analyses: Annotated[int, Field(ge=0, le=MAX_EXTRA_ANALYSES)] = MAX_EXTRA_ANALYSES
    max_coordinator_rounds: Annotated[int, Field(ge=0, le=MAX_COORDINATOR_ROUNDS)] = MAX_COORDINATOR_ROUNDS
    already_asked_questions: Annotated[list[ShortText], Field(max_length=50)] = Field(default_factory=list)


class CoordinatorInput(_Strict):
    """Everything the coordinator receives for one decision round."""
    report_name: Annotated[str, Field(min_length=1, max_length=200)]
    report_run_id: Annotated[str, Field(min_length=1, max_length=64)]
    sections: Annotated[list[CoordinatorSection], Field(min_length=1, max_length=50)]
    semantic_context: Annotated[list[CoordinatorSemanticNote], Field(max_length=50)] = Field(default_factory=list)
    constraints: CoordinatorConstraints = Field(default_factory=CoordinatorConstraints)

    def ok_section_keys(self) -> set[str]:
        return {section.key for section in self.sections if section.status == "ok"}


class RequestedAnalysis(_Strict):
    """One additional analytical question the coordinator wants ``AnalyticsExecutor`` to answer.

    ``question`` is executed verbatim by KLARA's normal dynamic routing; the
    coordinator never supplies DAX, a model, a tier or a skill for it.
    """
    question: ShortText
    purpose: MediumText
    expected_value: MediumText
    related_section_keys: Annotated[list[SectionKey], Field(max_length=20)] = Field(default_factory=list)


class CoordinatorDecision(_Strict):
    """Strict, auditable output of one coordination round.

    ``summary`` is a short, auditable explanation of the decision -- never
    chain-of-thought -- suitable for an admin debug panel.
    """
    action: Literal["finish", "run_analysis"]
    summary: ShortText
    analyses: Annotated[list[RequestedAnalysis], Field(max_length=MAX_EXTRA_ANALYSES)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _action_matches_analyses(self) -> "CoordinatorDecision":
        if self.action == "finish" and self.analyses:
            raise ValueError('action "finish" must not include analyses')
        if self.action == "run_analysis" and not self.analyses:
            raise ValueError('action "run_analysis" requires at least one analysis')
        return self


def coordinator_decision_json_schema() -> dict:
    return CoordinatorDecision.model_json_schema()
