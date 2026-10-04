"""ReportWriter: turns a ``ReportDraft`` into a validated ``FinalReport``.

The writer is deliberately blind to analytics: it never calls Power BI, DAX,
schema retrieval or skills, and it never sees credentials. It only receives a
compact, sanitized view of the draft and talks to the provider-neutral
``LLMRuntime``. The concrete model comes from the ``report_writer`` model role.
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

from .contracts import ReportDraft
from .final_report import FinalReport, final_report_json_schema

LOG = logging.getLogger(__name__)

WRITER_COMPONENT = "report_writer"
WRITER_ROLE = "report_writer"
WRITER_STAGE = "writing"
MAX_VALIDATION_ERRORS = 20
MAX_ERROR_CHARS = 300
MAX_REPAIR_ECHO_CHARS = 60_000
MAX_REPAIR_RETRIES = 1


# --------------------------------------------------------------------------- errors
class ReportWriterError(Exception):
    """Base class; ``code`` is a stable, public identifier."""
    code = "report_writer_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class ReportWriterConfigurationError(ReportWriterError):
    code = "report_writer_not_configured"


class ReportWriterExecutionError(ReportWriterError):
    code = "report_writer_execution_failed"


class ReportWriterInvalidOutputError(ReportWriterError):
    code = "report_writer_invalid_output"

    def __init__(self, message: str, *, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = list(errors or [])


# ---------------------------------------------------------------------- observability
@dataclass
class WriterAttempt:
    """One provider call of the writer, reported to ``on_attempt`` (billing/diagnostics)."""
    attempt: int
    repair: bool
    valid: bool
    model: ModelConfig
    response: LLMResponse
    latency_ms: int
    usage_event: dict[str, Any]
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
            "cache_read_tokens": usage.cache_read_tokens, "cache_write_tokens": usage.cache_write_tokens,
            "latency_ms": self.latency_ms, "cost_usd": cost,
            "finish_reason": self.response.finish_reason,
            "validation_errors": list(self.validation_errors),
        }


WriterAttemptHook = Callable[[WriterAttempt], Awaitable[None] | None]


class ReportWriter(Protocol):  # legacy: ReportDraft -> FinalReport 1.1 (see structured_writer for 1.2)
    async def write(self, draft: ReportDraft, *,
                    on_attempt: WriterAttemptHook | None = None) -> FinalReport: ...


# ------------------------------------------------------------------------------ prompt
_RULES = """\
Sos el redactor de informes ejecutivos de KLARA. Recibís un ReportDraft: respuestas analíticas ya calculadas y verificadas. Tu única tarea es seleccionar, sintetizar y estructurar esa información para producir un informe claro para el cliente final.

REGLAS DE CONTENIDO
- Usá exclusivamente la información del ReportDraft. No agregues conocimiento externo.
- No inventes números, fechas, sucursales, productos, explicaciones ni causas.
- No modifiques, redondees ni recalcules cifras. Copiá los valores tal como aparecen. No derives sumas, diferencias, promedios, porcentajes ni equivalencias nuevas a partir de otros valores.
- Priorizá claridad y síntesis. El informe NO debe repetir la misma información en varios formatos sin aportar algo nuevo.
- El executive_summary debe contener sólo los hallazgos más relevantes del informe. Preferí 3 a 5 highlights concretos y no repitas simplemente el contenido de todas las secciones.
- Cada highlight es {text, source_section_keys}: declará qué secciones exitosas lo respaldan. headline_source_section_keys es opcional; usalo cuando el titular resuma hallazgos concretos.
- El headline debe sintetizar el hallazgo principal sin insinuar relaciones causales que el ReportDraft no demuestre.
- NO uses expresiones como "explica", "provoca", "se debe a", "impulsa", "genera" o equivalentes entre métricas distintas salvo que esa relación esté explícitamente respaldada por el ReportDraft.
- Si dos métricas usan bases, granularidades, filtros, períodos o hechos distintos, presentalas como análisis separados. No redactes una como explicación causal de la otra.
- Usá attention_points únicamente para hallazgos de negocio que realmente merezcan atención: cambios relevantes, anomalías, deterioros, riesgos o comportamientos destacados respaldados por los datos.
- NO conviertas diferencias metodológicas normales, bases de cálculo distintas o aclaraciones semánticas en attention_points. Esas aclaraciones pertenecen a notes o callouts metodológicos.
- La severidad de un attention_point representa relevancia dentro del informe, no una prioridad operativa absoluta. No declares urgencia, criticidad o prioridad empresarial si el ReportDraft no contiene criterios que lo justifiquen.
- Las tarjetas kpis deben ser pocas y realmente ejecutivas. Preferí métricas principales y accionables.
- Evitá mostrar simultáneamente como KPIs principales dos totales que usan bases de cálculo diferentes si eso puede hacer pensar que deberían coincidir. En ese caso, dejá la métrica secundaria dentro de su sección correspondiente.
- Cada KPI.value debe ser una cifra literal presente en el ReportDraft.
- period: completá label/start/end/comparison_label sólo si surgen explícitamente de las respuestas. Si no están respaldados, dejá null. Nunca inventes fechas.
- Si una sección del draft tiene status="failed", no inventes su análisis. Sólo podés mencionarla de forma neutra en notes para indicar que ese análisis no estuvo disponible, sin detalles técnicos.

