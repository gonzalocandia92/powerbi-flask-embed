"""ReportCostService: what did ONE report generation cost?

Reporting never prices anything. Every figure here comes from the AI usage ledger
(``AIUsageEvent``): the ``total_cost_usd`` that ``ai_billing.record_ai_usage_event``
already resolved (pricing, service tier, cache, context band, calendar). This module
only *selects* the events of a ``report_run_id``, *groups* them semantically
(stage -> component -> model / call) and *adds them up*. There is no
``tokens * price`` anywhere.

Source of truth is the internal ledger, not Langfuse: a Langfuse parent span would
double-count its child generations, while the ledger has exactly one row per billable
provider call (each writer/coordinator attempt, each AnalyticsExecutor generation).

The summary is provider-neutral and does not care where the events come from:
``summarize_events`` takes any iterable of ledger-shaped rows, so a future persisted
``ReportRun`` (or a Fixed vs Coordinated comparator) can feed it the same way.

Billing honesty: only ``verified`` events add to ``cost_usd``. A call whose pricing is
pending is never counted as zero: it is reported as ``estimated`` (the ledger holds a
conservative reservation, kept apart in ``estimated_cost_usd``) or ``missing``.
``unbilled`` marks diagnostic-only events that the ledger itself records as $0.

Known limits (documented, not papered over):
* Attribution to ``section_00N`` / ``extra_00N`` relies on the ledger's
  ``report_section_key`` metadata; events without it are still counted in their
  stage/component, they are just not assigned to a section.
* Per-call latency is only known for events that recorded ``latency_ms`` (writer and
  coordinator do; AnalyticsExecutor events do not yet). ``latency_complete`` says so.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping

from app.services import ai_billing

STAGE_ORDER = ("analysis", "coordination", "extra_analysis", "writing")
STAGE_LABELS = {
    "analysis": "Análisis inicial",
    "coordination": "Coordinación",
    "extra_analysis": "Análisis adicionales",
    "writing": "Escritura",
    "other": "Otros",
}
COMPONENT_LABELS = {
    "main_agent": "Main agent",
    "query_rewriter": "Query rewriter",
    "skill_selector": "Skill selector",
    "skill_router": "Skill router",
    "complexity_classifier": "Complexity classifier",
    "schema_retrieval": "Schema retrieval",
    "report_coordinator": "Report coordinator",
    "report_writer": "Report writer",
}
STATUS_VERIFIED = "verified"
STATUS_ESTIMATED = "estimated"
STATUS_MISSING = "missing"
STATUS_UNBILLED = "unbilled"

_ZERO = Decimal(0)
_USD_QUANT = Decimal("0.000001")


def format_usd(value: Decimal | None) -> str | None:
    """Common display format for report costs: six decimals, never a rounded ``$0.02``."""
    if value is None:
        return None
    quantized = value.quantize(_USD_QUANT)
    if value > 0 and quantized == 0:
        return "USD <0.000001"
    return f"USD {quantized:f}"


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _plain(value: Decimal) -> str:
    """Exact decimal string without exponent or ledger padding zeros (``0.007``, never ``7E-3``)."""
    return format(value.normalize(), "f") if value else "0"


def _money_payload(value: Decimal | None) -> dict[str, str | None]:
    return {"usd": None if value is None else _plain(value), "display": format_usd(value)}


# ------------------------------------------------------------------------------ DTOs
@dataclass(frozen=True)
class CostFigures:
    """Additive figures shared by every level of the summary (call sums, never prices)."""
    cost_usd: Decimal                 # verified ledger cost only
    estimated_cost_usd: Decimal       # ledger reservations for unverified calls (NOT in cost_usd)
    calls: int
    verified_calls: int
    estimated_calls: int
    missing_calls: int
    unbilled_calls: int
    input_tokens: int                 # total input as reported (includes cached input)
    output_tokens: int
    cached_input_tokens: int          # cache reads
    cache_write_tokens: int
    reasoning_tokens: int | None      # None when no call reported it
    latency_ms: int | None            # sum of the calls that recorded it; None if none did
    latency_complete: bool
    repair_calls: int
    repair_cost_usd: Decimal

    @property
    def is_exact(self) -> bool:
        return self.estimated_calls == 0 and self.missing_calls == 0

    def figures_payload(self) -> dict[str, Any]:
        return {
            "cost_usd": _plain(self.cost_usd), "cost_display": format_usd(self.cost_usd),
            "estimated_cost_usd": _plain(self.estimated_cost_usd),
            "estimated_cost_display": format_usd(self.estimated_cost_usd),
            "calls": self.calls, "verified_calls": self.verified_calls,
            "estimated_calls": self.estimated_calls, "missing_calls": self.missing_calls,
            "unbilled_calls": self.unbilled_calls, "is_exact": self.is_exact,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens, "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "latency_ms": self.latency_ms, "latency_complete": self.latency_complete,
            "repair_calls": self.repair_calls,
            "repair_cost_usd": _plain(self.repair_cost_usd), "repair_cost_display": format_usd(self.repair_cost_usd),
        }


@dataclass(frozen=True)
class ReportCostCall:
    """One real ledger event (= one billable provider call). Admin/technical detail only."""
    call_id: int | None
    stage: str
    component: str
    label: str
    section_key: str | None
    operation: str | None
    kind: str | None                  # "initial" | "repair" | "retry" | None (unknown)
    attempt: int | None
    event_type: str
    provider: str
    model: str
    model_key: str | None
    service_tier: str | None
    thinking: str | None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int | None
    latency_ms: int | None
    cost_usd: Decimal | None          # verified cost; None if not verified
    estimated_cost_usd: Decimal | None
    billing_status: str
    ledger_status: str

    def to_payload(self) -> dict[str, Any]:
        cost = _money_payload(self.cost_usd)
        return {
            "id": self.call_id, "stage": self.stage, "component": self.component, "label": self.label,
            "section_key": self.section_key, "operation": self.operation, "kind": self.kind,
            "attempt": self.attempt, "event_type": self.event_type, "provider": self.provider,
            "model": self.model, "model_key": self.model_key, "service_tier": self.service_tier,
            "thinking": self.thinking, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens, "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens, "latency_ms": self.latency_ms,
            "cost_usd": cost["usd"], "cost_display": cost["display"],
            "estimated_cost_display": format_usd(self.estimated_cost_usd),
            "billing_status": self.billing_status, "status": self.ledger_status,
        }


@dataclass(frozen=True)
class ReportModelCost(CostFigures):
    provider: str
    model: str
    model_key: str | None

    def to_payload(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "model_key": self.model_key,
                **self.figures_payload()}


@dataclass(frozen=True)
class ReportComponentCost(CostFigures):
    stage: str
    component: str
    label: str
    model_breakdown: tuple[ReportModelCost, ...]
    calls_detail: tuple[ReportCostCall, ...]

    def to_payload(self) -> dict[str, Any]:
        return {"stage": self.stage, "component": self.component, "label": self.label,
                **self.figures_payload(),
                "models": [item.to_payload() for item in self.model_breakdown],
                "calls_detail": [item.to_payload() for item in self.calls_detail]}


@dataclass(frozen=True)
class ReportSectionCost(CostFigures):
    key: str
    title: str | None

    def to_payload(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title, **self.figures_payload()}


@dataclass(frozen=True)
class ReportStageCost(CostFigures):
    stage: str
    label: str
    sections: tuple[ReportSectionCost, ...]

    def to_payload(self) -> dict[str, Any]:
        return {"stage": self.stage, "label": self.label, **self.figures_payload(),
                "sections": [item.to_payload() for item in self.sections]}


@dataclass(frozen=True)
class ReportCostSummary(CostFigures):
    report_run_id: str
    stages: tuple[ReportStageCost, ...]
    components: tuple[ReportComponentCost, ...]
    models: tuple[ReportModelCost, ...]

    # Names used by the requirements/contract; same values as the shared figures.
    @property
    def total_cost_usd(self) -> Decimal:
        return self.cost_usd

    @property
    def total_calls(self) -> int:
        return self.calls

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self.output_tokens

    @property
    def total_cached_input_tokens(self) -> int:
        return self.cached_input_tokens

    @property
    def total_reasoning_tokens(self) -> int | None:
        return self.reasoning_tokens

    @property
    def total_latency_ms(self) -> int | None:
        return self.latency_ms

    def to_payload(self) -> dict[str, Any]:
        return {"report_run_id": self.report_run_id, **self.figures_payload(),
                "stages": [item.to_payload() for item in self.stages],
                "components": [item.to_payload() for item in self.components],
                "models": [item.to_payload() for item in self.models]}


# ------------------------------------------------------------------------ aggregation
def _figures(calls: list[ReportCostCall]) -> dict[str, Any]:
    reasoning = [c.reasoning_tokens for c in calls if c.reasoning_tokens is not None]
    latency = [c.latency_ms for c in calls if c.latency_ms is not None]
    repairs = [c for c in calls if c.kind in ("repair", "retry")]
    return {
        "cost_usd": sum((c.cost_usd for c in calls if c.cost_usd is not None), _ZERO),
        "estimated_cost_usd": sum((c.estimated_cost_usd for c in calls if c.estimated_cost_usd is not None), _ZERO),
        "calls": len(calls),
        "verified_calls": sum(c.billing_status == STATUS_VERIFIED for c in calls),
        "estimated_calls": sum(c.billing_status == STATUS_ESTIMATED for c in calls),
        "missing_calls": sum(c.billing_status == STATUS_MISSING for c in calls),
        "unbilled_calls": sum(c.billing_status == STATUS_UNBILLED for c in calls),
        "input_tokens": sum(c.input_tokens for c in calls),
        "output_tokens": sum(c.output_tokens for c in calls),
        "cached_input_tokens": sum(c.cached_input_tokens for c in calls),
        "cache_write_tokens": sum(c.cache_write_tokens for c in calls),
        "reasoning_tokens": sum(reasoning) if reasoning else None,
        "latency_ms": sum(latency) if latency else None,
        "latency_complete": len(latency) == len(calls),
        "repair_calls": len(repairs),
        "repair_cost_usd": sum((c.cost_usd for c in repairs if c.cost_usd is not None), _ZERO),
    }


def _classify(event: Any) -> tuple[str, Decimal | None, Decimal | None]:
    """(billing_status, verified_cost, estimated_cost) exactly as the ledger recorded it."""
    ledger_status = getattr(event, "billing_status", None)
    cost = _dec(getattr(event, "total_cost_usd", None))
    reserved = _dec(getattr(event, "reserved_cost_usd", None))
    if ledger_status == "unbilled_diagnostic":
        return STATUS_UNBILLED, cost if cost is not None else _ZERO, None
    if ledger_status == "verified" and cost is not None:
        return STATUS_VERIFIED, cost, None
    if reserved is not None:
        return STATUS_ESTIMATED, None, reserved
    return STATUS_MISSING, None, None


def _call_from_event(event: Any) -> ReportCostCall:
    metadata = getattr(event, "metadata_json", None) or {}
    usage = metadata.get("normalized_usage") if isinstance(metadata.get("normalized_usage"), Mapping) else {}
    stage = metadata.get("report_stage") or "other"
    component = metadata.get("component") or getattr(event, "source_type", None) or "unknown"
    billing_status, cost, estimated = _classify(event)

    cache_read = _int(getattr(event, "cache_read_tokens", None))
    if cache_read is None:
        cache_read = _int(getattr(event, "cached_input_tokens", None)) or 0
    cache_write = _int(getattr(event, "cache_write_tokens", None)) or 0
    uncached = _int(getattr(event, "input_tokens", None)) or 0
    input_total = _int(usage.get("input_total_tokens"))
    if input_total is None:
        input_total = uncached + cache_read + cache_write

    if metadata.get("repair_retry") is True:
        kind = "repair"
    elif metadata.get("retry"):
        kind = "retry"
    elif "attempt" in metadata or "repair_retry" in metadata:
        kind = "initial"
    else:
        kind = None
    thinking = metadata.get("thinking_level") or getattr(event, "effective_thinking_mode", None) \
        or metadata.get("effective_thinking_mode")
    section_key = metadata.get("report_section_key")
    return ReportCostCall(
        call_id=getattr(event, "id", None), stage=stage, component=component,
        label=COMPONENT_LABELS.get(component, component.replace("_", " ").capitalize()),
        section_key=section_key if isinstance(section_key, str) else None,
        operation=getattr(event, "operation_name", None), kind=kind, attempt=_int(metadata.get("attempt")),
        event_type=getattr(event, "event_type", None) or "generation",
        provider=getattr(event, "provider", None) or "unknown",
        model=getattr(event, "actual_model", None) or getattr(event, "model", None) or "unknown",
        model_key=getattr(event, "model_key", None), service_tier=getattr(event, "service_tier", None),
        thinking=thinking if isinstance(thinking, str) else None,
        input_tokens=input_total, output_tokens=_int(getattr(event, "output_tokens", None)) or 0,
        cached_input_tokens=cache_read, cache_write_tokens=cache_write,
        reasoning_tokens=_int(usage.get("reasoning_tokens")) if usage else None,
        latency_ms=_int(metadata.get("latency_ms")),
        cost_usd=cost, estimated_cost_usd=estimated, billing_status=billing_status,
        ledger_status=getattr(event, "status", None) or "success",
    )


def _group(calls: Iterable[ReportCostCall], key: Callable[[ReportCostCall], Any]) -> dict[Any, list[ReportCostCall]]:
    groups: dict[Any, list[ReportCostCall]] = {}
    for call in calls:  # dicts keep first-seen (chronological) order
        groups.setdefault(key(call), []).append(call)
    return groups


def _model_costs(calls: list[ReportCostCall]) -> tuple[ReportModelCost, ...]:
    rows = [ReportModelCost(provider=provider, model=model, model_key=model_key, **_figures(group))
            for (provider, model, model_key), group in
            _group(calls, lambda c: (c.provider, c.model, c.model_key)).items()]
    return tuple(rows)


def _stage_sort(stage: str) -> tuple[int, str]:
    return (STAGE_ORDER.index(stage), "") if stage in STAGE_ORDER else (len(STAGE_ORDER), stage)


def summarize_events(report_run_id: str, events: Iterable[Any], *,
                     section_titles: Mapping[str, str] | None = None) -> ReportCostSummary:
    """Pure aggregation over ledger-shaped rows of ONE run.

    Rows that do not belong to ``report_run_id`` are ignored (defence in depth on top
    of the ledger query), so a caller can never mix runs by accident.
    """
    if not isinstance(report_run_id, str) or not report_run_id.strip():
        raise ValueError("report_run_id is required")
    titles = dict(section_titles or {})
    calls = [_call_from_event(event) for event in events
             if ((getattr(event, "metadata_json", None) or {}).get("report_run_id")) == report_run_id]

    by_stage = _group(calls, lambda c: c.stage)
    stages = []
    components = []
    for stage in sorted(by_stage, key=_stage_sort):
        stage_calls = by_stage[stage]
        section_groups = _group([c for c in stage_calls if c.section_key], lambda c: c.section_key)
        stages.append(ReportStageCost(
            stage=stage, label=STAGE_LABELS.get(stage, stage.replace("_", " ").capitalize()),
            sections=tuple(ReportSectionCost(key=key, title=titles.get(key), **_figures(group))
                           for key, group in sorted(section_groups.items())),
            **_figures(stage_calls)))
        for component, group in _group(stage_calls, lambda c: c.component).items():
            components.append(ReportComponentCost(
                stage=stage, component=component, label=group[0].label,
                model_breakdown=_model_costs(group), calls_detail=tuple(group), **_figures(group)))

    return ReportCostSummary(
        report_run_id=report_run_id, stages=tuple(stages), components=tuple(components),
        models=_model_costs(calls), **_figures(calls))


class ReportCostService:
    """Reads the ledger for one ``report_run_id`` and returns a ``ReportCostSummary``."""

    def __init__(self, load_events: Callable[..., Iterable[Any]] | None = None):
        self._load_events = load_events or ai_billing.list_usage_events_for_report_run

    def summarize(self, report_run_id: str, *, report_id: int | None = None,
                  section_titles: Mapping[str, str] | None = None) -> ReportCostSummary:
        events = self._load_events(report_run_id, report_id=report_id)
        return summarize_events(report_run_id, events, section_titles=section_titles)
