"""Direct, persistent evaluations over the internal AnalyticsExecutor API."""
from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app import db
from app.models import AIModelConfig, ModelEvaluationCase, ModelEvaluationRun
from app.services import ai_billing
from app.services.analytics import AnalyticsExecutor, AnalyticsRequest
from app.services.llm import CachePolicy, CacheScope


def _utcnow():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class EvaluationQuestion:
    question: str
    expected_answer: str | None = None


@dataclass(frozen=True)
class EvaluationSpec:
    name: str
    report_id: int
    model_keys: list[str]
    questions: list[EvaluationQuestion]
    mode: str = "independent"
    cache_mode: str = "cold"
    configuration: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.mode not in {"independent", "conversation"}:
            raise ValueError("mode must be independent or conversation")
        if self.cache_mode not in {"cold", "warm", "both"}:
            raise ValueError("cache_mode must be cold, warm or both")
        if not self.model_keys or not self.questions:
            raise ValueError("At least one model and one question are required")
        if any(not item.question.strip() for item in self.questions):
            raise ValueError("Evaluation questions cannot be empty")
        if len(self.questions) > 100:
            raise ValueError("An evaluation supports at most 100 questions")


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def _event_usage(result: dict) -> dict[str, int]:
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}
    for event in result.get("ai_usage_events") or []:
        totals["input"] += int(event.get("input_tokens") or 0)
        totals["output"] += int(event.get("output_tokens") or 0)
        totals["cache_read"] += int(event.get("cache_read_tokens") or 0)
        totals["cache_write"] += int(event.get("cache_write_tokens") or 0)
        normalized = (event.get("metadata_json") or {}).get("normalized_usage") or {}
        totals["reasoning"] += int(normalized.get("reasoning_tokens") or 0)
    return totals


class SQLAlchemyEvaluationRepository:
    def create_run(self, spec: EvaluationSpec) -> ModelEvaluationRun:
        cache_count = 2 if spec.cache_mode == "both" else 1
        run = ModelEvaluationRun(
            name=spec.name, report_id_fk=spec.report_id, mode=spec.mode,
            cache_mode=spec.cache_mode, status="running", started_at=_utcnow(),
            configuration_json={**spec.configuration, "model_keys": spec.model_keys},
            total_cases=len(spec.model_keys) * len(spec.questions) * cache_count,
        )
        db.session.add(run)
        db.session.flush()
        db.session.commit()
        return run

    def create_case(self, run, *, index, item, model_key, cache_mode):
        case = ModelEvaluationCase(
            run_id=run.id, sequence_index=index, question=item.question,
            expected_answer=item.expected_answer, model_key=model_key,
            cache_mode=cache_mode, status="running", attempt_count=1, started_at=_utcnow(),
        )
        db.session.add(case)
        db.session.flush()
        db.session.commit()
        return case

    def complete_case(self, case, result: dict, *, latency_ms: int, report) -> None:
        persisted_events = []
        for raw in result.get("ai_usage_events") or []:
            event = dict(raw)
            metadata = dict(event.pop("metadata_json", None) or {})
            metadata.update({"evaluation_run_id": case.run_id, "evaluation_case_id": case.id})
            # The engine labels the main event with its consumer and retains
            # technical source types for retrieval/selector events.
            event["trigger_type"] = "evaluation"
            persisted_events.append(ai_billing.record_ai_usage_event(
                report=report, metadata_json=metadata, **event
            ))
        costs = ai_billing.summarize_pipeline_costs(persisted_events)
        usage = _event_usage(result)
        route = result.get("route_metadata_json") or {}
        model_metadata = result.get("model_metadata") or {}
        case.answer = result.get("answer")
        case.provider = result.get("provider") or model_metadata.get("provider")
        case.physical_model = result.get("model") or model_metadata.get("physical_model")
        case.actual_model = result.get("actual_model")
        case.gateway = result.get("gateway") or model_metadata.get("gateway")
        case.service_tier = result.get("service_tier") or model_metadata.get("service_tier")
        case.status = "error" if result.get("had_error") else "success"
        case.latency_ms = latency_ms
        case.input_tokens = usage["input"]
        case.output_tokens = usage["output"]
        case.cache_read_tokens = usage["cache_read"]
        case.cache_write_tokens = usage["cache_write"]
        case.reasoning_tokens = usage["reasoning"]
        case.main_model_cost = costs["main_model_cost"]
        case.decision_layer_cost = costs["decision_layer_cost"]
        case.pipeline_total_cost = costs["pipeline_total_cost"]
        case.selected_skills_json = route.get("selected_skill_ids")
        case.candidate_skills_json = route.get("candidate_skill_ids")
        case.complexity_assessment_json = result.get("complexity_assessment")
        case.execution_policy_json = (result.get("execution_metadata") or {}).get("execution_policy")
        case.tools_called_json = result.get("tools_called") or []
        case.dax_query = result.get("dax_query")
        case.tool_rounds = result.get("tool_rounds")
        case.failure_reason = result.get("failure_reason")
        case.error_message = result.get("error_message")
        case.trace_id = result.get("trace_id")
        case.metrics_json = result.get("latency_by_component_ms") or {}
        case.completed_at = _utcnow()
        run = db.session.get(ModelEvaluationRun, case.run_id)
        if run is not None:
            run.completed_cases = run.cases.filter(
                ModelEvaluationCase.status.in_(("success", "error", "interrupted", "cancelled"))
            ).count()
        db.session.commit()

    def fail_case(self, case, exc: Exception, *, latency_ms: int) -> None:
        db.session.rollback()
        case = db.session.get(ModelEvaluationCase, case.id)
        case.status = "error"
        case.latency_ms = latency_ms
        case.error_message = str(exc)[:4000]
        case.completed_at = _utcnow()
        run = db.session.get(ModelEvaluationRun, case.run_id)
        if run is not None:
            run.completed_cases = run.cases.filter(
                ModelEvaluationCase.status.in_(("success", "error", "interrupted", "cancelled"))
            ).count()
        db.session.commit()

    def finish_run(self, run) -> ModelEvaluationRun:
        run = db.session.get(ModelEvaluationRun, run.id)
        cases = run.cases.all()
        by_model = {}
        for case in cases:
            item = by_model.setdefault(case.model_key, {"cases": 0, "successful": 0, "errors": 0,
                                                        "latency_ms": 0, "pipeline_total_cost": 0.0})
            item["cases"] += 1
            item["successful"] += int(case.status == "success")
            item["errors"] += int(case.status != "success")
            item["latency_ms"] += int(case.latency_ms or 0)
            item["pipeline_total_cost"] += float(case.pipeline_total_cost or 0.0)
        for item in by_model.values():
            item["average_latency_ms"] = round(item.pop("latency_ms") / item["cases"]) if item["cases"] else 0
        run.summary_json = {"models": by_model, "total_cases": len(cases)}
        run.status = "completed" if cases and all(case.status == "success" for case in cases) else "completed_with_errors"
        run.completed_at = _utcnow()
        run.completed_cases = len(cases)
        run.worker_id = None
        run.lease_expires_at = None
        # Promotion is intentionally never automatic. The administrative review
        # endpoint requires every case to be successful and manually marked correct.
        db.session.commit()
        return run

    def fail_run(self, run, exc: Exception) -> None:
        db.session.rollback()
        run = db.session.get(ModelEvaluationRun, run.id)
        run.status = "failed"
        run.error_message = str(exc)[:4000]
        run.completed_at = _utcnow()
        run.worker_id = None
        run.lease_expires_at = None
        db.session.commit()