REGLAS DE NO REDUNDANCIA
- Cada sección debe tener una función clara:
  - summary: conclusión principal de la sección;
  - paragraph: contexto adicional que NO repita el summary;
  - table: detalle estructurado;
  - bullet_list: sólo conclusiones o detalles que agreguen información;
  - callout: aclaración verdaderamente relevante.
- Si el summary ya contiene una cifra y un paragraph sólo repetiría esa misma información, omití el paragraph.
- Si una tabla ya muestra todos los valores, no repitas cada fila en bullets.
- No repitas una misma cifra en summary, paragraph y bullet_list salvo que sea imprescindible para comprender el texto.
- Preferí menos bloques con información útil antes que muchos bloques redundantes.

REGLAS SEMÁNTICAS (semantic_notes)
- semantic_notes son contexto interno curado para interpretar correctamente las métricas.
- Nunca reconcilies silenciosamente métricas con distinta semántica: no elijas un valor arbitrario, no promedies, no corrijas números ni afirmes que existe un error sólo porque dos totales difieren.
- Una diferencia entre métricas puede ser completamente válida si usan distintas bases, granularidades, filtros, períodos o hechos.
- Cuando dos métricas no sean directamente comparables, mantené explícitamente esa separación también en el executive_summary, KPIs y attention_points.
- Cuando sea relevante para comprender el informe, convertí semantic_notes en una nota legible para el cliente con kind="methodology".
- No copies semantic_notes literalmente si contienen lenguaje técnico. Traducilas a lenguaje empresarial claro.
- Evitá términos internos o de BI como "grano", "routing", "skill", "cabecera técnica", "schema" o similares cuando exista una forma más clara de expresarlo.

REGLAS DE PROVENANCE (source_section_keys)
- El ReportDraft incluye valid_source_section_keys. Esa lista es la autoridad para todas las referencias analíticas del FinalReport.
- Todo source_section_keys usado en kpis, highlights, headline_source_section_keys, sections y attention_points DEBE contener exclusivamente valores presentes literalmente en valid_source_section_keys.
- Copiá esas keys exactamente como aparecen. No las traduzcas, no las acortes, no las reformules y no inventes aliases.
- Ejemplos de keys válidas pueden ser "section_001", "section_002" o "extra_001".
- NUNCA uses como source_section_keys una key creada por vos para organizar el FinalReport, como "ventas", "sucursales", "ticket_promedio", "medios_pago" o cualquier otro nombre editorial.
- FinalReportSection.key y source_section_keys cumplen funciones distintas:
  - FinalReportSection.key es una key editorial que podés crear para estructurar el informe final.
  - source_section_keys identifica evidencia del ReportDraft y sólo puede usar keys existentes en valid_source_section_keys.
- Antes de devolver el JSON, verificá que cada valor usado en source_section_keys pertenezca literalmente a valid_source_section_keys.
- Si un hallazgo se apoya únicamente en una sección del ReportDraft, referenciá sólo esa key.
- Si un hallazgo combina evidencia de varias secciones, incluí únicamente las keys de las secciones que realmente respaldan esa afirmación.
- Si una conclusión usa un análisis adicional origin="coordinator", podés referenciar su key, por ejemplo "extra_001".
- Si una conclusión combina una sección original y una profundización del coordinator, incluí ambas keys cuando ambas sean necesarias para respaldar la afirmación, por ejemplo ["section_002", "extra_001"].
- No agregues una sección a source_section_keys sólo porque trate un tema parecido.
- Si existe failed_source_section_keys, esas keys NO pueden respaldar afirmaciones analíticas, KPIs, highlights, headline, sections ni attention_points.
- Las keys de failed_source_section_keys sólo pueden aparecer en notes cuando sea necesario indicar de forma neutra que ese análisis no estuvo disponible.

