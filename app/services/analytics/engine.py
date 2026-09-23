"""KLARA implementation of the consumer-agnostic analytics boundary."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from app import db
from app.models import Report
from app.services import agent_prompts, ai_billing, model_catalog
from app.services.agent_core import build_runtime_settings
from app.services.chat_credentials import resolve_powerbi_env_for_report
from app.services.klara_execution import ExecutionContext, KlaraExecutionService
from app.services.llm import LiteLLMRuntime
from app.utils.powerbi import get_current_dataset_id

from .contracts import (
    AnalyticsBillingLimitExceededError,
    AnalyticsConfigurationError,
    AnalyticsModelError,
    AnalyticsReportNotFoundError,
    AnalyticsRequest,
    AnalyticsResult,
    PreparedAnalyticsExecution,
)


class KlaraAnalyticsEngine:
    """Prepare report context and delegate analytical work to KLARA."""

    def __init__(
        self,
        *,
        config: dict[str, Any],
        runtime=None,
        execution_service=None,
    ):
        self.config = dict(config)
        self._prepare_token = object()
        self.runtime = runtime or LiteLLMRuntime(cost_resolver=ai_billing.generation_cost_details)
        self.execution_service = execution_service or KlaraExecutionService(runtime=self.runtime)

    @staticmethod
    def _validate_request(request: AnalyticsRequest) -> None:
        if not isinstance(request, AnalyticsRequest):
            raise AnalyticsConfigurationError("request must be an AnalyticsRequest")
        if isinstance(request.report_id, bool) or not isinstance(request.report_id, int) or request.report_id <= 0:
            raise AnalyticsConfigurationError("report_id must be a positive Report.id")
        if not isinstance(request.question, str) or not request.question.strip():
            raise AnalyticsConfigurationError("question cannot be empty")
        if not isinstance(request.history, list):
            raise AnalyticsConfigurationError("history must be a list")
        if not isinstance(request.trace_context, dict):
            raise AnalyticsConfigurationError("trace_context must be a dictionary")

    @staticmethod
    def _map_result(request: AnalyticsRequest, payload: dict[str, Any]) -> AnalyticsResult:
        """Explicitly translate the agent payload into the public result contract."""
        return AnalyticsResult(
            answer=str(payload.get("answer") or ""),
            report_id=request.report_id,
            tool_rounds=int(payload.get("tool_rounds") or 0),
            tools_called=list(payload.get("tools_called") or []),
            dax_query=payload.get("dax_query"),
            model_key=payload.get("model_key"),
            model=payload.get("model"),
            provider=payload.get("provider"),
            gateway=payload.get("gateway"),
            service_tier=payload.get("service_tier"),
            actual_model=payload.get("actual_model"),
            input_tokens=payload.get("input_tokens"),
            output_tokens=payload.get("output_tokens"),
            ai_usage_events=list(payload.get("ai_usage_events") or []),
            had_error=bool(payload.get("had_error")),
            error_message=payload.get("error_message"),
            failure_reason=payload.get("failure_reason"),
            route_metadata_json=payload.get("route_metadata_json"),
            route_validation_warnings=list(payload.get("route_validation_warnings") or []),
            latency_by_component_ms=dict(payload.get("latency_by_component_ms") or {}),
            trace_id=payload.get("trace_id"),
            execution_metadata=dict(payload.get("execution_metadata") or {}),
            model_metadata=dict(payload.get("model_metadata") or {}),
            complexity_assessment=dict(payload.get("complexity_assessment") or {}),
        )

    def _prepare_sync(self, request: AnalyticsRequest) -> PreparedAnalyticsExecution:
        """Resolve network and database backed context off the event loop."""
        report = db.session.get(Report, request.report_id)
        if report is None:
            raise AnalyticsReportNotFoundError(f"Report not found: {request.report_id}")

        dataset_id = get_current_dataset_id(report)
        try:
            credentials = resolve_powerbi_env_for_report(report)
        except RuntimeError as exc:
            raise AnalyticsConfigurationError(str(exc)) from exc
        try:
            settings = build_runtime_settings(self.config)
        except (TypeError, ValueError) as exc:
            raise AnalyticsConfigurationError(str(exc)) from exc
        billing_context = ai_billing.resolve_report_billing_context(report)
        instructions = agent_prompts.resolve_agent_prompt_instructions(report)

        try:
            model_roles = request.role_configuration
            if model_roles is None:
                model_roles = model_catalog.build_execution_resolver(
                    settings,
                    report_id=report.id,
                    empresa_id=billing_context.empresa_id,
                    model_key=request.model_key,
                    config=self.config,
                )
        except model_catalog.ModelSelectionError as exc:
            raise AnalyticsModelError(str(exc)) from exc

        try:
            ai_billing.validate_execution_pricing(report, settings, model_roles)
        except ai_billing.BillingLimitExceeded as exc:
            raise AnalyticsBillingLimitExceededError(str(exc)) from exc
        except ai_billing.BillingConfigurationError as exc:
            raise AnalyticsConfigurationError(str(exc)) from exc

        if request.model_key is not None:
            try:
                main = model_roles.resolve(
                    "main_agent", report_id=report.id, empresa_id=billing_context.empresa_id,
                )
            except model_catalog.ModelSelectionError as exc:
                raise AnalyticsModelError(str(exc)) from exc
            if main.model_key != request.model_key:
                raise AnalyticsModelError("The requested model does not match the effective main model")

        context = ExecutionContext(
            user_message=request.question.strip(),
            dataset_id=dataset_id,
            settings=settings,
            history=list(request.history),
            source=request.source,
            report_id=report.id,
            report_name=report.name,
            empresa_id=billing_context.empresa_id,
            conversation_id=request.execution_id,
            powerbi_credentials=credentials,
            custom_instructions=instructions,
            schema_retrieval_prompt=report.schema_retrieval_prompt,
            schema_table_context_limit=report.schema_table_context_limit,
            schema_measure_context_limit=report.schema_measure_context_limit,
            requested_model_key=request.model_key,
            role_configuration=model_roles,
            cache_policy=request.cache_policy,
            billing_context=billing_context,
            trace_context=dict(request.trace_context),
        )
        return PreparedAnalyticsExecution(
            request=replace(
                request,
                history=list(request.history),
                trace_context=dict(request.trace_context),
            ),
            context=context,
            owner=self._prepare_token,
        )

    async def prepare(self, request: AnalyticsRequest) -> PreparedAnalyticsExecution:
        self._validate_request(request)
        return await asyncio.to_thread(self._prepare_sync, request)

    async def execute_prepared(
        self,
        prepared: PreparedAnalyticsExecution,
        *,
        history: list[dict[str, Any]] | None = None,
        execution_id: str | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> AnalyticsResult:
        if not isinstance(prepared, PreparedAnalyticsExecution) or prepared.owner is not self._prepare_token:
            raise AnalyticsConfigurationError("prepared execution is invalid")
        if history is not None and not isinstance(history, list):
            raise AnalyticsConfigurationError("history must be a list")
        if trace_context is not None and not isinstance(trace_context, dict):
            raise AnalyticsConfigurationError("trace_context must be a dictionary")
        context = replace(
            prepared.context,
            history=list(history) if history is not None else list(prepared.request.history),
            conversation_id=execution_id if execution_id is not None else prepared.request.execution_id,
            trace_context=dict(trace_context) if trace_context is not None else dict(prepared.request.trace_context),
        )
        turn = await self.execution_service.execute(context)
        return self._map_result(prepared.request, turn.to_dict())

    async def execute(self, request: AnalyticsRequest) -> AnalyticsResult:
        prepared = await self.prepare(request)
        return await self.execute_prepared(prepared)


def build_analytics_engine(config: dict[str, Any], *, runtime=None) -> KlaraAnalyticsEngine:
    return KlaraAnalyticsEngine(config=config, runtime=runtime)
