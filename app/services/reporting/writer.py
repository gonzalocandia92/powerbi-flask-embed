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


class ReportWriter(Protocol):
    async def write(self, draft: ReportDraft, *,
                    on_attempt: WriterAttemptHook | None = None) -> FinalReport: ...


# ------------------------------------------------------------------------------ prompt
_RULES = """\
Sos el redactor de informes ejecutivos de KLARA. Recibís un ReportDraft: respuestas analíticas ya calculadas y verificadas. Tu única tarea es redactar y estructurar un informe para el cliente final.

REGLAS DE CONTENIDO
- Usá exclusivamente la información del ReportDraft. No agregues conocimiento externo.
- No inventes números, fechas, sucursales, productos ni causas. No modifiques, redondees ni recalcules cifras: copiá los valores tal como aparecen (formato incluido, por ejemplo "$20.425.450" o "-3,45%"). No calcules sumas, promedios ni porcentajes nuevos.
- Resumí sin perder información importante: no descartes cifras, variaciones ni anomalías relevantes de las respuestas.
- Priorizá en el resumen ejecutivo: cambios relevantes, anomalías, estabilidad y puntos de atención. No repitas simplemente todas las secciones. Usá entre 3 y 6 highlights cuando haya material suficiente.
- Usá attention_points sólo cuando exista evidencia explícita en el draft. No exageres ni suavices la magnitud de los resultados.
- Las tarjetas kpis deben ser pocas y relevantes; cada value debe ser una cifra literal del draft.
- period: completá label/start/end/comparison_label sólo si surgen de las respuestas. Si no están respaldados, dejá null. Nunca inventes fechas.
- Si una sección del draft tiene status "failed", no la inventes: mencioná de forma neutra, en notes, que ese análisis no estuvo disponible, sin detalles técnicos.

REGLAS SEMÁNTICAS (semantic_notes)
- semantic_notes son contexto interno curado para interpretar correctamente las métricas.
- Nunca reconcilies silenciosamente métricas con distinta semántica: no elijas un valor arbitrario, no promedies, no corrijas números ni afirmes que hay un error sólo porque dos totales difieren (por ejemplo, ventas generales y ventas por sucursal pueden diferir legítimamente si usan bases distintas).
- Cuando sea relevante para entender el informe, expresá ese contexto como una nota legible para el cliente en notes con kind "methodology". No copies las notas literalmente si son técnicas: redactalas de forma clara y sin jerga interna.

REGLAS DE FORMATO
- Escribí en español rioplatense neutro y profesional.
- No incluyas HTML, Markdown, estilos, colores ni clases. Sólo texto plano en los campos de texto.
- Las tablas deben ser estructuradas (columns + rows como objetos con las mismas keys de las columns). Nunca tablas en Markdown.
- trend es semántico: "up", "down", "stable" o "neutral". No devuelvas colores.
- Bloques permitidos: paragraph, bullet_list, table, callout (severity info|warning|critical).
- source_section_keys de cada KPI, sección, attention_point y nota deben contener sólo keys de secciones del ReportDraft que respalden ese contenido.
- No incluyas DAX, consultas, nombres de skills, routing, tokens, modelos ni información de depuración.
- Respondé únicamente con un objeto JSON válido que cumpla el schema. Sin texto adicional ni bloques de código.
"""


def build_system_prompt() -> str:
    schema = json.dumps(final_report_json_schema(), ensure_ascii=False, separators=(",", ":"))
    return f"{_RULES}\nSCHEMA JSON DE SALIDA (FinalReport, schema_version \"1.0\"):\n{schema}\n"


def compact_draft(draft: ReportDraft) -> dict[str, Any]:
    """Minimal view of a draft for the writer.

    Excludes DAX, tool traces, usage, routing, recovered-error details, model
    identity and any internal payload. Failed sections carry no technical detail.
    """
    sections = []
    for section in draft.sections:
        if section.had_error:
            sections.append({"key": section.key, "title": section.title,
                             "question": section.question, "status": "failed"})
            continue
        item: dict[str, Any] = {
            "key": section.key, "title": section.title, "question": section.question,
            "status": "ok", "answer": section.answer,
        }
        if section.semantic_notes:
            item["semantic_notes"] = list(section.semantic_notes)
        sections.append(item)
    return {"report_name": draft.name, "sections": sections}


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


def parse_final_report(text: str, valid_section_keys: set[str]) -> tuple[FinalReport | None, list[str]]:
    """Parse and validate writer output. Returns (report, []) or (None, schema errors).

    Error strings never echo model-provided values, only paths and messages.
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
    unknown = sorted(report.referenced_source_keys() - valid_section_keys)
    if unknown:
        return None, [_clip("source_section_keys referencia secciones inexistentes del ReportDraft: "
                            + ", ".join(unknown[:20]) + ". Keys válidas: " + ", ".join(sorted(valid_section_keys)))]
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
                 attempt: int, repair: bool, valid: bool) -> dict[str, Any]:
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
            "output_valid": valid,
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
        valid_keys = {section.key for section in draft.sections}
        messages = [LLMMessage("user", json.dumps(payload, ensure_ascii=False))]
        with _stage_observation(draft):
            errors: list[str] = []
            for attempt in range(1, MAX_REPAIR_RETRIES + 2):
                repair = attempt > 1
                response, latency_ms = await self._generate(messages)
                report, errors = parse_final_report(response.text, valid_keys)
                info = WriterAttempt(
                    attempt=attempt, repair=repair, valid=report is not None, model=self.model,
                    response=response, latency_ms=latency_ms, validation_errors=list(errors),
                    usage_event=_usage_event(self.model, response, draft=draft, attempt=attempt,
                                             repair=repair, valid=report is not None),
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
