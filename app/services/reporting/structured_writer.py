"""StructuredReportWriter: ``ComposedReportInput`` -> ``FinalReportV12`` (the writer of the structured flow).

Separate from the legacy ``LLMReportWriter`` (which still produces ``FinalReport`` 1.1 for ``/generate``): they
share the runtime, the error types, ``WriterAttempt`` and the repair pattern, but this one has no ``if version``
branches. It only knows the composed input: it never sees ``ReportDraft``, the free-text ``structure_prompt``,
a snapshot, DAX, schema, skills, credentials, DB models or artifacts, and it never re-interprets the
user's structure.

Freedom is limited to the INSIDE of each item: wording, which KPI cards, which blocks (paragraph / bullet_list /
table / callout), which attention points. WHAT items exist and in WHAT ORDER is fixed by the composed input and
enforced after the model answers (``composition_validation``), not trusted to the prompt.
"""
from __future__ import annotations

import inspect
import json
import logging
import time
from contextlib import nullcontext
from typing import Any, Awaitable, Callable, Protocol

from pydantic import ValidationError

from app.services.llm import LLMError, LLMMessage, LLMRequest, ModelConfig
from app.services.llm.contracts import CachePolicy, CacheScope, LLMResponse
from app.services.observability import start_observation

from .composed_report import ComposedReportInput
from .composition_validation import validate_report_against_composition
from .final_report_v12 import FinalReportV12, final_report_v12_json_schema
from .writer import (
    MAX_REPAIR_ECHO_CHARS, MAX_REPAIR_RETRIES, MAX_VALIDATION_ERRORS, WRITER_COMPONENT,
    WRITER_STAGE, ReportWriterExecutionError, ReportWriterInvalidOutputError, WriterAttempt, _clip, _strip_fences,
)

LOG = logging.getLogger(__name__)

STRUCTURED_WRITER_PROMPT_VERSION = "structured-writer-v1"

WriterAttemptHook = Callable[[WriterAttempt], Awaitable[None] | None]


class StructuredReportWriter(Protocol):
    async def write(self, composed: ComposedReportInput, *, report_run_id: str,
                    on_attempt: WriterAttemptHook | None = None) -> FinalReportV12: ...


