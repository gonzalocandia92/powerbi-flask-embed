"""ReportCoordinator: decides whether a ``ReportDraft`` needs 0-2 more analyses.

Mirrors ``writer.py``'s shape deliberately: provider-neutral over ``LLMRuntime``,
JSON-mode structured output validated with Pydantic, at most one repair retry for
an invalid JSON/schema response, and a compact input that already strips
everything Power BI/DAX/skill/credential related (see
``coordinator_contracts.CoordinatorInput``). The coordinator is NOT a free agent
over Power BI: it never calls ``AnalyticsExecutor`` itself and it never chooses a
model, tier, DAX or skill for the analyses it requests -- see ``extra_analysis``.
"""
from __future__ import annotations

import inspect
import json
import logging
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from pydantic import ValidationError

from app.services.llm import LLMError, LLMMessage, LLMRequest, ModelConfig
from app.services.llm.contracts import CachePolicy, CacheScope, LLMResponse
from app.services.observability import start_observation
from app.services.semantic_notes import normalize_semantic_notes

from .contracts import ReportDraft
from .coordinator_contracts import (
    CoordinatorConstraints, CoordinatorDecision, CoordinatorInput, CoordinatorSection,
    CoordinatorSemanticNote, coordinator_decision_json_schema,
)

LOG = logging.getLogger(__name__)

COORDINATOR_COMPONENT = "report_coordinator"
COORDINATOR_ROLE = "report_coordinator"
COORDINATOR_STAGE = "coordination"
MAX_VALIDATION_ERRORS = 20
MAX_ERROR_CHARS = 300
MAX_REPAIR_ECHO_CHARS = 60_000
MAX_REPAIR_RETRIES = 1


# --------------------------------------------------------------------------- errors
class CoordinatorError(Exception):
    """Base class; ``code`` is a stable, public identifier.

    Any subclass of this is a *safe* coordinator failure: the caller
    (``CoordinationRunner``) always keeps the original ``ReportDraft`` and
    continues to ``ReportWriter`` as if the strategy had been ``fixed``.
    """
    code = "report_coordinator_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class CoordinatorConfigurationError(CoordinatorError):
    code = "report_coordinator_not_configured"


class CoordinatorExecutionError(CoordinatorError):
    code = "report_coordinator_execution_failed"


