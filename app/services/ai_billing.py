"""Billing and AI usage ledger helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import calendar
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import case, func, or_

from app import db
from app.models import AIModelPricing, AIUsageEvent, BillingLimit, ChatMessage, Report

BILLING_SCOPE_EMPRESA = "empresa"
BILLING_SCOPE_GLOBAL = "global"
BILLING_PERIOD_MONTHLY_ANNIVERSARY = "monthly_anniversary"
DEFAULT_BLOCKED_MESSAGE = "La empresa supero el limite configurado de consumo AI."


class BillingConfigurationError(RuntimeError):
    """Raised when billing configuration is incomplete or inconsistent."""


class BillingLimitExceeded(RuntimeError):
    """Raised when the available prepaid balance for the active cycle is exhausted."""


@dataclass(frozen=True)
class BillingContext:
    workspace_id: Optional[int]
    report_id: Optional[int]
    empresa_id: Optional[int]
    billing_scope_type: str
    billing_scope_id: Optional[str]


@dataclass(frozen=True)
class BillingCycleWindow:
    cycle_start: datetime
    cycle_end: datetime
    anchor_day: int


def summarize_pipeline_costs(events) -> Dict[str, float]:
    """Partition persisted ledger costs without creating additional usage events."""
    main = decision = total = Decimal(0)
    for event in events:
        cost = _money(event.total_cost_usd)
        total += cost
        metadata = event.metadata_json or {}
        component = metadata.get("component")
        if component == "main_agent" or event.operation_name in {"chat-response", "chat-response-fallback"}:
            main += cost
        elif component in {"query_rewriter", "skill_selector", "complexity_classifier"} or event.source_type.startswith("skill_router"):
            decision += cost
    return {"main_model_cost": float(main), "decision_layer_cost": float(decision),
            "pipeline_total_cost": float(total)}


def generation_cost_details(model, usage, *, response=None) -> Dict[str, float]:
    """Use the auditable pricing table for generation telemetry as well as billing."""
    return generation_cost_details_for_response(model, usage, response=response)


def _money(value) -> Decimal:
    return Decimal(str(value or 0))


def generation_cost_details_for_response(model, usage, *, response=None) -> Dict[str, float]:
    """The same quote used by runtime telemetry and persisted usage events."""
    from app.services.llm.profiles import PROFILES
    profile = PROFILES.get(model.family_key) if getattr(model, 'family_key', None) else None
    at = utcnow()
    if response and getattr(response, 'provider_created_at', None):
        at = datetime.fromtimestamp(int(response.provider_created_at), timezone.utc)
    elif response and getattr(response, 'thinking_decision', None):
        started = response.thinking_decision.get('request_started_at')
        if started:
            at = datetime.fromisoformat(started)
    actual = response.model if response else model.physical_model
    actual_tier = response.actual_service_tier if response else None
    requested_tier = model.service_tier
    tier = None if actual_tier in (None, 'default') else actual_tier
    if profile:
        if usage.estimated:
            return {"billing_status": "pending_reconciliation", "billing_reason": "usage_unverified"}
        if (not actual or not profile.supports(model.provider, actual, model.gateway) or
                profile.billing_model(actual) != profile.billing_model(model.physical_model)):
            return {"billing_status": "pending_reconciliation", "billing_reason": "model_mismatch"}
        if requested_tier and actual_tier is None:
            return {"billing_status": "pending_reconciliation", "billing_reason": "service_tier_unverified"}
        if actual_tier is not None and tier != requested_tier:
            return {"billing_status": "pending_reconciliation", "billing_reason": "service_tier_mismatch"}
        band = profile.pricing_band(at, usage)
        context_band = profile.context_band(usage)
        lookup_model = profile.billing_model(actual)
    else:
        band, context_band, lookup_model = model.pricing_tier, None, model.physical_model
        tier = requested_tier
    pricing = resolve_pricing(provider=model.provider, model=lookup_model, event_type='generation',
                              service_tier=tier, pricing_tier=band, gateway=model.gateway if profile else None,
                              context_band=context_band, at=at, strict=bool(profile))
    _validate_price_columns(pricing, profile, usage)
    costs = calculate_cost_breakdown(pricing, **usage.ledger_fields())
    calendar_version = (f"cn-public-holidays-{(at.astimezone(timezone.utc) + timedelta(hours=8)).year}"
                        if profile and profile.key == 'deepseek-v4' else None)
    return {"input": float(costs["input_cost_usd"]), "output": float(costs["output_cost_usd"]),
            "cache_write": float(costs["cache_write_cost_usd"]), "cache_read": float(costs["cache_read_cost_usd"]),
            "total": float(costs["total_cost_usd"]), "pricing_id": pricing.id,
            "pricing_tier": band, "context_band": context_band, "service_tier": tier,
            "calendar_version": calendar_version,
            "billing_status": "verified"}


def validate_pricing_coverage(model) -> None:
    """Fail preflight unless all price bands this profile may use are present."""
    from app.services.llm.profiles import PROFILES
    profile = PROFILES.get(getattr(model, 'family_key', None))
    if profile is None:
        resolve_pricing(provider=model.provider, model=model.physical_model,
                        event_type='generation', service_tier=model.service_tier,
                        pricing_tier=model.pricing_tier)
        return
    profile.validate(model)
    if profile.key == 'deepseek-v4':
        from app.services.llm.pricing_calendar import deepseek_pricing_band
        deepseek_pricing_band(utcnow())
    bands = ('peak', 'off-peak') if profile.key == 'deepseek-v4' else (None,)
    contexts = ('short', 'long') if profile.key == 'openai-gpt-5.6' else (None,)
    for band in bands:
        for context in contexts:
            pricing = resolve_pricing(provider=model.provider, model=profile.billing_model(model.physical_model),
                                      event_type='generation', service_tier=model.service_tier,
                                      pricing_tier=band, gateway=model.gateway, context_band=context, strict=True)
            _validate_price_columns(pricing, profile)


def _validate_price_columns(pricing, profile, usage=None) -> None:
    required = ['input_cost_per_million_usd', 'output_cost_per_million_usd']
    if profile is not None:
        if profile.key in {'claude-haiku-4.5', 'openai-gpt-5.6'}:
            required.extend(('cache_read_cost_per_million_usd', 'cache_write_cost_per_million_usd'))
        elif profile.key in {'deepseek-v4', 'openai-gpt-4.1'}:
            required.append('cache_read_cost_per_million_usd')
    if usage is not None:
        if usage.cache_read_tokens:
            required.append('cache_read_cost_per_million_usd')
        if usage.cache_write_tokens:
            required.append('cache_write_cost_per_million_usd')
    missing = {field for field in required if getattr(pricing, field) is None}
    if missing:
        raise BillingConfigurationError(f"Tarifa {pricing.id} incompleta: {', '.join(sorted(missing))}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc_naive(value: datetime) -> datetime:
    """The existing SQL columns are timestamp-without-time-zone in UTC."""
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _using_sqlite() -> bool:
    bind = db.session.get_bind()
    return bool(bind and bind.dialect.name == "sqlite")


def _next_integer_id(model) -> int:
    current_max = db.session.query(func.max(model.id)).scalar()
    return int(current_max or 0) + 1


def _prepare_sqlite_id(instance, model) -> None:
    if _using_sqlite() and getattr(instance, "id", None) is None:
        setattr(instance, "id", _next_integer_id(model))


def _days_in_month(year: int, month: int) -> int:
    return int(calendar.monthrange(year, month)[1])


def _clamp_anchor_day(year: int, month: int, anchor_day: int) -> int:
    return max(1, min(int(anchor_day), _days_in_month(year, month)))


def _month_shift(year: int, month: int, offset: int) -> Tuple[int, int]:
    absolute_month = (year * 12) + (month - 1) + offset
    shifted_year = absolute_month // 12
    shifted_month = (absolute_month % 12) + 1
    return shifted_year, shifted_month


def _replace_month_with_anchor(dt: datetime, year: int, month: int, anchor_day: int) -> datetime:
    safe_day = _clamp_anchor_day(year, month, anchor_day)
    return dt.replace(year=year, month=month, day=safe_day, hour=0, minute=0, second=0, microsecond=0)


def resolve_cycle_anchor_day(limit: BillingLimit) -> int:
    if limit.cycle_anchor_day:
        return max(1, min(int(limit.cycle_anchor_day), 31))
    if limit.starts_at is not None:
        return int(limit.starts_at.day)
    if limit.created_at is not None:
        return int(limit.created_at.day)
    return 1


def monthly_anniversary_window(limit: BillingLimit, *, as_of: Optional[datetime] = None) -> BillingCycleWindow:
    reference_time = as_of or utcnow()
    anchor_day = resolve_cycle_anchor_day(limit)

    current_month_anchor = _replace_month_with_anchor(reference_time, reference_time.year, reference_time.month, anchor_day)
    if reference_time >= current_month_anchor:
        cycle_start = current_month_anchor
    else:
        previous_year, previous_month = _month_shift(reference_time.year, reference_time.month, -1)
        cycle_start = _replace_month_with_anchor(reference_time, previous_year, previous_month, anchor_day)

    next_year, next_month = _month_shift(cycle_start.year, cycle_start.month, 1)
    cycle_end = _replace_month_with_anchor(reference_time, next_year, next_month, anchor_day)
    return BillingCycleWindow(cycle_start=cycle_start, cycle_end=cycle_end, anchor_day=anchor_day)


def resolve_report_billing_context(report: Optional[Report]) -> BillingContext:
    if report is None:
        return BillingContext(
            workspace_id=None,
            report_id=None,
            empresa_id=None,
            billing_scope_type=BILLING_SCOPE_GLOBAL,
            billing_scope_id=None,
        )

    empresa_id = getattr(report, "empresa_facturadora_id", None)
    if not empresa_id:
        associated_companies = list(getattr(report, "empresas", []) or [])
        if len(associated_companies) == 1:
            empresa_id = associated_companies[0].id

    if empresa_id:
        return BillingContext(
            workspace_id=report.workspace_id_fk,
            report_id=report.id,
            empresa_id=empresa_id,
            billing_scope_type=BILLING_SCOPE_EMPRESA,
            billing_scope_id=str(empresa_id),
        )

    return BillingContext(
        workspace_id=report.workspace_id_fk,
        report_id=report.id,
        empresa_id=None,
        billing_scope_type=BILLING_SCOPE_GLOBAL,
        billing_scope_id=None,
    )


def resolve_billing_limit(
    *,
    empresa_id: Optional[int],
    as_of: Optional[datetime] = None,
) -> Optional[BillingLimit]:
    reference_time = as_of or utcnow()

    def _base_query(scope_type: str, scope_id: Optional[str]):
        query = BillingLimit.query.filter(
            BillingLimit.scope_type == scope_type,
            BillingLimit.is_active.is_(True),
            BillingLimit.period_type == BILLING_PERIOD_MONTHLY_ANNIVERSARY,
            BillingLimit.starts_at.is_(None) | (BillingLimit.starts_at <= reference_time),
            BillingLimit.ends_at.is_(None) | (BillingLimit.ends_at >= reference_time),
        )
        if scope_id is None:
            query = query.filter(BillingLimit.scope_id.is_(None))
        else:
            query = query.filter(BillingLimit.scope_id == scope_id)
        return query.order_by(BillingLimit.id.desc())

    if empresa_id is not None:
        empresa_limit = _base_query(BILLING_SCOPE_EMPRESA, str(empresa_id)).first()
        if empresa_limit is not None:
            return empresa_limit

    return _base_query(BILLING_SCOPE_GLOBAL, None).first()


def calculate_spend_decimal(
    *,
    scope_type: str,
    scope_id: Optional[str],
    cycle_start: datetime,
    cycle_end: datetime,
) -> Decimal:
    query = AIUsageEvent.query.filter(
        AIUsageEvent.billing_scope_type == scope_type,
        AIUsageEvent.created_at >= _utc_naive(cycle_start),
        AIUsageEvent.created_at < _utc_naive(cycle_end),
    )
    if scope_id is None:
        query = query.filter(AIUsageEvent.billing_scope_id.is_(None))
    else:
        query = query.filter(AIUsageEvent.billing_scope_id == scope_id)

    confirmed, reserved = query.with_entities(
        func.sum(AIUsageEvent.total_cost_usd),
        func.sum(AIUsageEvent.reserved_cost_usd),
    ).one()
    return _money(confirmed) + _money(reserved)


def calculate_spend(*, scope_type: str, scope_id: Optional[str],
                    cycle_start: datetime, cycle_end: datetime) -> float:
    """Legacy public shape; the limit calculation retains Decimal internally."""
    return float(calculate_spend_decimal(scope_type=scope_type, scope_id=scope_id,
                                         cycle_start=cycle_start, cycle_end=cycle_end))


def get_cycle_balance_for_report(
    report: Report,
    *,
    as_of: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    context = resolve_report_billing_context(report)
    active_limit = resolve_billing_limit(empresa_id=context.empresa_id, as_of=as_of)
    if active_limit is None:
        return None

    window = monthly_anniversary_window(active_limit, as_of=as_of)
    spent_decimal = calculate_spend_decimal(
        scope_type=context.billing_scope_type,
        scope_id=context.billing_scope_id,
        cycle_start=window.cycle_start,
        cycle_end=window.cycle_end,
    )
    credit_decimal = _money(active_limit.limit_usd)
    remaining_decimal = max(Decimal(0), credit_decimal - spent_decimal)
    credit_usd = float(credit_decimal)
    spent_usd = float(spent_decimal)
    remaining_usd = float(remaining_decimal)
    return {
        "credit_usd": credit_usd,
        "spent_usd": spent_usd,
        "remaining_usd": remaining_usd,
        "cycle_start": window.cycle_start,
        "cycle_end": window.cycle_end,
        "anchor_day": window.anchor_day,
        "scope_type": context.billing_scope_type,
        "scope_id": context.billing_scope_id,
    }


def enforce_limit_for_report(report: Report, *, as_of: Optional[datetime] = None) -> None:
    cycle_balance = get_cycle_balance_for_report(report, as_of=as_of)
    if cycle_balance is None:
        return

    if float(cycle_balance["remaining_usd"]) <= 0.0:
        raise BillingLimitExceeded(DEFAULT_BLOCKED_MESSAGE)


def resolve_pricing(
    *,
    provider: str,
    model: str,
    event_type: str,
    service_tier: Optional[str] = None,
    pricing_tier: Optional[str] = None,
    gateway: Optional[str] = None,
    context_band: Optional[str] = None,
    at: Optional[datetime] = None,
    strict: bool = False,
) -> AIModelPricing:
    reference_time = _utc_naive(at or utcnow())
    query = AIModelPricing.query.filter(
            AIModelPricing.provider == provider,
            AIModelPricing.model == model,
            AIModelPricing.event_type == event_type,
            AIModelPricing.is_active.is_(True),
            AIModelPricing.effective_from <= reference_time,
            AIModelPricing.effective_to.is_(None) | (AIModelPricing.effective_to >= reference_time),
        )
    ordering = []
    for column, value in ((AIModelPricing.service_tier, service_tier),
                          (AIModelPricing.pricing_tier, pricing_tier)):
        if value is None:
            query = query.filter(column.is_(None))
        else:
            query = query.filter(or_(column == value, column.is_(None)))
            ordering.append(case((column == value, 1), else_=0).desc())
    if strict:
        query = query.filter(AIModelPricing.service_tier == service_tier)
        query = query.filter(AIModelPricing.context_band == context_band)
        if gateway == 'direct':
            query = query.filter(or_(AIModelPricing.gateway == 'direct', AIModelPricing.gateway.is_(None)))
            ordering.append(case((AIModelPricing.gateway == 'direct', 1), else_=0).desc())
        else:
            query = query.filter(AIModelPricing.gateway == gateway)
        if pricing_tier is not None:
            query = query.filter(AIModelPricing.pricing_tier == pricing_tier)
        if context_band is not None:
            query = query.filter(AIModelPricing.context_band == context_band)
    pricing = query.order_by(*ordering, AIModelPricing.effective_from.desc(), AIModelPricing.id.desc()).first()
    if pricing is None:
        raise BillingConfigurationError(
            f"No hay pricing activo para provider={provider} model={model} event_type={event_type} "
            f"service_tier={service_tier} pricing_tier={pricing_tier}"
        )
    return pricing


def overlapping_pricing(*, provider, model, event_type, service_tier, pricing_tier,
                        gateway, context_band, effective_from, effective_to, exclude_id=None):
    query = AIModelPricing.query.filter_by(
        provider=provider, model=model, event_type=event_type,
        service_tier=service_tier, pricing_tier=pricing_tier,
        gateway=gateway, context_band=context_band, is_active=True,
    )
    if exclude_id is not None:
        query = query.filter(AIModelPricing.id != exclude_id)
    if effective_to is not None:
        query = query.filter(AIModelPricing.effective_from <= effective_to)
    query = query.filter(AIModelPricing.effective_to.is_(None) |
                         (AIModelPricing.effective_to >= effective_from))
    return query.first()


def calculate_cost_breakdown(
    pricing: AIModelPricing,
    *,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cache_write_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
) -> Dict[str, Decimal]:
    input_tokens = int(input_tokens or 0)
    output_tokens = int(output_tokens or 0)
    cache_write_tokens = int(cache_write_tokens or 0)
    cache_read_tokens = int(cache_read_tokens or 0)

    input_cost = input_tokens * _money(pricing.input_cost_per_million_usd) / Decimal(1_000_000)
    output_cost = output_tokens * _money(pricing.output_cost_per_million_usd) / Decimal(1_000_000)
    cache_write_cost = cache_write_tokens * _money(pricing.cache_write_cost_per_million_usd) / Decimal(1_000_000)
    cache_read_cost = cache_read_tokens * _money(pricing.cache_read_cost_per_million_usd) / Decimal(1_000_000)
    total_cost = input_cost + output_cost + cache_write_cost + cache_read_cost

    return {
        "input_cost_usd": input_cost,
        "output_cost_usd": output_cost,
        "cache_write_cost_usd": cache_write_cost,
        "cache_read_cost_usd": cache_read_cost,
        "total_cost_usd": total_cost,
    }


def _generation_quote_from_event(provider, model, gateway, service_tier, actual_model, metadata, *,
                                 input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, created_at):
    from types import SimpleNamespace
    from app.services.llm.contracts import LLMUsage
    family_key = metadata.get('family_key')
    usage_data = metadata.get('normalized_usage') or {}
    usage = LLMUsage(
        input_total_tokens=int(usage_data.get('input_total_tokens') or
                               (input_tokens or 0) + (cache_read_tokens or 0) + (cache_write_tokens or 0)),
        input_uncached_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0),
        cache_read_tokens=int(cache_read_tokens or 0), cache_write_tokens=int(cache_write_tokens or 0),
        reasoning_tokens=int(usage_data.get('reasoning_tokens') or 0),
        estimated=bool(usage_data.get('estimated') or not usage_data),
    )
    config = SimpleNamespace(provider=provider, physical_model=model, gateway=gateway or 'direct',
                             service_tier=service_tier, pricing_tier=metadata.get('pricing_tier'),
                             family_key=family_key)
    response = SimpleNamespace(
        model=actual_model or model,
        actual_service_tier=metadata.get('actual_service_tier'),
        provider_created_at=metadata.get('provider_created_at'),
        thinking_decision=metadata,
    )
    return generation_cost_details_for_response(config, usage, response=response)


def _conservative_reserve(provider, model, metadata, input_tokens):
    if metadata.get('billing_pending_reason') == 'model_mismatch':
        return Decimal('1000000')
    from app.services.llm.profiles import PROFILES
    profile = PROFILES.get(metadata.get('family_key'))
    billing_model = profile.billing_model(model) if profile else model
    rates = AIModelPricing.query.filter_by(
        provider=provider, model=billing_model, event_type='generation', is_active=True,
    ).all()
    if not rates:
        return Decimal('1000000')
    estimated_input = max(0, int(metadata.get('normalized_usage', {}).get('input_total_tokens') or input_tokens or 0))
    max_output = max(0, int(metadata.get('max_output_tokens') or 4096))
    return max((max(_money(rate.input_cost_per_million_usd),
                    _money(rate.cache_write_cost_per_million_usd),
                    _money(rate.cache_read_cost_per_million_usd)) * estimated_input +
                _money(rate.output_cost_per_million_usd) * max_output) / Decimal(1_000_000)
               for rate in rates)


def record_ai_usage_event(
    *,
    provider: str,
    model: str,
    model_key: Optional[str] = None,
    gateway: Optional[str] = None,
    service_tier: Optional[str] = None,
    pricing_tier: Optional[str] = None,
    actual_model: Optional[str] = None,
    event_type: str,
    source_type: str,
    trigger_type: str,
    status: str = "success",
    operation_name: Optional[str] = None,
    report: Optional[Report] = None,
    session_id: Optional[int] = None,
    message_id: Optional[int] = None,
    workspace_id: Optional[int] = None,
    report_id: Optional[int] = None,
    empresa_id: Optional[int] = None,
    billing_scope_type: Optional[str] = None,
    billing_scope_id: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cached_input_tokens: Optional[int] = None,
    cache_write_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    trace_id: Optional[str] = None,
    observation_id: Optional[str] = None,
    metadata_json: Optional[Dict[str, Any]] = None,
    created_at: Optional[datetime] = None,
) -> AIUsageEvent:
    if (
        workspace_id is None
        or report_id is None
        or billing_scope_type is None
        or (billing_scope_type == BILLING_SCOPE_EMPRESA and billing_scope_id is None)
    ):
        context = resolve_report_billing_context(report)
    else:
        context = BillingContext(
            workspace_id=workspace_id,
            report_id=report_id,
            empresa_id=empresa_id,
            billing_scope_type=billing_scope_type,
            billing_scope_id=billing_scope_id,
        )
    metadata_json = dict(metadata_json or {})
    model_key = model_key or metadata_json.get("model_key")
    gateway = gateway or metadata_json.get("gateway")
    service_tier = service_tier or metadata_json.get("service_tier")
    pricing_tier = pricing_tier or metadata_json.get("pricing_tier")
    actual_model = actual_model or metadata_json.get("actual_model")
    pricing = None
    quote = None
    pending_reason = None
    unbilled_diagnostic_estimate = status != "success" and metadata_json.get("estimated_usage") is True
    profile_generation = (event_type == 'generation' and bool(metadata_json.get('family_key'))
                          and not unbilled_diagnostic_estimate)
    if profile_generation:
        try:
            quote = _generation_quote_from_event(
                provider, model, gateway, service_tier, actual_model, metadata_json,
                input_tokens=input_tokens, output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens,
                created_at=created_at,
            )
            if quote.get('billing_status') != 'verified':
                pending_reason = quote.get('billing_reason', 'usage_or_model_unverified')
            else:
                pricing = db.session.get(AIModelPricing, quote['pricing_id'])
                pricing_tier = quote['pricing_tier']
                service_tier = quote['service_tier']
                if quote.get('calendar_version'):
                    metadata_json['pricing_calendar_version'] = quote['calendar_version']
        except (BillingConfigurationError, ValueError, OverflowError, TypeError):
            pending_reason = 'pricing_unavailable'
    if pricing is None and not pending_reason and not unbilled_diagnostic_estimate:
        pricing = resolve_pricing(provider=provider, model=model, event_type=event_type,
                                  service_tier=service_tier, pricing_tier=pricing_tier, at=created_at)

    resolved_workspace_id = workspace_id if workspace_id is not None else context.workspace_id
    resolved_report_id = report_id if report_id is not None else context.report_id
    resolved_empresa_id = empresa_id if empresa_id is not None else context.empresa_id
    resolved_scope_type = billing_scope_type or context.billing_scope_type
    resolved_scope_id = billing_scope_id if billing_scope_id is not None else context.billing_scope_id

    computed_total_tokens = total_tokens
    if computed_total_tokens is None:
        computed_total_tokens = int(input_tokens or 0) + int(output_tokens or 0)

    if pending_reason:
        metadata_json['billing_status'] = 'pending_reconciliation'
        metadata_json['billing_pending_reason'] = pending_reason
        costs = {key: None for key in ('input_cost_usd', 'output_cost_usd', 'cache_write_cost_usd',
                                      'cache_read_cost_usd', 'total_cost_usd')}
        reserved_cost = _conservative_reserve(provider, model, metadata_json, input_tokens)
    elif unbilled_diagnostic_estimate:
        metadata_json.setdefault("billable", False)
        metadata_json.setdefault("usage_accounting", "diagnostic_estimate")
        costs = {
            "input_cost_usd": 0.0,
            "output_cost_usd": 0.0,
            "cache_write_cost_usd": 0.0,
            "cache_read_cost_usd": 0.0,
            "total_cost_usd": 0.0,
        }
        reserved_cost = None
    else:
        costs = calculate_cost_breakdown(
            pricing,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        reserved_cost = None

    event = AIUsageEvent(
        created_at=created_at or utcnow(),
        session_id=session_id,
        message_id=message_id,
        workspace_id_fk=resolved_workspace_id,
        report_id_fk=resolved_report_id,
        empresa_id=resolved_empresa_id,
        billing_scope_type=resolved_scope_type,
        billing_scope_id=resolved_scope_id,
        source_type=source_type,
        trigger_type=trigger_type,
        provider=provider,
        model=model,
        model_key=model_key,
        gateway=gateway,
        service_tier=service_tier,
        pricing_tier=pricing_tier,
        context_band=quote.get('context_band') if quote else None,
        billing_status=('pending_reconciliation' if pending_reason else
                        'unbilled_diagnostic' if unbilled_diagnostic_estimate else 'verified'),
        reserved_cost_usd=reserved_cost,
        effective_thinking_mode=metadata_json.get('effective_thinking_mode'),
        actual_model=actual_model,
        event_type=event_type,
        operation_name=operation_name,
        status=status,
        input_tokens=int(input_tokens or 0) if input_tokens is not None else None,
        output_tokens=int(output_tokens or 0) if output_tokens is not None else None,
        total_tokens=int(computed_total_tokens or 0) if computed_total_tokens is not None else None,
        cached_input_tokens=int(cached_input_tokens or 0) if cached_input_tokens is not None else None,
        cache_write_tokens=int(cache_write_tokens or 0) if cache_write_tokens is not None else None,
        cache_read_tokens=int(cache_read_tokens or 0) if cache_read_tokens is not None else None,
        input_cost_usd=costs["input_cost_usd"],
        output_cost_usd=costs["output_cost_usd"],
        cache_write_cost_usd=costs["cache_write_cost_usd"],
        cache_read_cost_usd=costs["cache_read_cost_usd"],
        total_cost_usd=costs["total_cost_usd"],
        currency="USD",
        pricing_id=pricing.id if pricing is not None else None,
        trace_id=trace_id,
        observation_id=observation_id,
        metadata_json=metadata_json or None,
    )
    _prepare_sqlite_id(event, AIUsageEvent)
    db.session.add(event)
    db.session.flush()
    return event


def update_message_usage_totals(message_id: int) -> ChatMessage:
    message = db.session.get(ChatMessage, message_id)
    if message is None:
        raise BillingConfigurationError(f"Chat message not found: {message_id}")

    totals = (
        AIUsageEvent.query
        .filter(AIUsageEvent.message_id == message_id)
        .with_entities(
            func.coalesce(func.sum(AIUsageEvent.total_cost_usd), 0.0),
            func.coalesce(func.sum(AIUsageEvent.input_tokens), 0),
            func.coalesce(func.sum(AIUsageEvent.output_tokens), 0),
            func.coalesce(func.sum(AIUsageEvent.total_tokens), 0),
        )
        .first()
    )
    summed_input_tokens = int(totals[1] or 0)
    summed_output_tokens = int(totals[2] or 0)
    summed_total_tokens = int(totals[3] or 0)
    total_input_tokens = summed_input_tokens
    if summed_total_tokens and summed_total_tokens != summed_input_tokens + summed_output_tokens:
        total_input_tokens = max(0, summed_total_tokens - summed_output_tokens)

    message.total_cost_usd = float(totals[0] or 0.0)
    message.total_input_tokens = total_input_tokens
    message.total_output_tokens = summed_output_tokens
    message.input_tokens = total_input_tokens
    message.output_tokens = summed_output_tokens
    db.session.flush()
    return message
