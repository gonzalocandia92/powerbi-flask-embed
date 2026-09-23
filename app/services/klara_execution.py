"""Reusable turn composition, independent of Flask's request/application globals."""
from __future__ import annotations
import asyncio
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from app.services.llm import CachePolicy, LiteLLMRuntime, ModelCapabilities, ModelConfig, report_cache_scope
from app.services.decisions import (
    ComplexityAssessment, DisabledComplexityClassifier, LLMComplexityClassifier,
    PassthroughQueryRewriter, ExecutionPolicy,
    StaticExecutionPolicyResolver, ScopedModelRoleResolver,
    LLMQueryRewriter, LLMSkillSelector, EmbeddingSkillSelector, ExistingSkillRouter,
    JevSkillSelector, JevWithLLMFallbackSelector,
)
from app.services.observability import hash_identifier, start_observation, observation_preview


@dataclass
class ExecutionContext:
    user_message: str
    dataset_id: str
    settings: Any
    history: list = field(default_factory=list)
    source: str = "chat"
    report_id: int | None = None
    report_name: str | None = None
    empresa_id: int | None = None
    conversation_id: str | None = None
    schema_text: str | None = None
    custom_instructions: list | None = None
    schema_retrieval_prompt: str | None = None
    schema_table_context_limit: int | None = None
    schema_measure_context_limit: int | None = None
    powerbi_credentials: dict | None = field(default=None, repr=False)
    requested_model_key: str | None = None
    role_configuration: Any = field(default=None, repr=False)
    cache_policy: CachePolicy | None = None
    service_tier: str | None = None
    execution_policy: ExecutionPolicy | None = None
    billing_context: Any = field(default=None, repr=False)
    trace_context: dict = field(default_factory=dict)
    decision_usage_events: list = field(default_factory=list, repr=False)


@dataclass
class AgentTurnResult:
    payload: dict
    policy: ExecutionPolicy
    assessment: ComplexityAssessment

    def to_dict(self):
        return dict(self.payload)


def default_model_roles(settings):
    return ScopedModelRoleResolver({
        "main_agent": ModelConfig(settings.anthropic_model, settings.anthropic_model,
                                  max_output_tokens=settings.anthropic_max_tokens, api_key=settings.anthropic_api_key),
        "query_rewriter": ModelConfig("claude-haiku-rewriter", "claude-haiku-4-5-20251001",
                                      max_output_tokens=100, api_key=settings.anthropic_api_key),
        "skill_selector": ModelConfig(settings.skill_router_settings.selector_model, settings.skill_router_settings.selector_model,
                                      max_output_tokens=500, api_key=settings.anthropic_api_key),
    })


def _model_from_config(value, default, settings):
    if isinstance(value, ModelConfig):
        return value
    if not isinstance(value, dict):
        raise TypeError("Model role configuration must contain ModelConfig values or dictionaries")
    provider = str(value.get("provider") or default.provider)
    api_key = value.get("api_key")
    if api_key is None and provider == "anthropic":
        api_key = settings.anthropic_api_key
    capabilities = value.get("capabilities") or default.capabilities
    if isinstance(capabilities, dict):
        capabilities = ModelCapabilities(**capabilities)
    physical_model = str(value.get("physical_model") or value.get("litellm_model") or default.physical_model)
    if str(value.get("gateway") or default.gateway) == "direct" and physical_model.startswith(provider + "/"):
        physical_model = physical_model[len(provider) + 1:]
    return ModelConfig(
        model_key=str(value.get("model_key") or default.model_key),
        physical_model=physical_model,
        provider=provider, gateway=str(value.get("gateway") or default.gateway),
        max_output_tokens=int(value.get("max_output_tokens") or default.max_output_tokens),
        capabilities=capabilities,
        service_tier=value.get("service_tier", default.service_tier),
        pricing_tier=value.get("pricing_tier", default.pricing_tier),
        reasoning_effort=value.get("reasoning_effort", default.reasoning_effort),
        provider_options=dict(value.get("provider_options") or {}), api_key=api_key,
        family_key=value.get("family_key", default.family_key),
        family_options=dict(value.get("family_options", default.family_options) or {}),
        thinking_mode=value.get("thinking_mode", default.thinking_mode),
    )


def configured_model_roles(settings, configuration=None):
    """Resolve typed global/company/report role overrides without storing secrets."""
    defaults = default_model_roles(settings)
    if configuration is None or hasattr(configuration, "resolve"):
        return configuration or defaults
    if not isinstance(configuration, dict):
        raise TypeError("KLARA_MODEL_ROLES must be a mapping or ModelRoleResolver")
    global_raw = configuration.get("global", configuration if "global" not in configuration else {})
    global_roles = {role: _model_from_config(value, defaults.resolve(role), settings)
                    for role, value in global_raw.items() if role in defaults.global_roles}
    for role, value in defaults.global_roles.items():
        global_roles.setdefault(role, value)
    def scoped(scope_name):
        result = {}
        for raw_id, roles in (configuration.get(scope_name) or {}).items():
            scope_id = int(raw_id)
            result[scope_id] = {role: _model_from_config(value, global_roles[role], settings)
                                for role, value in roles.items() if role in global_roles}
        return result
    return ScopedModelRoleResolver(global_roles, empresa_roles=scoped("empresa"), report_roles=scoped("report"))