class CoordinatorInvalidDecisionError(CoordinatorError):
    code = "report_coordinator_invalid_output"

    def __init__(self, message: str, *, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = list(errors or [])


class CoordinatorBillingLimitError(CoordinatorError):
    """Wraps a billing-limit rejection so ``CoordinationRunner`` can contain it too.

    Resolving the coordinator's model reuses the same billing preflight as the
    writer (see ``coordinator_factory.resolve_report_coordinator``), which raises
    the shared ``ai_billing.BillingLimitExceeded`` directly. That exception is not
    a ``CoordinatorError`` by itself (``ai_billing`` has no reason to know about
    reporting), so the resolver callable handed to ``CoordinationRunner`` is
    expected to re-raise it as this type instead of letting it escape.
    """
    code = "report_coordinator_billing_limit_exceeded"


# ---------------------------------------------------------------------- observability
@dataclass
class CoordinatorAttempt:
    """One provider call of the coordinator, reported to ``on_attempt`` (billing/diagnostics)."""
    attempt: int
    repair: bool
    valid: bool
    model: ModelConfig
    response: LLMResponse
    latency_ms: int
    usage_event: dict[str, Any]
    decision: CoordinatorDecision | None = None
    validation_errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        usage = self.response.usage
        quote = self.response.pricing_quote or {}
        cost = quote.get("total") if quote.get("billing_status") == "verified" else None
        return {
            "attempt": self.attempt, "repair": self.repair, "valid": self.valid,
            "provider": self.model.provider, "model": self.model.physical_model,
            "model_key": self.model.model_key,
            "service_tier": self.model.service_tier,
            "actual_service_tier": self.response.actual_service_tier,
            "input_tokens": usage.input_total_tokens, "output_tokens": usage.output_tokens,
            "cache_read_tokens": self.response.usage.cache_read_tokens, "cache_write_tokens": self.response.usage.cache_write_tokens,
            "latency_ms": self.latency_ms, "cost_usd": cost,
            "finish_reason": self.response.finish_reason,
            "validation_errors": list(self.validation_errors),
            "action": self.decision.action if self.decision else None,
            "requested_analyses_count": len(self.decision.analyses) if self.decision else None,
        }


CoordinatorAttemptHook = Callable[[CoordinatorAttempt], Awaitable[None] | None]


class ReportCoordinator(Protocol):
    async def coordinate(
        self, coordinator_input: CoordinatorInput, *, on_attempt: CoordinatorAttemptHook | None = None,
    ) -> CoordinatorDecision: ...


# ------------------------------------------------------------------------------ prompt
_RULES = """\
Sos el coordinador analítico de KLARA para un informe ya generado (ReportDraft). NO sos un agente libre sobre Power BI: nunca elegís modelo, tier, DAX ni skills, y nunca ejecutás nada vos mismo.

TU ÚNICA DECISIÓN
Con la información compacta que recibís (secciones ya resueltas, contexto semántico y restricciones), decidí exactamente una de estas dos acciones:
- "finish": el ReportDraft actual ya es suficiente para redactar un buen informe.
- "run_analysis": hay 1 o 2 preguntas analíticas adicionales que aportarían valor material al informe.

LÍMITES OBLIGATORIOS
- Como máximo constraints.max_extra_analyses análisis adicionales (nunca más).
- Una sola ronda: no podés pedir una segunda tanda de análisis ni encadenar razonamiento recursivo.
- related_section_keys de cada análisis solicitado sólo puede referenciar keys de sections con status "ok". Nunca una sección "failed".
- Nunca repitas, ni reformules casi textualmente, una pregunta que ya figura en constraints.already_asked_questions.
- Nunca pidas más de un análisis sobre exactamente el mismo hallazgo.

CUÁNDO SÍ PEDIR UN ANÁLISIS ADICIONAL (ejemplos)
- Una variación material entre segmentos (ej. una sucursal cae mucho más que las demás).
- Una anomalía clara o una concentración relevante que el draft menciona pero no explica.
- Un deterioro destacado que un cliente de negocio necesitaría entender mejor.

CUÁNDO NO PEDIR UN ANÁLISIS ADICIONAL
- Curiosidad: no alcanza con que un valor sea el máximo o el mínimo de una distribución para justificar una consulta extra.
- Reformulaciones de preguntas ya respondidas, o análisis redundantes con lo que ya está en el draft.
- Discrepancias entre métricas que semantic_context ya explica (por ejemplo dos totales que usan bases de cálculo distintas): NO investigues algo que el contexto semántico ya aclara como normal.
- Preguntas sin relación directa con el informe, o que el modelo semántico probablemente no pueda responder.
- Preguntas conductuales, causales especulativas o de intención (¿por qué los clientes prefieren...?).
- Análisis que sólo repetirían un ranking o distribución ya disponible en el draft.
La información adicional debe poder mejorar materialmente el informe, no simplemente añadir volumen.

REGLA CRÍTICA SOBRE semantic_context
semantic_context son restricciones analíticas curadas, no sugerencias. Si dos métricas difieren y semantic_context ya explica que usan bases, granularidades, filtros o períodos distintos, esa diferencia NO es una anomalía y no debe disparar un análisis adicional.

FORMATO DE LA RESPUESTA
- summary debe ser una explicación breve y auditable de tu decisión (1-3 oraciones), pensada para un panel de administración. Nunca chain-of-thought, nunca texto libre extenso.
- Cada análisis en "analyses" necesita question (la pregunta analítica concreta y autocontenida que ejecutará KLARA), purpose (por qué se solicita), expected_value (qué información nueva debería aportar) y related_section_keys (qué hallazgo original lo motivó).
- Si action es "finish", analyses debe ser una lista vacía. Si action es "run_analysis", analyses debe tener entre 1 y constraints.max_extra_analyses elementos.
- Respondé únicamente con un objeto JSON válido que cumpla el schema. Sin texto adicional ni bloques de código.
"""


def build_system_prompt() -> str:
    schema = json.dumps(coordinator_decision_json_schema(), ensure_ascii=False, separators=(",", ":"))
    return f"{_RULES}\nSCHEMA JSON DE SALIDA (CoordinatorDecision):\n{schema}\n"


def build_coordinator_input(draft: ReportDraft) -> CoordinatorInput:
    """Compact, redacted view of a ``ReportDraft`` for the coordinator.

    Excludes DAX, tool traces, usage, routing, model identity and any internal
    payload -- exactly like ``writer.compact_draft`` -- and additionally already
    lists every question asked so far (definition + any prior extra) so the
    coordinator cannot ask for the same thing twice.
    """
    sections = []
    for section in draft.sections:
        if section.had_error:
            sections.append(CoordinatorSection(
                key=section.key, title=section.title, question=section.question, status="failed"))
        else:
            sections.append(CoordinatorSection(
                key=section.key, title=section.title, question=section.question, status="ok",
                answer=section.answer, semantic_notes=list(section.semantic_notes)))
    notes_by_text: dict[str, list[str]] = {}
    for section in draft.sections:
        if section.had_error:
            continue
        for note in section.semantic_notes:
            notes_by_text.setdefault(note, []).append(section.key)
    semantic_context = [
        CoordinatorSemanticNote(text=text, source_section_keys=keys)
        for text, keys in notes_by_text.items()
    ]
    already_asked = normalize_semantic_notes([section.question for section in draft.sections])
    return CoordinatorInput(
        report_name=draft.name, report_run_id=draft.report_run_id, sections=sections,
        semantic_context=semantic_context,
        constraints=CoordinatorConstraints(already_asked_questions=already_asked),
    )


# ---------------------------------------------------------------------- parse/validate
def _strip_fences(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def _clip(value: str) -> str:
    return value if len(value) <= MAX_ERROR_CHARS else value[:MAX_ERROR_CHARS] + "…"


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def parse_coordinator_decision(
    text: str, coordinator_input: CoordinatorInput,
) -> tuple[CoordinatorDecision | None, list[str]]:
    """Parse and validate coordinator output. Returns (decision, []) or (None, errors).

    Beyond the ``CoordinatorDecision`` schema itself, this additionally enforces
    the contract's hard constraints: at most ``max_extra_analyses`` analyses,
    ``related_section_keys`` referencing only successful sections, and no
    analysis that duplicates (loosely, case/whitespace-insensitively) a question
    already asked. All of this feeds the same single repair retry as a schema
    error -- these are structural contract violations, not a judgment call.
    """
    try:
        data = json.loads(_strip_fences(text))
    except (ValueError, TypeError) as exc:
        return None, [_clip(f"JSON inválido: {exc}")]
    if not isinstance(data, dict):
        return None, ["La salida debe ser un objeto JSON."]
    try:
        decision = CoordinatorDecision.model_validate(data)
    except ValidationError as exc:
        errors = []
        for item in exc.errors(include_input=False, include_url=False)[:MAX_VALIDATION_ERRORS]:
            path = ".".join(str(part) for part in item.get("loc", ())) or "(raíz)"
            errors.append(_clip(f"{path}: {item.get('msg')}"))
        return None, errors

    errors: list[str] = []
    max_extra = coordinator_input.constraints.max_extra_analyses
    if len(decision.analyses) > max_extra:
        errors.append(_clip(f"analyses no puede tener más de {max_extra} elemento(s)."))

    ok_keys = coordinator_input.ok_section_keys()
    already_asked = {_normalized(question) for question in coordinator_input.constraints.already_asked_questions}
    seen_in_decision: set[str] = set()
    for index, analysis in enumerate(decision.analyses):
        unknown = sorted(set(analysis.related_section_keys) - ok_keys)
        if unknown:
            errors.append(_clip(
                f"analyses[{index}].related_section_keys sólo puede referenciar secciones exitosas; "
                f"no son válidas: {', '.join(unknown)}. Secciones exitosas disponibles: "
                + ", ".join(sorted(ok_keys))))
        normalized_question = _normalized(analysis.question)
        if normalized_question in already_asked:
            errors.append(_clip(
                f"analyses[{index}].question repite (o reformula casi textualmente) una pregunta ya ejecutada."))
        if normalized_question in seen_in_decision:
            errors.append(_clip(f"analyses[{index}].question está duplicada dentro de la misma decisión."))
        seen_in_decision.add(normalized_question)

    if errors:
        return None, errors
    return decision, []


def _repair_message(errors: list[str]) -> str:
    listed = "\n".join(f"- {error}" for error in errors[:MAX_VALIDATION_ERRORS])
    return ("Tu respuesta anterior no cumple el contrato de CoordinatorDecision. Corregí únicamente estos "
            "errores y devolvé el JSON completo y válido, sin texto adicional:\n" + listed)


# ------------------------------------------------------------------------- observability
def _stage_observation(coordinator_input: CoordinatorInput):
    try:
        return start_observation(
            name="report-coordinator", as_type="span",
            metadata={"report_run_id": coordinator_input.report_run_id, "report_stage": COORDINATOR_STAGE,
                      "component": COORDINATOR_COMPONENT})
    except Exception:  # tracing is never allowed to break coordination
        LOG.warning("Langfuse observation could not be started for report coordinator", exc_info=True)
        return nullcontext(None)


def _usage_event(model: ModelConfig, response: LLMResponse, *, coordinator_input: CoordinatorInput,
                 attempt: int, repair: bool, decision: CoordinatorDecision | None) -> dict[str, Any]:
    ledger = response.usage.ledger_fields()
    return {
        "provider": model.provider, "model": model.physical_model, "event_type": "generation",
        "source_type": "report_coordinator", "trigger_type": "report_run",
        "operation_name": "repair-coordinator-decision" if repair else "coordinate-report",
        "status": "success",
        "input_tokens": ledger["input_tokens"], "output_tokens": ledger["output_tokens"],
        "total_tokens": ledger["input_tokens"] + ledger["output_tokens"],
        "cache_write_tokens": ledger["cache_write_tokens"], "cache_read_tokens": ledger["cache_read_tokens"],
        "metadata_json": {
            "component": COORDINATOR_COMPONENT, "report_stage": COORDINATOR_STAGE,
            "report_run_id": coordinator_input.report_run_id, "repair_retry": repair, "attempt": attempt,
            "output_valid": decision is not None,
            "action": decision.action if decision is not None else None,
            "requested_analyses_count": len(decision.analyses) if decision is not None else None,
            "normalized_usage": response.usage.metadata(), **model.metadata(),
            "actual_model": response.model, "actual_service_tier": response.actual_service_tier,
            "pricing_quote": response.pricing_quote, **response.thinking_decision,
        },
    }


# ----------------------------------------------------------------------------- the coordinator
class LLMReportCoordinator:
    """Provider-neutral coordinator over ``LLMRuntime``; the model is injected, never hardcoded."""

    def __init__(self, runtime, model: ModelConfig, *, cache_scope: CacheScope | None = None):
        self.runtime = runtime
        self.model = model
        self.cache = CachePolicy(scope=cache_scope) if cache_scope is not None else CachePolicy()
        self._system_prompt = build_system_prompt()

    def _request(self, messages: list[LLMMessage]) -> LLMRequest:
        return LLMRequest(
            model=self.model, instructions=[{"text": self._system_prompt, "cache_boundary": True}],
            messages=messages, cache=self.cache, operation="report-coordinator",
            response_format="json_object",
        )

    async def _generate(self, messages: list[LLMMessage]) -> tuple[LLMResponse, int]:
        started = time.monotonic()
        try:
            response = await self.runtime.generate(self._request(messages))
        except LLMError as exc:
            raise CoordinatorExecutionError(f"El coordinator falló ({exc.kind}).") from exc
        except Exception as exc:
            LOG.exception("Report coordinator provider call failed")
            raise CoordinatorExecutionError("El coordinator falló de forma inesperada.") from exc
        return response, round((time.monotonic() - started) * 1000)

    async def coordinate(
        self, coordinator_input: CoordinatorInput, *, on_attempt: CoordinatorAttemptHook | None = None,
    ) -> CoordinatorDecision:
        messages = [LLMMessage("user", coordinator_input.model_dump_json())]
        with _stage_observation(coordinator_input):
            errors: list[str] = []
            for attempt in range(1, MAX_REPAIR_RETRIES + 2):
                repair = attempt > 1
                response, latency_ms = await self._generate(messages)
                decision, errors = parse_coordinator_decision(response.text, coordinator_input)
                info = CoordinatorAttempt(
                    attempt=attempt, repair=repair, valid=decision is not None, model=self.model,
                    response=response, latency_ms=latency_ms, validation_errors=list(errors), decision=decision,
                    usage_event=_usage_event(self.model, response, coordinator_input=coordinator_input,
                                             attempt=attempt, repair=repair, decision=decision),
                )
                if on_attempt is not None:
                    hooked = on_attempt(info)
                    if inspect.isawaitable(hooked):
                        await hooked
                if decision is not None:
                    return decision
                # One repair retry: the invalid output plus only the contract errors.
                messages = [messages[0],
                            LLMMessage("assistant", (response.text or "")[:MAX_REPAIR_ECHO_CHARS]),
                            LLMMessage("user", _repair_message(errors))]
        raise CoordinatorInvalidDecisionError(
            "El coordinator no produjo una decisión válida tras el reintento de reparación.", errors=errors)