REGLAS PARA ANÁLISIS DEL COORDINATOR
- Las sections con origin="coordinator" son evidencia complementaria solicitada para profundizar hallazgos de las secciones indicadas por related_section_keys.
- Usá esas sections para enriquecer, aclarar o corregir la interpretación de las conclusiones relacionadas cuando aporten información relevante.
- NO crees automáticamente una sección independiente en el FinalReport por cada análisis adicional del coordinator.
- Preferí integrar el hallazgo adicional dentro de la sección de negocio que profundiza.
- related_section_keys describe qué análisis original motivó la profundización; no implica por sí mismo causalidad.
- Un análisis adicional puede detectar que una comparación original es incompleta, no comparable o requiere contexto adicional. En ese caso, priorizá la interpretación respaldada por la evidencia adicional sin borrar ni modificar arbitrariamente los datos originales.
- No ocultes una limitación relevante detectada por un análisis adicional. Presentala de forma clara y orientada al cliente, evitando detalles técnicos internos.

REGLAS DE PRESENTACIÓN
- Escribí en español rioplatense neutro, profesional, preciso y conciso.
- El informe debe poder ser leído rápidamente por una persona de negocio.
- Evitá títulos, subtítulos o textos que repitan innecesariamente el mismo período.
- No expongas identificadores técnicos internos como nombres de datasets, modelos semánticos, IDs o nombres técnicos del sistema cuando no sean necesarios para el cliente.
- No incluyas HTML, Markdown, estilos, colores ni clases. Sólo texto plano en los campos de texto.
- Las tablas deben ser estructuradas (columns + rows como objetos con las mismas keys de columns). Nunca tablas en Markdown.
- trend describe exclusivamente la dirección del cambio: "up", "down", "stable" o "neutral".
- impact describe si ese cambio puede interpretarse como favorable, desfavorable o neutro: "positive", "negative", "neutral".
- No infieras impact sólo a partir de trend. Por ejemplo, una suba puede ser positiva para ventas y negativa para gastos.
- Asigná impact únicamente cuando el contexto del ReportDraft permita interpretarlo razonablemente. Si no está claro, usá "neutral" o dejalo sin asignar.
- No inventes objetivos, thresholds ni criterios de negocio para decidir que algo es bueno, malo, urgente o crítico.
- Bloques permitidos: paragraph, bullet_list, table, callout (severity info|warning|critical).
- No incluyas DAX, consultas, nombres de skills, routing, tokens, modelos ni información de depuración.
- Respondé únicamente con un objeto JSON válido que cumpla el schema. Sin texto adicional ni bloques de código.

