from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Protocol, TYPE_CHECKING
from app.services.llm import CachePolicy, ModelConfig
if TYPE_CHECKING:
    from app.services.skill_router import SkillSelectorDecision


@dataclass(frozen=True)
class ComplexityAssessment:
    complexity_level: str | None = None
    needs_schema_lookup: bool | None = None
    likely_requires_dax: bool | None = None
    multi_step_analysis: bool | None = None
    requires_comparison: bool | None = None
    ambiguous_question: bool | None = None
    requires_time_intelligence: bool | None = None
    likely_multiple_dax_queries: bool | None = None
    needs_reasoning: bool | None = None
    confidence: float | None = None
    strategy: str = "disabled"

    def __post_init__(self):
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class ExecutionPolicy:
    main_model_key: str
    max_tool_rounds: int = 10
    schema_table_limit: int = 6
    schema_measure_limit: int = 10
    skill_candidate_limit: int = 6
    retrieval_strategy: str = "current"
    reasoning_effort: str | None = None
    service_tier: str | None = None
    cache: CachePolicy = field(default_factory=CachePolicy)

    def __post_init__(self):
        if self.max_tool_rounds < 0 or min(self.schema_table_limit, self.schema_measure_limit, self.skill_candidate_limit) < 1:
            raise ValueError("Invalid execution limits")


class SkillCandidateSelector(Protocol):
    async def select(self, query: str, candidates: list, context: dict) -> SkillSelectorDecision: ...


class QueryRewriter(Protocol):
    async def rewrite(self, query: str, context: dict) -> str: ...


class ComplexityClassifier(Protocol):
    async def classify(self, context: Any) -> ComplexityAssessment: ...


class ExecutionPolicyResolver(Protocol):
    def resolve(self, assessment: ComplexityAssessment, default: ExecutionPolicy) -> ExecutionPolicy: ...


class ModelRoleResolver(Protocol):
    def resolve(self, role: str, *, report_id=None, empresa_id=None) -> ModelConfig: ...
