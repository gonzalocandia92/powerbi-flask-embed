"""Billing and AI usage ledger helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import calendar
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import func

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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def calculate_spend(
    *,
    scope_type: str,
    scope_id: Optional[str],
    cycle_start: datetime,
    cycle_end: datetime,
) -> float:
    query = AIUsageEvent.query.filter(
        AIUsageEvent.billing_scope_type == scope_type,
        AIUsageEvent.created_at >= cycle_start,
        AIUsageEvent.created_at < cycle_end,
    )
    if scope_id is None:
        query = query.filter(AIUsageEvent.billing_scope_id.is_(None))
    else:
        query = query.filter(AIUsageEvent.billing_scope_id == scope_id)

    total = query.with_entities(func.coalesce(func.sum(AIUsageEvent.total_cost_usd), 0.0)).scalar()
    return float(total or 0.0)


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
    spent_usd = calculate_spend(
        scope_type=context.billing_scope_type,
        scope_id=context.billing_scope_id,
        cycle_start=window.cycle_start,
        cycle_end=window.cycle_end,
    )
    credit_usd = float(active_limit.limit_usd or 0.0)
    remaining_usd = max(0.0, credit_usd - spent_usd)
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
    at: Optional[datetime] = None,
) -> AIModelPricing:
    reference_time = at or utcnow()
    pricing = (
        AIModelPricing.query
        .filter(
            AIModelPricing.provider == provider,
            AIModelPricing.model == model,
            AIModelPricing.event_type == event_type,
            AIModelPricing.is_active.is_(True),
            AIModelPricing.effective_from <= reference_time,
            AIModelPricing.effective_to.is_(None) | (AIModelPricing.effective_to >= reference_time),
        )
        .order_by(AIModelPricing.effective_from.desc(), AIModelPricing.id.desc())
        .first()
    )
    if pricing is None:
        raise BillingConfigurationError(
            f"No hay pricing activo para provider={provider} model={model} event_type={event_type}"
        )
    return pricing


def calculate_cost_breakdown(
    pricing: AIModelPricing,
    *,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cache_write_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
) -> Dict[str, float]:
    input_tokens = int(input_tokens or 0)
    output_tokens = int(output_tokens or 0)
    cache_write_tokens = int(cache_write_tokens or 0)
    cache_read_tokens = int(cache_read_tokens or 0)

    input_cost = input_tokens * float(pricing.input_cost_per_million_usd or 0.0) / 1_000_000
    output_cost = output_tokens * float(pricing.output_cost_per_million_usd or 0.0) / 1_000_000
    cache_write_cost = cache_write_tokens * float(pricing.cache_write_cost_per_million_usd or 0.0) / 1_000_000
    cache_read_cost = cache_read_tokens * float(pricing.cache_read_cost_per_million_usd or 0.0) / 1_000_000
    total_cost = input_cost + output_cost + cache_write_cost + cache_read_cost

    return {
        "input_cost_usd": input_cost,
        "output_cost_usd": output_cost,
        "cache_write_cost_usd": cache_write_cost,
        "cache_read_cost_usd": cache_read_cost,
        "total_cost_usd": total_cost,
    }


def record_ai_usage_event(
    *,
    provider: str,
    model: str,
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
    pricing = resolve_pricing(provider=provider, model=model, event_type=event_type, at=created_at)

    resolved_workspace_id = workspace_id if workspace_id is not None else context.workspace_id
    resolved_report_id = report_id if report_id is not None else context.report_id
    resolved_empresa_id = empresa_id if empresa_id is not None else context.empresa_id
    resolved_scope_type = billing_scope_type or context.billing_scope_type
    resolved_scope_id = billing_scope_id if billing_scope_id is not None else context.billing_scope_id

    computed_total_tokens = total_tokens
    if computed_total_tokens is None:
        computed_total_tokens = int(input_tokens or 0) + int(output_tokens or 0)

    costs = calculate_cost_breakdown(
        pricing,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_tokens=cache_read_tokens,
    )

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
        pricing_id=pricing.id,
        trace_id=trace_id,
        observation_id=observation_id,
        metadata_json=metadata_json or None,
    )
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
        )
        .first()
    )
    message.total_cost_usd = float(totals[0] or 0.0)
    message.total_input_tokens = int(totals[1] or 0)
    message.total_output_tokens = int(totals[2] or 0)
    db.session.flush()
    return message