class ModelEvaluationRunner:
    def __init__(self, *, analytics_executor_factory: Callable[..., AnalyticsExecutor] | None = None,
                 report_id: int | None = None, context_factory: Callable | None = None,
                 service_factory: Callable | None = None, repository=None, report=None):
        if analytics_executor_factory is None and (context_factory is None or service_factory is None):
            raise ValueError("analytics_executor_factory or legacy context/service factories are required")
        self.analytics_executor_factory = analytics_executor_factory
        self.report_id = report_id if report_id is not None else getattr(report, "id", None)
        self.context_factory = context_factory
        self.service_factory = service_factory
        self.repository = repository or SQLAlchemyEvaluationRepository()
        self.report = report

    async def _execute_case(self, *, question, history, model_key, cache_policy, run_id, experiment):
        if self.analytics_executor_factory is not None:
            executor = await _maybe_await(self.analytics_executor_factory(model_key, experiment))
            result = await executor.execute(AnalyticsRequest(
                report_id=self.report_id,
                question=question,
                history=history,
                source="evaluation",
                execution_id=f"evaluation:{run_id}:{model_key}",
                model_key=model_key,
                cache_policy=cache_policy,
                trace_context={
                    "evaluation_run_id": run_id,
                    "evaluation_model_key": model_key,
                },
            ))
            return result.to_dict()
        context = await _maybe_await(self.context_factory(
            question, history, model_key, cache_policy, str(run_id), experiment,
        ))
        service = await _maybe_await(self.service_factory(model_key, experiment))
        turn = await service.execute(context)
        return turn.to_dict()

    async def run(self, spec: EvaluationSpec) -> ModelEvaluationRun:
        run = self.repository.create_run(spec)
        cache_modes = ["cold", "warm"] if spec.cache_mode == "both" else [spec.cache_mode]
        sequence = 0
        try:
            for model_key in spec.model_keys:
                for cache_mode in cache_modes:
                    history = []
                    for item in spec.questions:
                        sequence += 1
                        case = self.repository.create_case(
                            run, index=sequence, item=item, model_key=model_key, cache_mode=cache_mode
                        )
                        scope_key = (
                            f"klara:evaluation:{run.id}:{model_key}:warm"
                            if cache_mode == "warm"
                            else f"klara:evaluation:{run.id}:{model_key}:cold:{case.id}"
                        )
                        started = time.monotonic()
                        try:
                            result = await self._execute_case(
                                question=item.question, history=list(history), model_key=model_key,
                                cache_policy=CachePolicy(scope=CacheScope(kind="evaluation", key=scope_key)),
                                run_id=run.id, experiment=spec.configuration,
                            )
                            latency_ms = round((time.monotonic() - started) * 1000)
                            self.repository.complete_case(case, result, latency_ms=latency_ms, report=self.report)
                            if spec.mode == "conversation":
                                history.extend([
                                    {"role": "user", "content": item.question},
                                    {"role": "assistant", "content": result.get("answer", "")},
                                ])
                        except Exception as exc:
                            latency_ms = round((time.monotonic() - started) * 1000)
                            self.repository.fail_case(case, exc, latency_ms=latency_ms)
                            if spec.mode == "conversation":
                                history = []
            return self.repository.finish_run(run)
        except Exception as exc:
            self.repository.fail_run(run, exc)
            raise

    async def run_existing(self, run: ModelEvaluationRun) -> ModelEvaluationRun:
        """Resume a queued persisted run without repeating terminal cases."""
        run = db.session.get(ModelEvaluationRun, run.id)
        config = dict(run.configuration_json or {})
        model_keys = list(config.get("model_keys") or [])
        cache_modes = ["cold", "warm"] if run.cache_mode == "both" else [run.cache_mode]
        run.status = "running"
        run.started_at = run.started_at or _utcnow()
        db.session.commit()
        try:
            for model_key in model_keys:
                for cache_mode in cache_modes:
                    history = []
                    cases = run.cases.filter_by(model_key=model_key, cache_mode=cache_mode).order_by(
                        ModelEvaluationCase.sequence_index.asc()
                    ).all()
                    for case in cases:
                        db.session.refresh(run)
                        if run.cancel_requested_at is not None:
                            for pending in run.cases.filter_by(status="pending").all():
                                pending.status = "cancelled"
                                pending.completed_at = _utcnow()
                            run.status = "cancelled"
                            run.completed_cases = run.total_cases
                            run.completed_at = _utcnow()
                            run.worker_id = None
                            run.lease_expires_at = None
                            db.session.commit()
                            return run
                        if case.status == "success":
                            if run.mode == "conversation":
                                history.extend([
                                    {"role": "user", "content": case.question},
                                    {"role": "assistant", "content": case.answer or ""},
                                ])
                            continue
                        if case.status not in {"pending"}:
                            if run.mode == "conversation":
                                history = []
                            continue
                        case.status = "running"
                        case.attempt_count += 1
                        case.started_at = _utcnow()
                        run.heartbeat_at = _utcnow()
                        run.lease_expires_at = _utcnow() + timedelta(hours=1)
                        db.session.commit()
                        scope_key = (
                            f"klara:evaluation:{run.id}:{model_key}:warm"
                            if cache_mode == "warm"
                            else f"klara:evaluation:{run.id}:{model_key}:cold:{case.id}"
                        )
                        started = time.monotonic()
                        try:
                            result = await self._execute_case(
                                question=case.question, history=list(history), model_key=model_key,
                                cache_policy=CachePolicy(scope=CacheScope(kind="evaluation", key=scope_key)),
                                run_id=run.id, experiment=config,
                            )
                            latency_ms = round((time.monotonic() - started) * 1000)
                            self.repository.complete_case(case, result, latency_ms=latency_ms, report=self.report)
                            if run.mode == "conversation":
                                history.extend([
                                    {"role": "user", "content": case.question},
                                    {"role": "assistant", "content": result.get("answer", "")},
                                ])
                        except Exception as exc:
                            latency_ms = round((time.monotonic() - started) * 1000)
                            self.repository.fail_case(case, exc, latency_ms=latency_ms)
                            if run.mode == "conversation":
                                history = []
            return self.repository.finish_run(run)
        except Exception as exc:
            self.repository.fail_run(run, exc)
            raise