_RULES = """\
Sos el redactor de informes ejecutivos de KLARA. Recibís un ComposedReportInput: una ESTRUCTURA ya decidida (items en orden) y la EVIDENCIA analítica autorizada para cada item. Tu única tarea es redactar el contenido de cada item a partir de esa evidencia y devolver un FinalReport 1.2.

LA ESTRUCTURA ES OBLIGATORIA
- "items" del input define exactamente qué componentes tiene el informe y en qué orden. Tu salida debe tener EXACTAMENTE un item por cada item del input, en el MISMO orden y con el MISMO type (items[i] de la salida corresponde a items[i] del input).
- No agregues, no elimines, no reordenes ni fusiones items. No crees secciones nuevas (por ejemplo "RENTABILIDAD"), no agregues resumen ejecutivo, KPIs, puntos de atención ni notas que la estructura no pidió, aunque te parezca editorialmente mejor.
- section: copiá key y title exactamente como vienen en el input. notes: copiá text exactamente, sin cambiarlo.
- Tu libertad editorial existe sólo DENTRO de cada item: redacción, qué KPIs mostrar (hasta max_kpis), qué bloques usar en una sección (paragraph, bullet_list, table, callout) y cuáles puntos de atención merecen aparecer.

EVIDENCIA Y PROVENANCE
- Cada item lista evidence_keys (evidencia asignada) y, en sections, supplementary_keys (análisis adicionales que profundizan la sección). Sólo podés usar la evidencia de ese item. Los datos están en "evidence", identificados por key.
- Toda afirmación analítica declara source_section_keys con keys copiadas literalmente de la evidencia AUTORIZADA para ese mismo item y con status "ok". Nunca uses keys editoriales inventadas ni evidencia de otro item. Un KPI sólo puede usar evidencia de su kpi_grid.
- Si un hallazgo se apoya en una sola evidencia, citá sólo esa key. Si combina varias, citá únicamente las que lo respaldan.
- supplementary_keys son evidencia complementaria: usala para enriquecer, aclarar o matizar la sección; no crees una sección aparte para cada análisis adicional. related_section_keys no implica causalidad.

CONTENIDO
- Usá exclusivamente la evidencia recibida. No inventes números, fechas, sucursales, productos, causas ni conocimiento externo. Copiá las cifras tal como aparecen: no redondees, no recalcules ni derives sumas, diferencias o porcentajes.
- No insinúes causalidad entre métricas que la evidencia no demuestre. Si dos métricas usan bases, filtros o períodos distintos, presentalas por separado. Respetá los semantic_notes como contexto interno y traducilos a lenguaje de negocio claro cuando corresponda (sin términos técnicos como "grano", "routing", "skill", "schema").
- Cada KPI.value debe ser una cifra literal de la evidencia. trend sólo indica dirección ("up","down","stable","neutral"); impact indica si es favorable ("positive","negative","neutral") y sólo cuando el contexto lo permita. No inventes objetivos ni umbrales.
- executive_summary: headline claro y 3 a 5 highlights concretos sólo con los hallazgos más relevantes de su evidencia; no repitas todas las secciones.
- attention_points: sólo hallazgos que realmente merezcan atención y estén respaldados por la evidencia; puede quedar vacío ("points": []) si nada lo amerita. No conviertas diferencias metodológicas normales en puntos de atención.
- methodology_notes: notas legibles para el cliente (kind "methodology", "data_quality" o "general") a partir de los semantic_notes y de los análisis no disponibles de su evidencia. Puede quedar vacío.
- No repitas la misma información en summary, paragraph, tablas y bullets. Preferí menos bloques útiles.
- period: completá sólo si surge explícitamente de la evidencia; si no, null. Nunca inventes fechas.

EVIDENCIA FALLIDA O FALTANTE
- Evidencia con status "failed" o "missing" NO tiene respuesta: nunca inventes su contenido ni cifras.
- Una section cuya evidencia no tiene ningún status "ok" debe tener status "unavailable", un summary breve y neutral que diga que el análisis no estuvo disponible (sin detalles técnicos), blocks vacío y source_section_keys vacío o con las keys no disponibles.
- Si una section tiene evidencia ok y otra fallida, status "ok", y podés mencionar neutralmente que parte del análisis no estuvo disponible. Las keys fallidas nunca respaldan afirmaciones analíticas.
- Si un kpi_grid, executive_summary o attention_points no tiene evidencia ok, dejalo sin contenido analítico (kpis [] / puntos []) o con un headline neutral sin cifras ni keys inventadas.

PRESENTACIÓN
- Escribí en español rioplatense neutro, profesional, preciso y conciso, en texto plano. No incluyas HTML, Markdown, CSS, colores, clases ni decisiones de layout.
- Tablas estructuradas (columns + rows con las mismas keys). No expongas DAX, consultas, skills, routing, tokens, modelos ni identificadores técnicos.
- composition_warnings es información sobre la composición: no la reproduzcas en el informe.
- Respondé únicamente con un objeto JSON válido que cumpla el schema (schema_version "1.2"). Sin texto adicional ni bloques de código.
"""


def build_system_prompt() -> str:
    schema = json.dumps(final_report_v12_json_schema(), ensure_ascii=False, separators=(",", ":"))
    return f"{_RULES}\nSCHEMA JSON DE SALIDA (FinalReport, schema_version \"1.2\"):\n{schema}\n"