class KlaraExecutionService:
    def __init__(self, *, runtime=None, model_roles=None, query_rewriter=None, skill_selector=None,
                 complexity_classifier=None, policy_resolver=None, tool_registry=None,
                 route_resolver=None, prompt_manager=None, classifier_timeout=2.0):
        self.runtime = runtime or LiteLLMRuntime()
        self.model_roles = model_roles
        self.query_rewriter = query_rewriter
        self.skill_selector = skill_selector
        self.classifier = complexity_classifier
        self.policy_resolver = policy_resolver or StaticExecutionPolicyResolver()
        self.tool_registry = tool_registry
        self.route_resolver = route_resolver
        self.prompt_manager = prompt_manager
        self.classifier_timeout = classifier_timeout

    async def execute(self, context: ExecutionContext) -> AgentTurnResult:
        from app.services.agent_core import AgentOrchestrator, PromptManager, ToolRegistry, _coerce_positive_int
        started = time.monotonic()
        metrics = {}
        settings = context.settings
        roles = configured_model_roles(settings, self.model_roles or context.role_configuration)
        scope = {"report_id": context.report_id, "empresa_id": context.empresa_id}
        main = roles.resolve("main_agent", **scope)
        component_resolver = getattr(roles, "component", None)

        classifier = self.classifier
        if classifier is None and component_resolver is not None:
            try:
                classifier_component = component_resolver("complexity_classifier", **scope)
                if classifier_component.strategy == "model" and classifier_component.model is not None:
                    classifier = LLMComplexityClassifier(self.runtime, classifier_component.model)
            except Exception:
                classifier = None
        classifier = classifier or DisabledComplexityClassifier()
        # Client model selection is not exposed in V1. Reject unknown keys rather than silently ignoring them.
        if context.requested_model_key and context.requested_model_key != main.model_key:
            raise ValueError("Model selection is not enabled for this execution")
        cache = context.cache_policy or CachePolicy(scope=report_cache_scope(context.empresa_id, context.report_id))
        default = context.execution_policy or ExecutionPolicy(
            main_model_key=main.model_key, max_tool_rounds=settings.max_tool_rounds,
            schema_table_limit=_coerce_positive_int(context.schema_table_context_limit, 6),
            schema_measure_limit=_coerce_positive_int(context.schema_measure_context_limit, 10),
            skill_candidate_limit=settings.skill_router_settings.candidate_limit,
            service_tier=context.service_tier or main.service_tier,
            reasoning_effort=main.reasoning_effort, cache=cache,
        )
        with start_observation(name="powerbi-chat-agent", as_type="agent", input={"user_message": context.user_message}) as span:
            assessment = ComplexityAssessment()
            classifier_failed = False
            tick = time.monotonic()
            with start_observation(name="classify-complexity", as_type="span") as classification_span:
                try:
                    assessment = await asyncio.wait_for(classifier.classify(context), self.classifier_timeout)
                    if not isinstance(assessment, ComplexityAssessment):
                        raise ValueError("Invalid complexity assessment")
                except Exception:
                    classifier_failed = True
                    assessment = ComplexityAssessment(strategy="fallback")
                if classification_span is not None:
                    classification_span.update(output=asdict(assessment), metadata={"fallback": classifier_failed})
            metrics["complexity_classifier"] = round((time.monotonic() - tick) * 1000)
            with start_observation(name="resolve-execution-policy", as_type="span") as policy_span:
                policy = default if classifier_failed else self.policy_resolver.resolve(assessment, default)
                if not isinstance(policy, ExecutionPolicy):
                    raise ValueError("Invalid execution policy")
                if policy.main_model_key != main.model_key:
                    raise ValueError("Automatic model routing is not enabled in V1")
                if policy_span is not None:
                    policy_span.update(output=asdict(policy))
            main = replace(main, reasoning_effort=policy.reasoning_effort, service_tier=policy.service_tier)
            metadata = {**context.trace_context, **main.metadata(), "reportid": context.report_id,
                        "reportname": context.report_name, "empresa": context.empresa_id, "source": context.source,
                        "conversationid": context.conversation_id, "datasethash": hash_identifier(context.dataset_id, prefix="dataset"),
                        "historycount": len(context.history), "schemaloaded": bool(context.schema_text),
                        "schemaretrievalpromptloaded": bool(context.schema_retrieval_prompt),
                        "schematablelimit": policy.schema_table_limit, "schemameasurelimit": policy.schema_measure_limit,
                        "cache_scope": policy.cache.scope.key, "execution_policy": asdict(policy),
                        "complexity_classifier_strategy": assessment.strategy,
                        "skill_selector_strategy": (
                            getattr(self.skill_selector, "strategy", type(self.skill_selector).__name__)
                            if self.skill_selector else ("llm" if settings.skill_router_settings.selector_enabled else "embeddings")
                        )}
            if span is not None:
                span.update(metadata=metadata)
            rewriter = self.query_rewriter
            if rewriter is None:
                rewriter_component = component_resolver("query_rewriter", **scope) if component_resolver else None
                if rewriter_component is not None and rewriter_component.strategy == "disabled":
                    rewriter = PassthroughQueryRewriter()
                else:
                    rewriter_model = rewriter_component.model if rewriter_component else roles.resolve("query_rewriter", **scope)
                    rewriter = LLMQueryRewriter(self.runtime, rewriter_model, metrics, policy.cache)
            selector = self.skill_selector
            selector_strategy = getattr(selector, "strategy", "custom") if selector is not None else "llm"
            if selector is None:
                selector_component = component_resolver("skill_selector", **scope) if component_resolver else None
                if selector_component is not None and selector_component.strategy == "embeddings":
                    selector = EmbeddingSkillSelector(metrics)
                    selector_strategy = "embeddings"
                elif selector_component is not None and selector_component.strategy in {"jev", "jev_with_llm_fallback"}:
                    jev_selector = JevSkillSelector(metrics)
                    selector_strategy = selector_component.strategy
                    if selector_strategy == "jev_with_llm_fallback":
                        fallback_model = getattr(roles, "fallback", roles).resolve("skill_selector", **scope)
                        llm_selector = LLMSkillSelector(self.runtime, fallback_model, metrics, policy.cache)
                        selector = JevWithLLMFallbackSelector(jev_selector, llm_selector)
                    else:
                        selector = jev_selector
                elif not settings.skill_router_settings.selector_enabled and selector_component is None:
                    selector = EmbeddingSkillSelector(metrics)
                    selector_strategy = "embeddings"
                else:
                    selector_model = selector_component.model if selector_component else roles.resolve("skill_selector", **scope)
                    selector = LLMSkillSelector(self.runtime, selector_model, metrics, policy.cache)
            metadata["skill_selector_strategy"] = selector_strategy
            if span is not None:
                span.update(metadata=metadata)
            registry = self.tool_registry or ToolRegistry(query_rewriter=rewriter)
            effective_settings = replace(
                settings,
                max_tool_rounds=policy.max_tool_rounds,
                skill_router_settings=replace(
                    settings.skill_router_settings,
                    candidate_limit=policy.skill_candidate_limit,
                ),
            )
            agent = AgentOrchestrator(effective_settings, self.prompt_manager or PromptManager(settings.history_limit), registry,
                runtime=self.runtime, model=main, cache_policy=policy.cache,
                route_resolver=self.route_resolver or ExistingSkillRouter(selector, metrics))
            agent.metrics = metrics
            result = await agent.generate_response(
                user_message=context.user_message, dataset_id=context.dataset_id, history=context.history,
                schema_text=context.schema_text, conversation_id=context.conversation_id,
                report_id=context.report_id, empresa_id=context.empresa_id,
                powerbi_credentials=context.powerbi_credentials, custom_instructions=context.custom_instructions,
                schema_retrieval_prompt=context.schema_retrieval_prompt,
                schema_table_context_limit=policy.schema_table_limit, schema_measure_context_limit=policy.schema_measure_limit,
            )
            metrics["total"] = round((time.monotonic() - started) * 1000)
            result["latency_by_component_ms"] = dict(metrics)
            result["execution_metadata"] = metadata
            result["complexity_assessment"] = asdict(assessment)
            result["ai_usage_events"].extend(context.decision_usage_events)
            for event in result.get("ai_usage_events", []):
                event["metadata_json"] = {**(event.get("metadata_json") or {}), "source": context.source,
                    "reportname": context.report_name, "execution_policy": asdict(policy), "latency_by_component_ms": dict(metrics)}
            if span is not None:
                result["trace_id"] = getattr(span, "trace_id", None)
                for event in result.get("ai_usage_events", []):
                    event["trace_id"] = result["trace_id"]
                span.update(output={"answer": observation_preview(result["answer"], max_length=1200),
                    "tool_rounds": result["tool_rounds"], "input_tokens": result["input_tokens"],
                    "output_tokens": result["output_tokens"]}, metadata={**metadata, "latency_by_component_ms": metrics})
            return AgentTurnResult(result, policy, assessment)