def enqueue_evaluation(spec: EvaluationSpec, *, requested_by_user_id: int | None = None) -> ModelEvaluationRun:
    """Persist a complete run matrix before any provider call is made."""
    model_keys = list(dict.fromkeys(spec.model_keys))
    cache_modes = ["cold", "warm"] if spec.cache_mode == "both" else [spec.cache_mode]
    total_cases = len(model_keys) * len(cache_modes) * len(spec.questions)
    if total_cases > 1000:
        raise ValueError("An evaluation supports at most 1000 cases")
    for model_key in model_keys:
        model = AIModelConfig.query.filter_by(model_key=model_key).first()
        if model is None or not model.enabled:
            raise ValueError(f"Model is not enabled: {model_key}")
    run = ModelEvaluationRun(
        name=spec.name, report_id_fk=spec.report_id, requested_by_user_id=requested_by_user_id,
        mode=spec.mode, cache_mode=spec.cache_mode, status="queued",
        configuration_json={**spec.configuration, "model_keys": model_keys},
        total_cases=total_cases, completed_cases=0,
    )
    db.session.add(run)
    db.session.flush()
    sequence = 0
    for model_key in model_keys:
        for cache_mode in cache_modes:
            for question in spec.questions:
                sequence += 1
                db.session.add(ModelEvaluationCase(
                    run_id=run.id, sequence_index=sequence, question=question.question,
                    expected_answer=question.expected_answer, model_key=model_key,
                    cache_mode=cache_mode, status="pending",
                ))
    db.session.commit()
    return run