def build_user_message(composed: ComposedReportInput) -> str:
    payload = composed.model_dump(mode="json", exclude_none=True)
    for index, item in enumerate(payload["items"]):
        item["index"] = index
    # The 1.2 writer predates structured evidence: its input stays byte-for-byte what it always was.
    for record in payload["evidence"]:
        for name in ("facts", "series", "tables"):
            record.pop(name, None)
    return json.dumps(payload, ensure_ascii=False)


def _stamp_literal_notes(data: dict, composed: ComposedReportInput) -> None:
    """Literal user notes are fixed by the composer: put the exact text where the model emitted a notes item.

    Only fills the text of a ``notes`` item that already sits at a ``notes`` position; it never creates,
    moves or retypes an item, so structural violations stay violations.
    """
    items = data.get("items")
    if not isinstance(items, list):
        return
    for index, want in enumerate(composed.items[:len(items)]):
        got = items[index]
        if want.type == "notes" and isinstance(got, dict) and got.get("type") == "notes":
            got["text"] = want.text


def parse_structured_report(text: str, composed: ComposedReportInput) -> tuple[FinalReportV12 | None, list[str]]:
    """Parse + schema-validate + enforce the composition. ``(report, [])`` or ``(None, errors)``."""
    try:
        data = json.loads(_strip_fences(text))
    except (ValueError, TypeError) as exc:
        return None, [_clip(f"JSON inválido: {exc}")]
    if not isinstance(data, dict):
        return None, ["La salida debe ser un objeto JSON."]
    _stamp_literal_notes(data, composed)
    try:
        report = FinalReportV12.model_validate(data)
    except ValidationError as exc:
        errors = []
        for item in exc.errors(include_input=False, include_url=False)[:MAX_VALIDATION_ERRORS]:
            path = ".".join(str(part) for part in item.get("loc", ())) or "(raíz)"
            errors.append(_clip(f"{path}: {item.get('msg')}"))
        return None, errors
    errors = [_clip(error) for error in validate_report_against_composition(report, composed)]
    return (None, errors) if errors else (report, [])


def _repair_message(errors: list[str], schema_version: str = "1.2") -> str:
    listed = "\n".join(f"- {error}" for error in errors[:MAX_VALIDATION_ERRORS])
    return (f"Tu respuesta anterior no cumple el contrato (schema FinalReport {schema_version} y/o la estructura obligatoria). "
            "Corregí únicamente estos errores y devolvé el JSON completo y válido, sin texto adicional:\n" + listed)


def _observation(report_run_id: str):
    try:
        return start_observation(
            name="report-structured-writer", as_type="span",
            metadata={"report_run_id": report_run_id, "report_stage": WRITER_STAGE, "component": WRITER_COMPONENT})
    except Exception:  # tracing is never allowed to break writing
        LOG.warning("Langfuse observation could not be started for structured writer", exc_info=True)
        return nullcontext(None)


def _usage_event(model: ModelConfig, response: LLMResponse, *, report_run_id: str, attempt: int, repair: bool,
                 valid: bool, latency_ms: int | None, report_schema_version: str = "1.2",
                 prompt_version: str = STRUCTURED_WRITER_PROMPT_VERSION) -> dict[str, Any]:
    ledger = response.usage.ledger_fields()
    return {
        "provider": model.provider, "model": model.physical_model, "event_type": "generation",
        "source_type": "report_writer", "trigger_type": "report_run",
        "operation_name": "repair-structured-report" if repair else "write-structured-report",
        "status": "success",
        "input_tokens": ledger["input_tokens"], "output_tokens": ledger["output_tokens"],
        "total_tokens": ledger["input_tokens"] + ledger["output_tokens"],
        "cache_write_tokens": ledger["cache_write_tokens"], "cache_read_tokens": ledger["cache_read_tokens"],
        "metadata_json": {
            "component": WRITER_COMPONENT, "report_stage": WRITER_STAGE,
            "report_run_id": report_run_id, "repair_retry": repair, "attempt": attempt,
            "latency_ms": latency_ms, "output_valid": valid,
            "report_schema_version": report_schema_version, "writer_prompt_version": prompt_version,
            "normalized_usage": response.usage.metadata(), **model.metadata(),
            "actual_model": response.model, "actual_service_tier": response.actual_service_tier,
            "pricing_quote": response.pricing_quote, **response.thinking_decision,
        },
    }