VALIDACIÓN FINAL ANTES DE RESPONDER
Antes de emitir el JSON final:
1. Verificá que todas las cifras y fechas provengan del ReportDraft.
2. Verificá que no hayas creado relaciones causales no respaldadas.
3. Verificá que no haya redundancia innecesaria entre summary, paragraphs, tablas y bullets.
4. Verificá que TODO valor de source_section_keys usado para afirmaciones analíticas pertenezca literalmente a valid_source_section_keys.
5. Verificá que ninguna key editorial creada para el FinalReport haya sido usada accidentalmente como source_section_keys.
6. Verificá que las sections origin="coordinator" hayan sido usadas como evidencia complementaria y no convertidas automáticamente en secciones independientes.
7. Devolvé únicamente el objeto JSON FinalReport válido.
"""


def build_system_prompt() -> str:
    schema = json.dumps(final_report_json_schema(), ensure_ascii=False, separators=(",", ":"))
    return f"{_RULES}\nSCHEMA JSON DE SALIDA (FinalReport, schema_version \"1.1\"):\n{schema}\n"


def source_section_keys(draft: ReportDraft) -> tuple[list[str], list[str]]:
    """Split a draft's section keys into valid and failed provenance, once.

    This is the single source of truth for both what ``compact_draft`` shows
    the writer as ``valid_source_section_keys``/``failed_source_section_keys``
    and what ``LLMReportWriter.write`` hands to ``parse_final_report`` as
    ``ok_section_keys``/``failed_section_keys``. Keeping one function means the
    allowlist offered to the model and the one enforced by the backend
    validator can never drift apart. Order follows ``draft.sections``.
    """
    valid: list[str] = []
    failed: list[str] = []
    for section in draft.sections:
        (failed if section.had_error else valid).append(section.key)
    return valid, failed


def compact_draft(draft: ReportDraft) -> dict[str, Any]:
    """Minimal view of a draft for the writer.

    Excludes DAX, tool traces, usage, routing, recovered-error details, model
    identity and any internal payload. Failed sections carry no technical detail.

    ``valid_source_section_keys``/``failed_source_section_keys`` make the
    provenance allowlist explicit data in the payload (rather than something
    the model must infer from ``status``), derived once via
    ``source_section_keys`` so it always matches what ``parse_final_report``
    actually accepts.
    """
    valid_keys, failed_keys = source_section_keys(draft)
    sections = []
    for section in draft.sections:
        if section.had_error:
            sections.append({"key": section.key, "title": section.title,
                             "question": section.question, "status": "failed"})
            continue
        item: dict[str, Any] = {
            "key": section.key, "title": section.title, "question": section.question,
            "status": "ok", "answer": section.answer, "origin": section.origin,
        }
        if section.purpose:
            item["purpose"] = section.purpose
        if section.related_section_keys:
            item["related_section_keys"] = list(section.related_section_keys)
        if section.semantic_notes:
            item["semantic_notes"] = list(section.semantic_notes)
        sections.append(item)
    return {
        "report_name": draft.name,
        "valid_source_section_keys": valid_keys,
        "failed_source_section_keys": failed_keys,
        "sections": sections,
    }


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


def parse_final_report(
    text: str, ok_section_keys: set[str], failed_section_keys: set[str] = frozenset(),
) -> tuple[FinalReport | None, list[str]]:
    """Parse and validate writer output. Returns (report, []) or (None, schema errors).

    ``ok_section_keys`` are the ReportDraft's successful sections: the only valid
    provenance for an analytical claim (kpis, highlights, sections, attention
    points). ``failed_section_keys`` additionally validate ``notes``, which may
    neutrally point at a failed section. Error strings never echo model-provided
    values, only paths and messages.
    """
    try:
        data = json.loads(_strip_fences(text))
    except (ValueError, TypeError) as exc:
        return None, [_clip(f"JSON inválido: {exc}")]
    if not isinstance(data, dict):
        return None, ["La salida debe ser un objeto JSON."]
    try:
        report = FinalReport.model_validate(data)
    except ValidationError as exc:
        errors = []
        for item in exc.errors(include_input=False, include_url=False)[:MAX_VALIDATION_ERRORS]:
            path = ".".join(str(part) for part in item.get("loc", ())) or "(raíz)"
            errors.append(_clip(f"{path}: {item.get('msg')}"))
        return None, errors
    unknown_analytical = sorted(report.analytical_source_keys() - ok_section_keys)
    if unknown_analytical:
        return None, [_clip(
            "source_section_keys de kpis/highlights/headline/sections/attention_points debe referenciar sólo "
            "secciones exitosas (status \"ok\") del ReportDraft. No son válidas: "
            + ", ".join(unknown_analytical[:20]) + ". Secciones exitosas disponibles: "
            + ", ".join(sorted(ok_section_keys)))]
    all_keys = ok_section_keys | failed_section_keys
    unknown_notes = sorted(report.note_source_keys() - all_keys)
    if unknown_notes:
        return None, [_clip("source_section_keys de notes referencia secciones inexistentes del ReportDraft: "
                            + ", ".join(unknown_notes[:20]) + ". Keys válidas: " + ", ".join(sorted(all_keys)))]
    return report, []


def _repair_message(errors: list[str]) -> str:
    listed = "\n".join(f"- {error}" for error in errors[:MAX_VALIDATION_ERRORS])
    return ("Tu respuesta anterior no cumple el schema de FinalReport. Corregí únicamente estos errores "
            "y devolvé el JSON completo y válido, sin texto adicional:\n" + listed)


# -------------------------------------------------------------------------- observability
def _stage_observation(draft: ReportDraft):
    try:
        return start_observation(
            name="report-writer", as_type="span",
            metadata={"report_run_id": draft.report_run_id, "report_stage": WRITER_STAGE,
                      "component": WRITER_COMPONENT})
    except Exception:  # tracing is never allowed to break writing
        LOG.warning("Langfuse observation could not be started for report writer", exc_info=True)
        return nullcontext(None)


def _usage_event(model: ModelConfig, response: LLMResponse, *, draft: ReportDraft,
                 attempt: int, repair: bool, valid: bool, latency_ms: int | None = None) -> dict[str, Any]:
    ledger = response.usage.ledger_fields()
    return {
        "provider": model.provider, "model": model.physical_model, "event_type": "generation",
        "source_type": "report_writer", "trigger_type": "report_run",
        "operation_name": "repair-final-report" if repair else "write-final-report",
        "status": "success",
        "input_tokens": ledger["input_tokens"], "output_tokens": ledger["output_tokens"],
        "total_tokens": ledger["input_tokens"] + ledger["output_tokens"],
        "cache_write_tokens": ledger["cache_write_tokens"], "cache_read_tokens": ledger["cache_read_tokens"],
        "metadata_json": {
            "component": WRITER_COMPONENT, "report_stage": WRITER_STAGE,
            "report_run_id": draft.report_run_id, "repair_retry": repair, "attempt": attempt,
            "latency_ms": latency_ms, "output_valid": valid,
            "normalized_usage": response.usage.metadata(), **model.metadata(),
            "actual_model": response.model, "actual_service_tier": response.actual_service_tier,
            "pricing_quote": response.pricing_quote, **response.thinking_decision,
        },
    }


# ---------------------------------------------------------------------------- the writer
class LLMReportWriter:
    """Provider-neutral writer over ``LLMRuntime``; the model is injected, never hardcoded."""

    def __init__(self, runtime, model: ModelConfig, *, cache_scope: CacheScope | None = None):
        self.runtime = runtime
        self.model = model
        self.cache = CachePolicy(scope=cache_scope) if cache_scope is not None else CachePolicy()
        self._system_prompt = build_system_prompt()

    def _request(self, messages: list[LLMMessage]) -> LLMRequest:
        return LLMRequest(
            model=self.model, instructions=[{"text": self._system_prompt, "cache_boundary": True}],
            messages=messages, cache=self.cache, operation="report-writer",
            response_format="json_object",
        )

    async def _generate(self, messages: list[LLMMessage]) -> tuple[LLMResponse, int]:
        started = time.monotonic()
        try:
            response = await self.runtime.generate(self._request(messages))
        except LLMError as exc:
            raise ReportWriterExecutionError(f"El modelo redactor falló ({exc.kind}).") from exc
        except Exception as exc:
            LOG.exception("Report writer provider call failed")
            raise ReportWriterExecutionError("El modelo redactor falló de forma inesperada.") from exc
        return response, round((time.monotonic() - started) * 1000)

    async def write(self, draft: ReportDraft, *, on_attempt: WriterAttemptHook | None = None) -> FinalReport:
        payload = compact_draft(draft)
        if not any(item["status"] == "ok" for item in payload["sections"]):
            raise ReportWriterExecutionError("El ReportDraft no tiene secciones analíticas exitosas para redactar.")
        # Analytical claims (kpis, highlights, sections, attention points) may only
        # be sourced from successful sections; notes may also point at a failed one.
        # Same helper as compact_draft(), so the allowlist shown to the model and
        # the one enforced here can never diverge.
        valid_keys, failed_keys_list = source_section_keys(draft)
        ok_keys = set(valid_keys)
        failed_keys = set(failed_keys_list)
        messages = [LLMMessage("user", json.dumps(payload, ensure_ascii=False))]
        with _stage_observation(draft):
            errors: list[str] = []
            for attempt in range(1, MAX_REPAIR_RETRIES + 2):
                repair = attempt > 1
                response, latency_ms = await self._generate(messages)
                report, errors = parse_final_report(response.text, ok_keys, failed_keys)
                info = WriterAttempt(
                    attempt=attempt, repair=repair, valid=report is not None, model=self.model,
                    response=response, latency_ms=latency_ms, validation_errors=list(errors),
                    usage_event=_usage_event(self.model, response, draft=draft, attempt=attempt,
                                             repair=repair, latency_ms=latency_ms, valid=report is not None),
                )
                if on_attempt is not None:
                    hooked = on_attempt(info)
                    if inspect.isawaitable(hooked):
                        await hooked
                if report is not None:
                    return report
                # One repair retry: the invalid output plus only the schema errors.
                messages = [messages[0],
                            LLMMessage("assistant", (response.text or "")[:MAX_REPAIR_ECHO_CHARS]),
                            LLMMessage("user", _repair_message(errors))]
        raise ReportWriterInvalidOutputError(
            "El redactor no produjo un FinalReport válido tras el reintento de reparación.", errors=errors)