def build_report_evaluation_runner(report, *, config: dict[str, Any], runtime=None,
                                   component_overrides: dict[str, Any] | None = None) -> ModelEvaluationRunner:
    """Compose an evaluation consumer over the same analytics API used by Chat."""
    from app.services.agent_core import build_runtime_settings
    from app.services.analytics import KlaraAnalyticsEngine
    from app.services.decisions import EmbeddingSkillSelector, JevSkillSelector, JevWithLLMFallbackSelector, LLMSkillSelector
    from app.services.klara_execution import KlaraExecutionService
    from app.services.llm import LiteLLMRuntime
    from app.services.model_catalog import build_execution_resolver

    shared_runtime = runtime or LiteLLMRuntime(cost_resolver=ai_billing.generation_cost_details)
    component_overrides = component_overrides or {}
    engines = {}
    resolvers = {}

    def resolver(model_key):
        if model_key not in resolvers:
            settings = build_runtime_settings(config)
            billing_context = ai_billing.resolve_report_billing_context(report)
            resolvers[model_key] = build_execution_resolver(
                settings, report_id=report.id, empresa_id=billing_context.empresa_id,
                model_key=model_key, config=config,
            )
        return resolvers[model_key]

    class PassthroughRewriter:
        async def rewrite(self, query, context):
            return query

    def analytics_executor_factory(model_key, experiment):
        rewriter_strategy = experiment.get("query_rewriter_strategy", "current")
        skill_strategy = experiment.get("skill_selector_strategy", "current")
        if rewriter_strategy not in {"current", "disabled"} and "query_rewriter" not in component_overrides:
            raise ValueError(f"Unsupported query rewriter strategy: {rewriter_strategy}")
        if skill_strategy not in {"current", "embeddings", "jev", "jev_with_llm_fallback"} and "skill_selector" not in component_overrides:
            raise ValueError(f"Unsupported skill selector strategy: {skill_strategy}")
        cache_key = (model_key, rewriter_strategy, skill_strategy)
        if cache_key in engines:
            return engines[cache_key]
        query_rewriter = component_overrides.get("query_rewriter")
        if rewriter_strategy == "disabled":
            query_rewriter = PassthroughRewriter()
        skill_selector = component_overrides.get("skill_selector")
        if skill_strategy == "embeddings":
            skill_selector = EmbeddingSkillSelector()
        elif skill_strategy in {"jev", "jev_with_llm_fallback"}:
            jev_selector = JevSkillSelector()
            if skill_strategy == "jev_with_llm_fallback":
                billing_context = ai_billing.resolve_report_billing_context(report)
                fallback_model = resolver(model_key).fallback.resolve(
                    "skill_selector", report_id=report.id, empresa_id=billing_context.empresa_id,
                )
                ai_billing.validate_pricing_coverage(fallback_model)
                skill_selector = JevWithLLMFallbackSelector(
                    jev_selector, LLMSkillSelector(shared_runtime, fallback_model),
                )
            else:
                skill_selector = jev_selector
        execution_service = KlaraExecutionService(
            runtime=shared_runtime, query_rewriter=query_rewriter,
            skill_selector=skill_selector,
            complexity_classifier=component_overrides.get("complexity_classifier"),
            policy_resolver=component_overrides.get("policy_resolver"),
        )
        engines[cache_key] = KlaraAnalyticsEngine(
            config=config, runtime=shared_runtime, execution_service=execution_service,
        )
        return engines[cache_key]

    return ModelEvaluationRunner(
        analytics_executor_factory=analytics_executor_factory,
        report_id=report.id,
        report=report,
    )