class LLMStructuredReportWriter:
    """Provider-neutral over ``LLMRuntime``; the model (``report_writer`` role) is injected, never hardcoded.

    The runtime/repair loop is shared by every structured writer version. A version (``structured_writer_v13``)
    subclasses and overrides ONLY the three hooks below (prompt, input rendering, parsing) and the two class
    attributes; the loop itself never branches on a schema version.
    """

    report_schema_version = "1.2"
    prompt_version = STRUCTURED_WRITER_PROMPT_VERSION

    def __init__(self, runtime, model: ModelConfig, *, cache_scope: CacheScope | None = None):
        self.runtime = runtime
        self.model = model
        self.cache = CachePolicy(scope=cache_scope) if cache_scope is not None else CachePolicy()
        self._system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        return build_system_prompt()

    def _build_user_message(self, composed: ComposedReportInput) -> str:
        return build_user_message(composed)

    def _parse(self, text: str, composed: ComposedReportInput):
        return parse_structured_report(text, composed)

    def _request(self, messages: list[LLMMessage]) -> LLMRequest:
        return LLMRequest(
            model=self.model, instructions=[{"text": self._system_prompt, "cache_boundary": True}],
            messages=messages, cache=self.cache, operation="report-structured-writer",
            response_format="json_object",
        )

    async def _generate(self, messages: list[LLMMessage]) -> tuple[LLMResponse, int]:
        started = time.monotonic()
        try:
            response = await self.runtime.generate(self._request(messages))
        except LLMError as exc:
            raise ReportWriterExecutionError(f"El modelo redactor falló ({exc.kind}).") from exc
        except Exception as exc:
            LOG.exception("Structured report writer provider call failed")
            raise ReportWriterExecutionError("El modelo redactor falló de forma inesperada.") from exc
        return response, round((time.monotonic() - started) * 1000)

    async def write(self, composed: ComposedReportInput, *, report_run_id: str,
                    on_attempt: WriterAttemptHook | None = None) -> FinalReportV12:
        if not composed.has_successful_evidence():
            raise ReportWriterExecutionError("El informe no tiene análisis exitosos para redactar.")
        messages = [LLMMessage("user", self._build_user_message(composed))]
        with _observation(report_run_id):
            errors: list[str] = []
            for attempt in range(1, MAX_REPAIR_RETRIES + 2):
                repair = attempt > 1
                response, latency_ms = await self._generate(messages)
                report, errors = self._parse(response.text, composed)
                info = WriterAttempt(
                    attempt=attempt, repair=repair, valid=report is not None, model=self.model,
                    response=response, latency_ms=latency_ms, validation_errors=list(errors),
                    usage_event=_usage_event(self.model, response, report_run_id=report_run_id, attempt=attempt,
                                             repair=repair, valid=report is not None, latency_ms=latency_ms,
                                             report_schema_version=self.report_schema_version,
                                             prompt_version=self.prompt_version))
                if on_attempt is not None:
                    hooked = on_attempt(info)
                    if inspect.isawaitable(hooked):
                        await hooked
                if report is not None:
                    return report
                # One repair retry: the invalid output plus only the contract errors.
                messages = [messages[0], LLMMessage("assistant", (response.text or "")[:MAX_REPAIR_ECHO_CHARS]),
                            LLMMessage("user", _repair_message(errors, self.report_schema_version))]
        raise ReportWriterInvalidOutputError(
            f"El redactor no produjo un FinalReport {self.report_schema_version} válido tras el reintento de reparación.",
            errors=errors)
