from __future__ import annotations
import json
import time
from app.services.observability import start_observation
from app.services.llm import CachePolicy, LLMMessage, LLMRequest, ToolDefinition, report_cache_scope
from .contracts import ComplexityAssessment


class DisabledComplexityClassifier:
    async def classify(self, context):
        return ComplexityAssessment()


class LLMComplexityClassifier:
    """Structured classifier enabled only by an explicit component assignment."""

    def __init__(self, runtime, model):
        self.runtime = runtime
        self.model = model

    async def classify(self, context):
        tool = ToolDefinition(
            name="submit_complexity_assessment",
            description="Devuelve una clasificacion estructurada de la consulta.",
            parameters={
                "type": "object",
                "properties": {
                    "complexity_level": {"type": "string", "enum": ["low", "medium", "high"]},
                    "needs_schema_lookup": {"type": "boolean"},
                    "likely_requires_dax": {"type": "boolean"},
                    "multi_step_analysis": {"type": "boolean"},
                    "requires_comparison": {"type": "boolean"},
                    "ambiguous_question": {"type": "boolean"},
                    "requires_time_intelligence": {"type": "boolean"},
                    "likely_multiple_dax_queries": {"type": "boolean"},
                    "needs_reasoning": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["complexity_level", "confidence"],
            },
        )
        response = await self.runtime.generate(LLMRequest(
            model=self.model,
            instructions=[{"text": (
                "Clasifica la complejidad de una pregunta para un agente de Power BI. "
                "No respondas la pregunta; devuelve solamente la evaluacion estructurada."
            )}],
            messages=[LLMMessage("user", context.user_message)], tools=[tool],
            tool_choice=tool.name, temperature=0.0,
            cache=context.cache_policy or CachePolicy(scope=report_cache_scope(context.empresa_id, context.report_id)),
            operation="complexity-classification-generation",
        ))
        payload = next((call.arguments for call in response.tool_calls if call.name == tool.name), None)
        if payload is None:
            payload = json.loads(response.text)
        allowed = set(ComplexityAssessment.__dataclass_fields__) - {"strategy"}
        values = {key: value for key, value in dict(payload).items() if key in allowed}
        assessment = ComplexityAssessment(**values, strategy="llm")
        usage = response.usage.ledger_fields()
        context.decision_usage_events.append({
            "provider": self.model.provider, "model": self.model.physical_model,
            "event_type": "generation", "source_type": context.source,
            "trigger_type": "user_request", "operation_name": "complexity-classification",
            "status": "success", "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "total_tokens": usage["input_tokens"] + usage["output_tokens"],
            "cache_write_tokens": usage["cache_write_tokens"],
            "cache_read_tokens": usage["cache_read_tokens"],
            "metadata_json": {"component": "complexity_classifier",
                              "normalized_usage": response.usage.metadata(),
                              "actual_model": response.model, **self.model.metadata()},
        })
        return assessment


class PassthroughQueryRewriter:
    async def rewrite(self, query, context):
        return query


class StaticExecutionPolicyResolver:
    def resolve(self, assessment, default):
        # Classification is descriptive. Only deterministic policy can control execution.
        return default


class ScopedModelRoleResolver:
    def __init__(self, global_roles, *, empresa_roles=None, report_roles=None):
        self.global_roles = dict(global_roles)
        self.empresa_roles = empresa_roles or {}
        self.report_roles = report_roles or {}

    def resolve(self, role, *, report_id=None, empresa_id=None):
        for roles in (self.report_roles.get(report_id, {}), self.empresa_roles.get(empresa_id, {}), self.global_roles):
            if role in roles:
                return roles[role]
        raise ValueError(f"No model configured for role {role}")


class LLMQueryRewriter:
    def __init__(self, runtime, model, metrics=None, cache_policy=None):
        self.runtime, self.model = runtime, model
        self.metrics = metrics if metrics is not None else {}
        self.cache_policy = cache_policy

    async def rewrite(self, query, context):
        from app.services.agent_core import _rewrite_query_for_reranker
        started = time.monotonic()
        try:
            return await _rewrite_query_for_reranker(user_message=query, runtime=self.runtime,
                                                     model=self.model, cache_policy=self.cache_policy, **context)
        finally:
            self.metrics["query_rewriter"] = self.metrics.get("query_rewriter", 0) + round((time.monotonic() - started) * 1000)


class LLMSkillSelector:
    strategy = "llm"

    def __init__(self, runtime, model, metrics=None, cache_policy=None):
        self.runtime, self.model = runtime, model
        self.metrics = metrics if metrics is not None else {}
        self.cache_policy = cache_policy

    async def select(self, query, candidates, context):
        from app.services.skill_router import _select_skill_candidates
        started = time.monotonic()
        try:
            return await _select_skill_candidates(user_message=query, candidates=candidates,
                                                  runtime=self.runtime, model=self.model,
                                                  cache_policy=self.cache_policy, **context)
        finally:
            self.metrics["skill_selector"] = self.metrics.get("skill_selector", 0) + round((time.monotonic() - started) * 1000)


class EmbeddingSkillSelector:
    """Select candidates deterministically from their vector ranking."""

    strategy = "embeddings"

    def __init__(self, metrics=None):
        self.metrics = metrics if metrics is not None else {}

    async def select(self, query, candidates, context):
        del query
        from app.services.skill_router import _select_embedding_skill_candidates
        started = time.monotonic()
        try:
            return _select_embedding_skill_candidates(
                candidates=candidates,
                settings=context["settings"],
            )
        finally:
            self.metrics["skill_selector"] = self.metrics.get("skill_selector", 0) + round((time.monotonic() - started) * 1000)


class ExistingSkillRouter:
    """Retains candidate retrieval, vector fallback, shadow mode and companions."""
    def __init__(self, selector=None, metrics=None):
        self.selector = selector
        self.metrics = metrics if metrics is not None else {}

    async def __call__(self, **kwargs):
        from app.services.skill_router import resolve_skill_route
        started = time.monotonic()
        try:
            return await resolve_skill_route(selector=self.selector, **kwargs)
        finally:
            self.metrics["skill_routing"] = round((time.monotonic() - started) * 1000)
