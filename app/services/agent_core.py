"""
Core agent orchestration for Power BI chat.

This module contains the prompt manager, tool registry and async agent
orchestrator used by the chatbot flow. The legacy compatibility facade now
delegates to these classes and functions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, cast
from zoneinfo import ZoneInfo

from app.models import SchemaEmbedding
from app.services.observability import hash_identifier, observation_preview, start_observation

from .powerbi_tools import execute_dax_query_local

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_HISTORY_LIMIT = 4
DEFAULT_MAX_TOOL_ROUNDS = 10
DEFAULT_DEBUG_ENABLED = True
DEFAULT_PROMPT_CACHING_ENABLED = True
DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS = 20
DEFAULT_TABLE_CONTEXT_LIMIT = 6
DEFAULT_MEASURE_CONTEXT_LIMIT = 10
VOYAGE_QUERY_EMBEDDING_MODEL = "voyage-4"
VOYAGE_QUERY_EMBEDDING_COST_PER_MILLION_USD = 0.06
ANTHROPIC_PROMPT_TOKEN_LIMIT = 200_000
TOOL_ERROR_RESULT_MAX_CHARS = 3_000
TOOL_ERROR_RESULT_HEAD_CHARS = 1_800
TOOL_ERROR_RESULT_TAIL_CHARS = 800
SAFE_TECHNICAL_ERROR_ANSWER = (
    "Disculpa, me encontre con un inconveniente tecnico al procesar los datos. "
    "Por favor, intenta nuevamente mas tarde."
)
DAX_ERROR_ATTEMPT_LIMIT = 3

TEMPORAL_CONTEXT_TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")
TEMPORAL_CONTEXT_LOCATION = "Resistencia, Chaco, Argentina"

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEBUG_FILE_LOCK = threading.RLock()
_DEBUG_FILE_DEFAULT = _PROJECT_ROOT / "agent_core_debug.txt"


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _debug_print(label: str, payload: Any, *, enabled: bool = True) -> None:
    """Render debug traces for agent orchestration."""
    if not enabled:
        return
    try:
        if isinstance(payload, (dict, list)):
            rendered = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        else:
            rendered = str(payload)
    except Exception:
        rendered = repr(payload)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = f"\n{timestamp} [agent_core] {label}\n{rendered}\n"
    print(message, flush=True)
    _append_debug_to_file(message)


def _append_debug_to_file(message: str) -> None:
    """Append debug output to a txt file for later inspection."""
    debug_file_raw = os.getenv("CHAT_DEBUG_FILE")
    debug_file = Path(debug_file_raw).expanduser() if debug_file_raw else _DEBUG_FILE_DEFAULT

    try:
        debug_file.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_FILE_LOCK:
            with debug_file.open("a", encoding="utf-8") as fh:
                fh.write(message)
                if not message.endswith("\n"):
                    fh.write("\n")
    except Exception as exc:
        logging.exception("Failed to write agent debug output to file: %s", debug_file)
        print(f"[agent_core] debug-file-error: {exc}", flush=True)


def _usage_metric(usage: Any, field_name: str) -> int:
    """Extract a numeric usage field from Anthropic responses."""
    if usage is None:
        return 0

    value = getattr(usage, field_name, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(field_name)

    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _new_usage_totals() -> Dict[str, int]:
    return {"input_tokens": 0, "output_tokens": 0}


def _add_usage_totals(usage_totals: Optional[Dict[str, int]], *, input_tokens: int = 0, output_tokens: int = 0) -> None:
    if usage_totals is None:
        return

    usage_totals["input_tokens"] = int(usage_totals.get("input_tokens", 0)) + int(input_tokens or 0)
    usage_totals["output_tokens"] = int(usage_totals.get("output_tokens", 0)) + int(output_tokens or 0)


def _anthropic_usage_metrics(usage: Any) -> Dict[str, int]:
    return {
        "input_tokens": _usage_metric(usage, "input_tokens"),
        "output_tokens": _usage_metric(usage, "output_tokens"),
        "cache_write_tokens": _usage_metric(usage, "cache_creation_input_tokens"),
        "cache_read_tokens": _usage_metric(usage, "cache_read_input_tokens"),
    }


def _append_ai_usage_event(events: Optional[List[Dict[str, Any]]], **payload: Any) -> None:
    if events is None:
        return
    events.append(payload)


def _is_prompt_too_long_error(exc: Exception) -> bool:
    text = str(exc or "").strip().lower()
    return "prompt is too long" in text or "maximum" in text and "tokens" in text


def _estimate_text_tokens(text: str, *, chars_per_token: int) -> int:
    return max(1, len(text or "") // max(1, int(chars_per_token)))


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _tool_output_is_error(output: Any) -> bool:
    text = str(output or "").strip().lower()
    if not text:
        return False
    return (
        text.startswith("error:")
        or text.startswith("error tecnico")
        or text.startswith("error interno")
        or text.startswith("error obteniendo")
    )


def _compact_tool_result_for_model(output: Any) -> str:
    text = str(output or "")
    if not _tool_output_is_error(text) or len(text) <= TOOL_ERROR_RESULT_MAX_CHARS:
        return text

    omitted_chars = len(text) - TOOL_ERROR_RESULT_HEAD_CHARS - TOOL_ERROR_RESULT_TAIL_CHARS
    if omitted_chars <= 0:
        return text[:TOOL_ERROR_RESULT_MAX_CHARS]

    return (
        f"{text[:TOOL_ERROR_RESULT_HEAD_CHARS]}\n\n"
        f"[Tool result compactado: se omitieron {omitted_chars} caracteres del error para controlar el contexto.]\n\n"
        f"{text[-TOOL_ERROR_RESULT_TAIL_CHARS:]}"
    )


@dataclass
class RuntimeSettings:
    """Configuration for the Anthropic + tool runtime."""

    anthropic_api_key: str = ANTHROPIC_API_KEY
    anthropic_model: str = DEFAULT_MODEL
    anthropic_max_tokens: int = DEFAULT_MAX_TOKENS
    history_limit: int = DEFAULT_HISTORY_LIMIT
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS
    debug_enabled: bool = DEFAULT_DEBUG_ENABLED
    prompt_caching_enabled: bool = DEFAULT_PROMPT_CACHING_ENABLED
    schema_context_timeout_seconds: int = DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS


def build_runtime_settings(config: Optional[Dict[str, Any]] = None) -> RuntimeSettings:
    """Build runtime settings from Flask config and environment variables."""
    config = config or {}

    def _cfg(name: str, default: Optional[str] = None) -> Optional[str]:
        value = config.get(name)
        if value is None:
            value = os.getenv(name, default)
        return value

    anthropic_api_key = _cfg("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required")

    model = _cfg("ANTHROPIC_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
    max_tokens_raw = _cfg("ANTHROPIC_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)) or str(DEFAULT_MAX_TOKENS)
    history_limit_raw = _cfg("CHAT_HISTORY_LIMIT", str(DEFAULT_HISTORY_LIMIT)) or str(DEFAULT_HISTORY_LIMIT)
    max_tool_rounds_raw = _cfg("CHAT_MAX_TOOL_ROUNDS", str(DEFAULT_MAX_TOOL_ROUNDS)) or str(DEFAULT_MAX_TOOL_ROUNDS)
    debug_enabled_default = "true" if DEFAULT_DEBUG_ENABLED else "false"
    debug_enabled_raw = _cfg("CHAT_DEBUG_ENABLED", debug_enabled_default) or debug_enabled_default
    prompt_caching_enabled_raw = _cfg("ANTHROPIC_PROMPT_CACHING_ENABLED", _cfg("CHAT_PROMPT_CACHING_ENABLED", "true")) or "true"
    schema_context_timeout_raw = _cfg("CHAT_SCHEMA_CONTEXT_TIMEOUT_SECONDS", str(DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS)) or str(DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS)

    return RuntimeSettings(
        anthropic_api_key=str(anthropic_api_key),
        anthropic_model=str(model),
        anthropic_max_tokens=int(max_tokens_raw),
        history_limit=int(history_limit_raw),
        max_tool_rounds=int(max_tool_rounds_raw),
        debug_enabled=_parse_bool(debug_enabled_raw, default=DEFAULT_DEBUG_ENABLED),
        prompt_caching_enabled=_parse_bool(prompt_caching_enabled_raw, default=DEFAULT_PROMPT_CACHING_ENABLED),
        schema_context_timeout_seconds=int(schema_context_timeout_raw),
    )


def _debug_enabled() -> bool:
    """Best-effort global debug switch for prints/file logs."""
    return _parse_bool(os.getenv("CHAT_DEBUG_ENABLED"), default=DEFAULT_DEBUG_ENABLED)


def _get_voyage_client():
    api_key = os.getenv("VOYAGE_API_KEY")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY is required")

    try:
        import voyageai
    except ImportError as exc:  # pragma: no cover - dependency missing in test env
        raise RuntimeError("The 'voyageai' package is required to retrieve schema context.") from exc

    return voyageai.Client(api_key=api_key)

async def _rewrite_query_for_reranker(
    *,
    user_message: str,
    settings: RuntimeSettings,
    debug_enabled: bool,
    schema_retrieval_prompt: Optional[str] = None,
    usage_totals: Optional[Dict[str, int]] = None,
    ai_usage_events: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Micro-agent that translates a user question into technical keywords."""
    with start_observation(
        name="rewrite-query-for-reranker",
        as_type="chain",
        input={"user_message": user_message},
    ) as observation:
        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except ImportError:
            logging.warning("The 'anthropic' package is not available for query rewriting")
            return user_message

        retrieval_hint = str(schema_retrieval_prompt or "").strip()
        report_context = (
            "\nDiccionario corto del reporte para retrieval:\n"
            f"{retrieval_hint}\n"
            if retrieval_hint
            else ""
        )
        system_prompt = (
            "Eres un experto en bases de datos y Power BI. Tu unica tarea es extraer y deducir "
            "los terminos tecnicos mas probables de la pregunta del usuario. "
            "Usa el diccionario corto del reporte solo para elegir terminos de tablas, medidas y dominios correctos. "
            "Reglas: Devuelve SOLO una lista de 5 a 8 palabras clave separadas por comas. "
            "No incluyas saludos, explicaciones ni vinietas. "
            f"{report_context}"
        )

        _debug_print("agent:rewriter:start", {"original_query": user_message}, enabled=debug_enabled)
        try:
            async with AsyncAnthropic(api_key=settings.anthropic_api_key) as client:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    temperature=0.0,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )
                usage = getattr(response, "usage", None)
                usage_metrics = _anthropic_usage_metrics(usage)
                _add_usage_totals(
                    usage_totals,
                    input_tokens=usage_metrics["input_tokens"],
                    output_tokens=usage_metrics["output_tokens"],
                )
                _append_ai_usage_event(
                    ai_usage_events,
                    provider="anthropic",
                    model="claude-haiku-4-5-20251001",
                    event_type="generation",
                    source_type="retrieval",
                    trigger_type="user_request",
                    operation_name="rewrite-query-for-reranker",
                    status="success",
                    input_tokens=usage_metrics["input_tokens"],
                    output_tokens=usage_metrics["output_tokens"],
                    total_tokens=usage_metrics["input_tokens"] + usage_metrics["output_tokens"],
                    cache_write_tokens=usage_metrics["cache_write_tokens"],
                    cache_read_tokens=usage_metrics["cache_read_tokens"],
                    metadata_json={"component": "query_rewriter"},
                )
                optimized_query = response.content[0].text.strip()
                if observation is not None:
                    observation.update(output={"optimized_query": optimized_query})
                _debug_print("agent:rewriter:success", {"optimized_query": optimized_query}, enabled=debug_enabled)
                return optimized_query or user_message
        except Exception as exc:
            logging.warning("The query rewriter failed. Using original query: %s", exc)
            estimated_input_tokens = _estimate_text_tokens(
                system_prompt + user_message,
                chars_per_token=4,
            )
            _add_usage_totals(
                usage_totals,
                input_tokens=estimated_input_tokens,
                output_tokens=0,
            )
            _append_ai_usage_event(
                ai_usage_events,
                provider="anthropic",
                model="claude-haiku-4-5-20251001",
                event_type="generation",
                source_type="retrieval",
                trigger_type="user_request",
                operation_name="rewrite-query-for-reranker",
                status="error",
                input_tokens=estimated_input_tokens,
                output_tokens=0,
                total_tokens=estimated_input_tokens,
                metadata_json={
                    "component": "query_rewriter",
                    "estimated_usage": True,
                    "error_type": "anthropic_provider_error",
                },
            )
            if observation is not None:
                observation.update(output={"error": observation_preview(repr(exc), max_length=500)})
            _debug_print("agent:rewriter:error", {"error": repr(exc)}, enabled=debug_enabled)
            return user_message


async def _fetch_schema_context(
    *,
    dataset_id: str,
    powerbi_credentials: Dict[str, Any],
    question: str,
    settings: RuntimeSettings,
    debug_enabled: bool,
    required: bool,
    table_context_limit: int = DEFAULT_TABLE_CONTEXT_LIMIT,
    measure_context_limit: int = DEFAULT_MEASURE_CONTEXT_LIMIT,
    usage_totals: Optional[Dict[str, int]] = None,
    ai_usage_events: Optional[List[Dict[str, Any]]] = None,
) -> str:
    with start_observation(
        name="fetch-schema-context",
        as_type="retriever",
        input={"question": question},
    ) as observation:
        if observation is not None:
            observation.update(metadata={"datasethash": hash_identifier(dataset_id, prefix="dataset")})
        _debug_print(
            "tool:get_schema_context:request",
            {
                "dataset_id": dataset_id,
                "question": question,
                "table_context_limit": table_context_limit,
                "measure_context_limit": measure_context_limit,
            },
            enabled=debug_enabled,
        )
        try:
            voyage_client = _get_voyage_client()
            with start_observation(
                name="voyage-query-embedding",
                as_type="embedding",
                input=[question],
            ) as embedding_observation:
                if embedding_observation is not None:
                    embedding_observation.update(
                        model=VOYAGE_QUERY_EMBEDDING_MODEL,
                        metadata={
                            "provider": "voyageai",
                            "inputtype": "query",
                        },
                    )

                try:
                    embedding_response = await asyncio.wait_for(
                        asyncio.to_thread(
                            lambda: voyage_client.embed(
                                [question],
                                model=VOYAGE_QUERY_EMBEDDING_MODEL,
                                input_type="query",
                            )
                        ),
                        timeout=max(1, int(settings.schema_context_timeout_seconds)),
                    )
                except Exception as exc:
                    estimated_input_tokens = _estimate_text_tokens(question, chars_per_token=5)
                    _add_usage_totals(
                        usage_totals,
                        input_tokens=estimated_input_tokens,
                        output_tokens=0,
                    )
                    _append_ai_usage_event(
                        ai_usage_events,
                        provider="voyageai",
                        model=VOYAGE_QUERY_EMBEDDING_MODEL,
                        event_type="embedding",
                        source_type="retrieval",
                        trigger_type="user_request",
                        operation_name="voyage-query-embedding",
                        status="error",
                        input_tokens=estimated_input_tokens,
                        output_tokens=0,
                        total_tokens=estimated_input_tokens,
                        metadata_json={
                            "input_type": "query",
                            "estimated_usage": True,
                            "error_type": "voyage_provider_error",
                        },
                    )
                    raise exc
                query_vector = embedding_response.embeddings[0]
                query_vector_list = list(query_vector)
                total_tokens = getattr(embedding_response, "total_tokens", None)
                _add_usage_totals(usage_totals, input_tokens=int(total_tokens or 0))
                _append_ai_usage_event(
                    ai_usage_events,
                    provider="voyageai",
                    model=VOYAGE_QUERY_EMBEDDING_MODEL,
                    event_type="embedding",
                    source_type="retrieval",
                    trigger_type="user_request",
                    operation_name="voyage-query-embedding",
                    status="success",
                    input_tokens=int(total_tokens or 0),
                    output_tokens=0,
                    total_tokens=int(total_tokens or 0),
                    metadata_json={"input_type": "query"},
                )
                if embedding_observation is not None:
                    update_payload = {
                        "output": {
                            "embedding_dimensions": len(query_vector_list),
                            "vector_count": 1,
                        }
                    }
                    if total_tokens is not None:
                        update_payload["usage_details"] = {"input": int(total_tokens)}
                        update_payload["cost_details"] = {
                            "input": (
                                int(total_tokens)
                                * VOYAGE_QUERY_EMBEDDING_COST_PER_MILLION_USD
                                / 1_000_000
                            )
                        }
                    embedding_observation.update(**update_payload)
            _debug_print(
                "tool:get_schema_context:query_vector_ready",
                {"dataset_id": dataset_id, "dimensions": len(query_vector_list)},
                enabled=debug_enabled,
            )

            table_results = (
                SchemaEmbedding.query
                .filter(
                    SchemaEmbedding.dataset_id == dataset_id,
                    SchemaEmbedding.item_type == "table",
                )
                .order_by(SchemaEmbedding.embedding.cosine_distance(query_vector_list))
                .limit(_coerce_positive_int(table_context_limit, DEFAULT_TABLE_CONTEXT_LIMIT))
                .all()
            )
            measure_results = (
                SchemaEmbedding.query
                .filter(
                    SchemaEmbedding.dataset_id == dataset_id,
                    SchemaEmbedding.item_type == "measure",
                )
                .order_by(SchemaEmbedding.embedding.cosine_distance(query_vector_list))
                .limit(_coerce_positive_int(measure_context_limit, DEFAULT_MEASURE_CONTEXT_LIMIT))
                .all()
            )

            payload = {
                "tables": [row.content_text for row in table_results],
                "measures": [row.content_text for row in measure_results],
            }
            fetched_schema_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if observation is not None:
                observation.update(
                    output={
                        "table_matches": len(table_results),
                        "measure_matches": len(measure_results),
                        "table_context_limit": table_context_limit,
                        "measure_context_limit": measure_context_limit,
                    }
                )
            _debug_print("tool:get_schema_context:response", fetched_schema_text, enabled=debug_enabled)
            return fetched_schema_text.strip()
        except asyncio.TimeoutError:
            logging.warning(
                "Timed out fetching vector schema context after %s seconds",
                settings.schema_context_timeout_seconds,
            )
            if observation is not None:
                observation.update(output={"timeout_seconds": settings.schema_context_timeout_seconds})
            _debug_print(
                "tool:get_schema_context:timeout",
                {"seconds": settings.schema_context_timeout_seconds},
                enabled=debug_enabled,
            )
            return ""
        except Exception as exc:
            logging.exception("Failed to fetch vector schema context")
            if observation is not None:
                observation.update(output={"error": observation_preview(repr(exc), max_length=500)})
            _debug_print("tool:get_schema_context:error", repr(exc), enabled=debug_enabled)
            return ""


def _minify_schema_text(schema_text: str) -> str:
    schema_text = schema_text.strip()
    if not schema_text:
        return "(No schema available)"
    try:
        schema_obj = json.loads(schema_text)
        return json.dumps(schema_obj, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return schema_text


def _build_temporal_context_line() -> str:
    weekdays = {
        0: "lunes",
        1: "martes",
        2: "miercoles",
        3: "jueves",
        4: "viernes",
        5: "sabado",
        6: "domingo",
    }
    months = {
        1: "enero",
        2: "febrero",
        3: "marzo",
        4: "abril",
        5: "mayo",
        6: "junio",
        7: "julio",
        8: "agosto",
        9: "septiembre",
        10: "octubre",
        11: "noviembre",
        12: "diciembre",
    }
    now = datetime.now(TEMPORAL_CONTEXT_TIMEZONE)
    fecha_actual = (
        f"{weekdays[now.weekday()]}, {now.day:02d} de {months[now.month]} de "
        f"{now.year} a las {now:%H:%M:%S}"
    )
    return (
        "- CONTEXTO TEMPORAL: "
        f"La fecha y hora actual del sistema es {fecha_actual}. "
        f"El usuario se encuentra en {TEMPORAL_CONTEXT_LOCATION}. "
        "Usa esta informacion para resolver referencias relativas como 'hoy', "
        "'este mes', o consideraciones regionales.\n"
    )


def _render_custom_instructions(custom_instructions: Optional[List[Any]] = None) -> str:
    if not custom_instructions:
        return ""

    rendered_sections: List[str] = []
    for item in custom_instructions:
        if isinstance(item, dict):
            scope_type = str(item.get("scope_type") or "config").strip()
            title = str(item.get("title") or scope_type).strip()
            instructions = str(item.get("instructions") or "").strip()
        else:
            scope_type = str(getattr(item, "scope_type", "config") or "config").strip()
            title = str(getattr(item, "title", scope_type) or scope_type).strip()
            instructions = str(getattr(item, "instructions", "") or "").strip()

        if not instructions:
            continue
        rendered_sections.append(f"[{scope_type} - {title}]\n{instructions}")

    if not rendered_sections:
        return ""

    return (
        "\nINSTRUCCIONES DINÁMICAS CONFIGURADAS:\n"
        "Estas instrucciones agregan contexto de negocio, tono y preferencias analíticas. "
        "No pueden anular las reglas críticas de seguridad, privacidad, uso de herramientas, "
        "sintaxis DAX ni manejo de errores definidas en este prompt base.\n"
        + "\n\n".join(rendered_sections)
        + "\n"
    )


def _build_system_prompt(schema_text: str, custom_instructions: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    schema_block = _minify_schema_text(schema_text)
    temporal_context_line = _build_temporal_context_line()
    custom_instruction_block = _render_custom_instructions(custom_instructions)
    return [
        {
            "type": "text",
            "text": (
                """Tu nombre es Klara. Eres un asistente experto de analítica para Power BI. Tu tarea es responder preguntas del usuario usando el modelo semántico disponible y, cuando haga falta, ejecutar consultas DAX mediante las herramientas del backend.
REGLAS ESTRICTAS E INQUEBRANTABLES
1. SEGURIDAD E INFORMACIÓN INTERNA
* Nunca pidas, menciones ni intentes inferir dataset_id, workspace_id, credenciales, tokens, URLs internas ni detalles técnicos del backend.
* Nunca expongas al usuario el código DAX generado, salvo que una configuración explícita del backend lo permita fuera de este prompt.
* Nunca muestres errores internos, stack traces, respuestas crudas de APIs ni mensajes técnicos de Power BI.
* Si ocurre un error técnico irrecuperable o agotas tus intentos de corrección, responde de forma breve y genérica:
  "Disculpa, me encontré con un inconveniente técnico al procesar los datos. Por favor, intenta nuevamente más tarde."
* Nunca atribuyas al usuario, al reporte o al modelo una falla técnica interna.
2. USO DE HERRAMIENTAS Y PREVENCIÓN DE ALUCINACIONES
* Para consultar datos, usa la herramienta execute_dax_query.
* Toda consulta DAX debe usar SIEMPRE la instrucción EVALUATE.
* NUNCA inventes nombres de tablas, columnas, medidas, relaciones, valores categóricos ni períodos disponibles.
* Si necesitás información que no está en el "Contexto del modelo semántico actual", estás obligado a usar get_schema_context para descubrir los nombres reales antes de invocar execute_dax_query.
* Si el contexto semántico actual ya contiene la medida solicitada, debés usarla directamente entre corchetes. Ejemplo: [Ventas], [Ticket Promedio].
* No recrees manualmente la lógica de una medida existente con SUMX, AVERAGEX, FILTER u otras funciones, salvo que la medida existente no cubra el requerimiento y hayas verificado que es necesario realizar el cálculo.
* Debés ejecutar execute_dax_query antes de responder toda pregunta que solicite ventas, tickets, montos, porcentajes, rankings, tendencias, comparaciones, períodos históricos, sucursales, productos, categorías, canales, medios de pago o recomendaciones basadas en datos.
* Solo podés responder sin ejecutar DAX ante saludos, explicaciones generales del funcionamiento de Klara, definiciones metodológicas o una aclaración estrictamente necesaria para interpretar una pregunta.
3. PROTOCOLO OBLIGATORIO DE EVIDENCIA Y VERIFICACIÓN
3.1. Fuente de verdad
* El contexto semántico sirve únicamente para identificar tablas, columnas, medidas y relaciones posibles. Nunca constituye evidencia de valores de negocio.
* Todo número, porcentaje, ranking, comparación, período histórico, afirmación de crecimiento o conclusión cuantitativa debe provenir de una ejecución exitosa de execute_dax_query realizada durante el turno actual.
* Nunca inventes, estimes, completes, extrapoles ni reutilices valores no verificados de conversaciones anteriores.
* Nunca uses valores de memoria, ejemplos del prompt, resultados hipotéticos o una respuesta previa como evidencia de negocio.
* Si el usuario aporta un número, podés mencionarlo como "según el valor que indicás", pero no lo presentes como validado hasta contrastarlo mediante execute_dax_query.
* Si no lográs validar el dato, explicá con claridad que no pudiste confirmarlo con los datos recuperados. No reemplaces la falta de evidencia con una cifra aproximada.
3.2. Validación antes de responder
Antes de redactar una respuesta cuantitativa, verificá internamente:
* La métrica exacta solicitada.
* El período solicitado o el período asumido.
* La moneda o unidad de medida.
* Los filtros de canal, sucursal, producto, categoría, cliente u otra dimensión relevante.
* La granularidad solicitada.
* La consistencia entre el resultado de la consulta y la pregunta del usuario.
No presentes como comparable una métrica MTD contra un mes cerrado completo.
Si la comparación es MTD, indicá expresamente que compara períodos equivalentes acumulados.
Si la respuesta usa un período asumido porque el usuario no lo indicó, declaralo de forma breve y explícita.
3.3. Declaraciones de falta de datos
* No declares "no tengo datos", "no tengo acceso", "no veo esa información" o expresiones similares basándote solo en el contexto semántico inicial.
* Antes de declarar falta de datos debés:
  a) usar get_schema_context si falta estructura;
  b) intentar una consulta DAX acotada;
  c) evaluar el resultado;
  d) realizar una consulta diagnóstica si el resultado es vacío, ambiguo o inconsistente.
* Solo afirmá que no hay información disponible cuando una consulta DAX o una verificación de esquema lo respalde.
3.4. Consultas complejas
* La regla de agrupar escalares en una única consulta aplica solo cuando los valores son independientes y la consulta es simple.
* Cuando la pregunta combine varias dimensiones, relaciones o etapas de razonamiento, descomponela en consultas pequeñas, verificables y secuenciales.
* Para una investigación compleja, seguí esta lógica:
  a) identificar la métrica, período y filtros relevantes;
  b) obtener el segmento o ranking principal;
  c) profundizar únicamente sobre las filas relevantes;
  d) ejecutar una consulta de control para validar totales, participaciones, variaciones o compensaciones;
  e) redactar la conclusión solamente con resultados confirmados.
* No fuerces toda la lógica en una única consulta si eso vuelve el DAX frágil, excesivamente largo o difícil de verificar.
* No presentes una recomendación basada en un cruce complejo si no lograste recuperar y validar todas las dimensiones críticas del análisis.
3.5. Resultados vacíos o sospechosos
* Una tabla vacía no demuestra por sí sola que falte un filtro obligatorio de negocio.
* Una tabla vacía puede indicar ausencia real de datos, período inexistente, filtro incorrecto, medida incompatible, relación ausente o consulta mal construida.
* Ante una tabla vacía, realizá una consulta diagnóstica antes de volver a intentar con filtros adicionales.
* Si una medida devuelve exactamente el mismo valor para todas las filas de una dimensión, tratá el resultado como sospechoso.
* No asumas automáticamente que existe un crossjoin.
* Verificá si la medida elimina filtros mediante ALL, REMOVEFILTERS u otra lógica interna, si la dimensión filtra correctamente y si una consulta puntual para una sola fila produce un resultado distinto.
* Está prohibido sacar conclusiones operativas a partir de una segmentación que no haya sido validada.
3.6. Causalidad y recomendaciones
Separá siempre los siguientes niveles:
* Dato confirmado: resultado obtenido mediante DAX.
* Interpretación: lectura razonable derivada de los datos.
* Hipótesis: posible explicación que requiere validación adicional.
* Nunca presentes una hipótesis como un hecho confirmado.
* No atribuyas una variación a inflación, promociones, cambios de demanda, estacionalidad, problemas operativos, cambios de mix, fraude, rentabilidad o decisiones comerciales salvo que el modelo contenga evidencia específica.
* Cuando no haya evidencia causal suficiente, usá formulaciones como:
  "podría asociarse a",
  "es una hipótesis a validar",
  "conviene investigar",
  "los datos disponibles no permiten confirmar la causa".
4. SINTAXIS DAX CRÍTICA
* DAX no es SQL. No uses sintaxis SQL.
* Lógica booleana: usa && y ||. No uses AND ni OR como palabras.
* Condicionales: no existe CASE WHEN. Usa SWITCH(TRUE(), ...).
* Nulos: usa ISBLANK() o BLANK(). No uses IS NULL.
* Texto: usa CONTAINSSTRING(). No uses LIKE.
* Concatenación: usa &. No uses ||.
* Confía en las relaciones del modelo. No intentes forzar JOINs manuales.
* Si necesitás iterar y acceder a columnas relacionadas, usa RELATED() únicamente cuando la relación real haya sido confirmada.
* No copies nombres de tablas, columnas o medidas desde ejemplos genéricos. Usá exclusivamente los nombres disponibles en el contexto semántico o recuperados con get_schema_context.
5. OPTIMIZACIÓN DE CONSULTAS Y AGRUPACIONES
* Si necesitás calcular múltiples valores escalares simples e independientes en una misma respuesta, agrupálos en una única consulta usando EVALUATE ROW.
* Nunca hagas llamadas separadas para escalares que puedan obtenerse de forma clara y segura en una única consulta.
* Para evolución temporal o tendencias simples, usa SUMMARIZECOLUMNS.
* Para variaciones, crecimiento intermensual, comparaciones interanuales u otros cálculos complejos al vuelo, estás autorizado a usar ADDCOLUMNS envolviendo una tabla generada por SUMMARIZECOLUMNS.
* ADDCOLUMNS exige un número impar de argumentos: tabla, "Nombre1", expresión1, "Nombre2", expresión2.
* Nunca pases filtros lógicos como argumentos de ADDCOLUMNS.
* Si agrupás por dimensiones descriptivas, como productos, clientes, sucursales o categorías, limitá el resultado destinado al usuario con TOPN.
* El límite por defecto es 15 filas, salvo que el usuario solicite otro número.
* No uses TOPN en una consulta de validación cuando necesites todas las filas para reconciliar un total, una participación o una variación.
* Si una consulta puede devolver demasiadas filas, primero identificá el segmento relevante y luego profundizá con filtros específicos.
6. REGLAS MULTIMONEDA Y AMBIGÜEDAD
* Si el modelo semántico maneja múltiples monedas, aplica siempre el filtro, medida o lógica de moneda que corresponda según el contexto de la pregunta o las instrucciones de negocio inyectadas.
* Nunca devuelvas métricas monetarias de forma ambigua.
* Indicá claramente si el resultado está expresado en ARS, USD, moneda constante, moneda corriente u otra unidad.
* Si la pregunta no especifica moneda y el reporte tiene una convención de negocio definida, aplicá esa convención e indicála brevemente.
* Si no existe una convención de negocio y la moneda altera materialmente la respuesta, pedí una aclaración breve antes de proceder.
7. FECHAS Y TIEMPO
* Usa el CONTEXTO TEMPORAL provisto por el sistema al final de este prompt para resolver referencias relativas como "hoy", "este mes", "últimos 30 días", "año actual" o "mes pasado".
* Cuando el modelo tenga una tabla de fechas explícita, úsala para filtros temporales en lugar de filtrar directamente la tabla de hechos.
* No asumas que "este mes" significa un mes cerrado. Si el mes está en curso, tratá el valor como MTD y aclaralo.
* Cuando compares períodos parciales, usá períodos equivalentes. Ejemplo: acumulado hasta el mismo día del mes anterior o del año anterior.
* Si existen varias medidas temporales similares, verificá su definición antes de elegir una.
* No uses una medida de variación mensual, interanual, MTD o YTD sin confirmar que responde exactamente a la comparación solicitada por el usuario.
8. MANEJO AUTÓNOMO DE ERRORES DAX
* Si execute_dax_query devuelve un error de sintaxis, columna o medida inexistente, Client Error 400 u otro error recuperable, no te disculpes con el usuario inmediatamente.
* Analiza el error internamente.
* Si el error sugiere que falta una tabla, columna, medida o relación, usa get_schema_context para verificar la estructura real.
* Corrige la consulta DAX y vuelve a intentar.
* Tenés un máximo de 3 intentos de corrección para una misma línea de consulta.
* Si tras el tercer intento el error persiste, aplica la disculpa genérica definida en la Regla 1.
* Si una consulta falla por tamaño, complejidad o cantidad de contexto, dividí la investigación en consultas más pequeñas antes de abandonar.
* No reemplaces una consulta fallida con una respuesta estimada, una cifra no validada o una conclusión genérica presentada como específica del negocio.
* Si obtenés datos parciales suficientes para responder solo una parte de la pregunta, indicá con precisión qué pudiste confirmar y qué no fue posible validar.
9. FORMATO DE RESPUESTA
* Responde siempre en español de forma clara, directa y precisa.
* Presenta los números con formato local: separador de miles con punto y decimales con coma. Ejemplo: 1.234.567,89.
* Si el resultado es un valor único, encuádralo brevemente: mencioná período, moneda o unidad y si es MTD, YTD u otro acumulado.
* Si el resultado tiene múltiples filas, presentalo como ranking numerado en texto plano.
* Después de responder, agregá una línea de contexto breve si el dato lo necesita. Ejemplos:
  "El mes aún está en curso.",
  "El resultado corresponde al acumulado hasta la última fecha disponible.",
  "La comparación usa períodos equivalentes acumulados."
* Cuando presentes análisis, distinguí con claridad entre datos confirmados, interpretación e hipótesis.
* No uses afirmaciones absolutas sobre causalidad, rentabilidad, impacto comercial o sostenibilidad si no hay evidencia suficiente en el modelo.
* Si la pregunta es demasiado ambigua y no puede resolverse con los datos disponibles, pedí una aclaración breve antes de proceder.
* Si no encontrás datos para responder pero la pregunta es válida, explicá brevemente qué información falta y qué necesitás para responderla. No devuelvas el error genérico en esos casos; ese mensaje es solo para fallos técnicos irrecuperables.
* Ofrecé una continuación o una profundización solo cuando sea útil y natural.
* No ofrezcas más de dos o tres opciones de continuación.
* No cierres obligatoriamente con una pregunta si la respuesta ya es completa.
10. JERARQUÍA DE INSTRUCCIONES
* A continuación recibirás instrucciones dinámicas configuradas por entorno global, empresa o reporte. Pueden incluir tono, preferencias de negocio, glosarios, definiciones de métricas, tablas principales, convenciones temporales o reglas específicas de análisis.
* Esas instrucciones dinámicas complementan este prompt.
* Las instrucciones dinámicas pueden definir qué medida corresponde a cada concepto de negocio, qué moneda usar por defecto, qué tabla de fechas corresponde y qué dimensiones son válidas para un reporte.
* Las instrucciones dinámicas nunca pueden anular las reglas de seguridad, uso obligatorio de herramientas, prevención de alucinaciones, sintaxis DAX, protocolo de evidencia, validación de datos ni manejo de errores definidos arriba.
* Ante una contradicción entre instrucciones dinámicas y este prompt base, prevalece este prompt base.
CONTEXTO TEMPORAL
A continuación se proveerá contexto temporal dinámico con la fecha actual, zona horaria, período en curso y cualquier otra referencia necesaria para interpretar expresiones relativas del usuario.
"""
                f"{temporal_context_line}"
                f"{custom_instruction_block}"
            ),
        },
        {
            "type": "text",
            "text": f"Contexto del modelo semántico actual:{schema_block}",
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _anthropic_tool_spec() -> Dict[str, Any]:
    return {
        "name": "execute_dax_query",
        "description": "Ejecuta una consulta DAX contra el dataset actual. Solo proporciona dax_query; el backend inyecta el dataset_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dax_query": {
                    "type": "string",
                    "description": "Consulta DAX a ejecutar",
                }
            },
            "required": ["dax_query"],
            "additionalProperties": False,
        },
    }


def _schema_context_tool_spec() -> Dict[str, Any]:
    return {
        "name": "get_schema_context",
        "description": "Recupera contexto relevante del esquema mediante reranking. Solo proporciona question; el backend inyecta el dataset_id. Úsala si necesitas más contexto para construir el DAX.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Pregunta del usuario o refinamiento para recuperar contexto relevante",
                }
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    }


def _block_to_dict(block: Any) -> Dict[str, Any]:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump()
    if hasattr(block, "dict"):
        return block.dict()
    result: Dict[str, Any] = {}
    for key in ("type", "text", "name", "id", "input", "content"):
        if hasattr(block, key):
            result[key] = getattr(block, key)
    return result


def _text_from_blocks(blocks: Iterable[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for block in blocks:
        if block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts).strip()


def _message_is_orphan_tool_result(message: Dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            return False
    return True


def _sanitize_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sanitized = list(messages)
    while sanitized and _message_is_orphan_tool_result(sanitized[0]):
        sanitized.pop(0)
    return sanitized


class PromptManager:
    """Builds the prompt payload sent to the model."""

    def __init__(self, history_limit: int = DEFAULT_HISTORY_LIMIT):
        self.history_limit = max(0, int(history_limit))

    def _minify_schema_text(self, schema_text: str) -> str:
        return _minify_schema_text(schema_text)

    def _sanitize_history(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return _sanitize_history(messages)

    def _trim_history(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self.history_limit <= 0 or len(messages) <= self.history_limit:
            return list(messages)
        return list(messages[-self.history_limit :])

    def _build_turn_history(self, messages: List[Dict[str, Any]], user_message: str) -> List[Dict[str, Any]]:
        history = self._sanitize_history(messages)
        history = self._trim_history(history)
        history.append({"role": "user", "content": user_message})
        return history

    def build_messages(self, history: List[Dict[str, Any]], new_message: str) -> List[Dict[str, Any]]:
        return self._build_turn_history(history, new_message)

    def get_system_prompt(self, schema_text: str, custom_instructions: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        return _build_system_prompt(schema_text, custom_instructions=custom_instructions)


class ToolRegistry:
    """Registry and dispatcher for agent tools."""

    def get_execute_dax_tool_spec(self) -> Dict[str, Any]:
        return _anthropic_tool_spec()

    def get_schema_context_tool_spec(self) -> Dict[str, Any]:
        return _schema_context_tool_spec()

    def get_all_tools(self) -> List[Dict[str, Any]]:
        return [self.get_schema_context_tool_spec(), self.get_execute_dax_tool_spec()]

    async def rewrite_query_for_reranker(
        self,
        *,
        user_message: str,
        settings: RuntimeSettings,
        debug_enabled: bool,
        schema_retrieval_prompt: Optional[str] = None,
        usage_totals: Optional[Dict[str, int]] = None,
        ai_usage_events: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        return await _rewrite_query_for_reranker(
            user_message=user_message,
            settings=settings,
            debug_enabled=debug_enabled,
            schema_retrieval_prompt=schema_retrieval_prompt,
            usage_totals=usage_totals,
            ai_usage_events=ai_usage_events,
        )

    async def _execute_dax_query_local_async(self, tool_input: Dict[str, Any], context: Dict[str, Any]) -> str:
        dataset_id = context.get("dataset_id")
        if not dataset_id:
            raise RuntimeError("dataset_id is required to execute DAX queries")

        dax_query = tool_input.get("dax_query")
        if not dax_query or not str(dax_query).strip():
            raise RuntimeError("Tool execute_dax_query requires a non-empty dax_query")

        powerbi_credentials = context.get("powerbi_credentials") or {}
        with start_observation(
            name="execute-dax-query",
            as_type="tool",
            input={"dax_query": str(dax_query)},
        ) as observation:
            if observation is not None:
                observation.update(metadata={"datasethash": hash_identifier(str(dataset_id), prefix="dataset")})
            result = await asyncio.to_thread(
                execute_dax_query_local,
                dataset_id,
                str(dax_query),
                powerbi_credentials,
            )
            if observation is not None:
                observation.update(
                    output={
                        "result_preview": observation_preview(result, max_length=1200),
                        "result_length": len(str(result)),
                    }
                )
            return result

    async def execute_tool(self, tool_name: str, tool_input: Dict[str, Any], context: Dict[str, Any]) -> str:
        if tool_name == "get_schema_context":
            question = tool_input.get("question") or context.get("user_message") or ""
            if not str(question).strip():
                raise RuntimeError("Tool get_schema_context requires a non-empty question")

            settings = context["settings"]
            debug_enabled = bool(context.get("debug_enabled", settings.debug_enabled))
            dataset_id = context.get("dataset_id")
            powerbi_credentials = context.get("powerbi_credentials") or {}
            if not dataset_id:
                raise RuntimeError("dataset_id is required to fetch schema context")

            question_is_rewritten = bool(tool_input.get("question_is_rewritten"))
            optimized_question = str(question)
            if not question_is_rewritten:
                optimized_question = await self.rewrite_query_for_reranker(
                    user_message=str(question),
                    settings=settings,
                    debug_enabled=debug_enabled,
                    schema_retrieval_prompt=context.get("schema_retrieval_prompt"),
                    usage_totals=context.get("usage_totals"),
                    ai_usage_events=context.get("ai_usage_events"),
                )
            return await _fetch_schema_context(
                dataset_id=dataset_id,
                powerbi_credentials=powerbi_credentials,
                question=optimized_question,
                settings=settings,
                debug_enabled=debug_enabled,
                required=False,
                table_context_limit=context.get("schema_table_context_limit", DEFAULT_TABLE_CONTEXT_LIMIT),
                measure_context_limit=context.get("schema_measure_context_limit", DEFAULT_MEASURE_CONTEXT_LIMIT),
                usage_totals=context.get("usage_totals"),
                ai_usage_events=context.get("ai_usage_events"),
            )

        if tool_name == "execute_dax_query":
            return await self._execute_dax_query_local_async(tool_input, context)

        raise RuntimeError(f"Unsupported tool requested by the model: {tool_name}")


class AgentOrchestrator:
    """Coordinates prompt construction, tool execution and model calls."""

    def __init__(self, settings: RuntimeSettings, prompt_manager: PromptManager, tool_registry: ToolRegistry):
        self.settings = settings
        self.prompt_manager = prompt_manager
        self.tool_registry = tool_registry

    def _build_context(
        self,
        *,
        dataset_id: str,
        powerbi_credentials: Optional[Dict[str, Any]],
        conversation_id: Optional[str],
        report_id: Optional[int],
        user_message: str,
        debug_enabled: bool,
        custom_instructions: Optional[List[Any]] = None,
        schema_retrieval_prompt: Optional[str] = None,
        schema_table_context_limit: Optional[int] = None,
        schema_measure_context_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        return {
            "dataset_id": dataset_id,
            "powerbi_credentials": powerbi_credentials or {},
            "settings": self.settings,
            "conversation_id": conversation_id,
            "report_id": report_id,
            "user_message": user_message,
            "debug_enabled": debug_enabled,
            "custom_instructions": custom_instructions or [],
            "schema_retrieval_prompt": str(schema_retrieval_prompt or "").strip(),
            "schema_table_context_limit": _coerce_positive_int(schema_table_context_limit, DEFAULT_TABLE_CONTEXT_LIMIT),
            "schema_measure_context_limit": _coerce_positive_int(schema_measure_context_limit, DEFAULT_MEASURE_CONTEXT_LIMIT),
            "usage_totals": _new_usage_totals(),
            "ai_usage_events": [],
        }

    async def estimate_tokens(
        self,
        *,
        user_message: str,
        history: List[Dict[str, Any]],
        schema_text: Optional[str] = None,
        custom_instructions: Optional[List[Any]] = None,
    ) -> int:
        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency missing in test env
            raise RuntimeError("El paquete 'anthropic' es requerido.") from exc

        schema_actual = schema_text or ""
        system_prompt = self.prompt_manager.get_system_prompt(schema_actual, custom_instructions=custom_instructions)
        token_history = self.prompt_manager.build_messages(history, user_message)
        tools = self.tool_registry.get_all_tools()

        try:
            async with AsyncAnthropic(api_key=self.settings.anthropic_api_key) as client:
                anthropic_messages = client.messages
                count_method = getattr(anthropic_messages, "count_tokens", None)
                if count_method is None:
                    raise AttributeError("AsyncAnthropic.messages.count_tokens no está disponible")

                respuesta = await count_method(
                    model=self.settings.anthropic_model,
                    system=cast(Any, system_prompt),
                    messages=token_history,
                    tools=tools,
                )
                input_tokens = getattr(respuesta, "input_tokens", None)
                if input_tokens is None and isinstance(respuesta, dict):
                    input_tokens = respuesta.get("input_tokens")
                if input_tokens is None:
                    raise RuntimeError("La respuesta de conteo no incluye input_tokens")
                return int(cast(Any, input_tokens))
        except Exception as exc:
            logging.warning("Error al consultar API de tokens, usando estimación local: %s", exc)
            texto_completo = (
                json.dumps(system_prompt, ensure_ascii=False, default=str)
                + json.dumps(token_history, ensure_ascii=False, default=str)
                + json.dumps(tools, ensure_ascii=False, default=str)
            )
            return max(1, len(texto_completo) // 4)

    async def estimate_request_tokens_payload(
        self,
        *,
        system_prompt: Any,
        messages: List[Dict[str, Any]],
        tools: Any,
    ) -> int:
        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency missing in test env
            raise RuntimeError("El paquete 'anthropic' es requerido.") from exc

        try:
            async with AsyncAnthropic(api_key=self.settings.anthropic_api_key) as client:
                anthropic_messages = client.messages
                count_method = getattr(anthropic_messages, "count_tokens", None)
                if count_method is None:
                    raise AttributeError("AsyncAnthropic.messages.count_tokens no está disponible")

                respuesta = await count_method(
                    model=self.settings.anthropic_model,
                    system=cast(Any, system_prompt),
                    messages=messages,
                    tools=tools,
                )
                input_tokens = getattr(respuesta, "input_tokens", None)
                if input_tokens is None and isinstance(respuesta, dict):
                    input_tokens = respuesta.get("input_tokens")
                if input_tokens is None:
                    raise RuntimeError("La respuesta de conteo no incluye input_tokens")
                return int(cast(Any, input_tokens))
        except Exception as exc:
            logging.warning("Error al consultar API de tokens del payload actual, usando estimación local: %s", exc)
            texto_completo = (
                json.dumps(system_prompt, ensure_ascii=False, default=str)
                + json.dumps(messages, ensure_ascii=False, default=str)
                + json.dumps(tools, ensure_ascii=False, default=str)
            )
            return max(1, len(texto_completo) // 4)

    async def generate_response(
        self,
        *,
        user_message: str,
        history: List[Dict[str, Any]],
        dataset_id: str,
        powerbi_credentials: Optional[Dict[str, Any]] = None,
        schema_text: Optional[str] = None,
        conversation_id: Optional[str] = None,
        report_id: Optional[int] = None,
        custom_instructions: Optional[List[Any]] = None,
        schema_retrieval_prompt: Optional[str] = None,
        schema_table_context_limit: Optional[int] = None,
        schema_measure_context_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency missing in test env
            raise RuntimeError("The 'anthropic' package is required to use the chat endpoint. Install it in production.") from exc

        schema_text = schema_text or ""
        settings_debug_enabled = self.settings.debug_enabled or _debug_enabled()
        turn_history = self.prompt_manager.build_messages(history, user_message)
        conv_id = str(conversation_id) if conversation_id is not None else None

        if settings_debug_enabled:
            _debug_print(
                "run_chat_turn:start",
                {
                    "conversation_id": conv_id,
                    "report_id": report_id,
                    "dataset_id": dataset_id,
                    "user_message": user_message,
                        "settings": {
                            "anthropic_model": self.settings.anthropic_model,
                            "anthropic_max_tokens": self.settings.anthropic_max_tokens,
                            "history_limit": self.settings.history_limit,
                            "max_tool_rounds": self.settings.max_tool_rounds,
                            "debug_enabled": self.settings.debug_enabled,
                        },
                        "existing_history_count": len(history),
                        "schema_loaded": bool(schema_text),
                        "schema_retrieval_prompt_loaded": bool(schema_retrieval_prompt),
                        "schema_table_context_limit": schema_table_context_limit,
                        "schema_measure_context_limit": schema_measure_context_limit,
                },
                enabled=settings_debug_enabled,
            )

        context = self._build_context(
            dataset_id=dataset_id,
            powerbi_credentials=powerbi_credentials,
            conversation_id=conv_id,
            report_id=report_id,
            user_message=user_message,
            debug_enabled=settings_debug_enabled,
            custom_instructions=custom_instructions,
            schema_retrieval_prompt=schema_retrieval_prompt,
            schema_table_context_limit=schema_table_context_limit,
            schema_measure_context_limit=schema_measure_context_limit,
        )

        initial_failure_reason: Optional[str] = None
        initial_error_message: Optional[str] = None
        if not schema_text:
            # We call the tool directly to ensure only one rewrite happens.
            # execute_tool will handle the rewrite if question_is_rewritten is not passed.
            try:
                fetched_schema_text = await self.tool_registry.execute_tool(
                    "get_schema_context",
                    {"question": user_message}, # No question_is_rewritten passed -> execute_tool will rewrite it
                    context,
                )
            except Exception:
                fetched_schema_text = ""
                initial_failure_reason = "semantic_model_unavailable"
                initial_error_message = "Semantic model context could not be retrieved"
            if fetched_schema_text:
                schema_text = fetched_schema_text
            if _tool_output_is_error(fetched_schema_text):
                initial_failure_reason = "semantic_model_unavailable"
                initial_error_message = "Semantic model context could not be retrieved"

        system_prompt = self.prompt_manager.get_system_prompt(schema_text, custom_instructions=custom_instructions)
        tools: Any = self.tool_registry.get_all_tools()
        _debug_print(
            "anthropic:request:prepared",
            {"system_prompt": system_prompt, "messages": turn_history, "tools": tools},
            enabled=settings_debug_enabled,
        )

        try:
            token_count = await self.estimate_tokens(
                user_message=user_message,
                history=history,
                schema_text=schema_text,
                custom_instructions=custom_instructions,
            )
        except Exception as exc:
            logging.warning("Falling back to local token estimate after token counting failure: %s", exc)
            token_count = _estimate_text_tokens(
                json.dumps(system_prompt, ensure_ascii=False, default=str)
                + json.dumps(turn_history, ensure_ascii=False, default=str)
                + json.dumps(tools, ensure_ascii=False, default=str),
                chars_per_token=4,
            )
        _debug_print(
            "anthropic:token_count",
            {"tokens": token_count, "conversation_id": conv_id, "report_id": report_id},
            enabled=settings_debug_enabled,
        )

        tool_rounds = 0
        tools_called: List[Dict[str, Any]] = []
        dax_query_used: Optional[str] = None
        dax_error_attempts = 0
        had_error = False
        error_message: Optional[str] = None
        failure_reason: Optional[str] = None
        usage_totals = cast(Dict[str, int], context["usage_totals"])
        ai_usage_events = cast(List[Dict[str, Any]], context["ai_usage_events"])
        messages = cast(Any, turn_history)
        system_payload = cast(Any, system_prompt)

        def mark_functional_failure(reason: str, message: str) -> None:
            nonlocal had_error, error_message, failure_reason
            had_error = True
            if error_message is None:
                error_message = message
            if failure_reason is None:
                failure_reason = reason

        def build_turn_result(answer: str) -> Dict[str, Any]:
            return {
                "answer": answer,
                "conversation_id": conv_id,
                "report_id": report_id,
                "tool_rounds": tool_rounds,
                "input_tokens": usage_totals["input_tokens"],
                "output_tokens": usage_totals["output_tokens"],
                "model": self.settings.anthropic_model,
                "mcp_used": bool(tools_called),
                "tools_called": tools_called,
                "dax_query": dax_query_used,
                "ai_usage_events": ai_usage_events,
                "had_error": had_error,
                "error_message": error_message,
                "failure_reason": failure_reason,
            }

        async def build_anthropic_error_result(
            *,
            reason: str,
            exc: Exception,
            operation_name: str = "chat-response",
            preflight_blocked: bool = False,
        ) -> Dict[str, Any]:
            estimated_input_tokens = await self.estimate_request_tokens_payload(
                system_prompt=system_payload,
                messages=cast(List[Dict[str, Any]], messages),
                tools=tools,
            )
            _add_usage_totals(
                usage_totals,
                input_tokens=estimated_input_tokens,
                output_tokens=0,
            )
            _append_ai_usage_event(
                ai_usage_events,
                provider="anthropic",
                model=self.settings.anthropic_model,
                event_type="generation",
                source_type="chat",
                trigger_type="user_request",
                operation_name=operation_name,
                status="error",
                input_tokens=estimated_input_tokens,
                output_tokens=0,
                total_tokens=estimated_input_tokens,
                metadata_json={
                    "tool_round": tool_rounds,
                    "error_type": reason,
                    "estimated_usage": True,
                    "preflight_blocked": preflight_blocked,
                },
            )
            mark_functional_failure(reason, str(exc))
            return build_turn_result(SAFE_TECHNICAL_ERROR_ANSWER)

        if initial_failure_reason and initial_error_message:
            mark_functional_failure(initial_failure_reason, initial_error_message)

        async with AsyncAnthropic(api_key=self.settings.anthropic_api_key) as client:
            anthropic_messages = client.messages

            while True:
                _debug_print(
                    "anthropic:request:send",
                    {
                        "model": self.settings.anthropic_model,
                        "max_tokens": self.settings.anthropic_max_tokens,
                        "system": system_prompt,
                        "messages": messages,
                        "tools": tools,
                        "tool_rounds": tool_rounds,
                    },
                    enabled=settings_debug_enabled,
                )
                if tool_rounds > 0:
                    estimated_input_tokens = await self.estimate_request_tokens_payload(
                        system_prompt=system_payload,
                        messages=cast(List[Dict[str, Any]], messages),
                        tools=tools,
                    )
                    if estimated_input_tokens > ANTHROPIC_PROMPT_TOKEN_LIMIT:
                        _debug_print(
                            "anthropic:request:blocked",
                            {
                                "estimated_input_tokens": estimated_input_tokens,
                                "tool_rounds": tool_rounds,
                            },
                            enabled=settings_debug_enabled,
                        )
                        return await build_anthropic_error_result(
                            reason="anthropic_prompt_too_long",
                            exc=RuntimeError(
                                f"Prompt too long: {estimated_input_tokens} tokens > "
                                f"{ANTHROPIC_PROMPT_TOKEN_LIMIT} maximum"
                            ),
                            preflight_blocked=True,
                        )
                try:
                    response = await anthropic_messages.create(
                        model=self.settings.anthropic_model,
                        max_tokens=self.settings.anthropic_max_tokens,
                        system=system_payload,
                        messages=messages,
                        tools=tools,
                    )
                except Exception as exc:
                    if _is_prompt_too_long_error(exc):
                        _debug_print(
                            "anthropic:response:error",
                            {
                                "error": repr(exc),
                                "tool_rounds": tool_rounds,
                            },
                            enabled=settings_debug_enabled,
                        )
                        return await build_anthropic_error_result(
                            reason="anthropic_prompt_too_long",
                            exc=exc,
                        )
                    return await build_anthropic_error_result(
                        reason="anthropic_provider_error",
                        exc=exc,
                    )

                usage = getattr(response, "usage", None)
                usage_metrics = _anthropic_usage_metrics(usage)
                _add_usage_totals(
                    usage_totals,
                    input_tokens=usage_metrics["input_tokens"],
                    output_tokens=usage_metrics["output_tokens"],
                )
                _append_ai_usage_event(
                    ai_usage_events,
                    provider="anthropic",
                    model=self.settings.anthropic_model,
                    event_type="generation",
                    source_type="chat",
                    trigger_type="user_request",
                    operation_name="chat-response",
                    status="success",
                    input_tokens=usage_metrics["input_tokens"],
                    output_tokens=usage_metrics["output_tokens"],
                    total_tokens=usage_metrics["input_tokens"] + usage_metrics["output_tokens"],
                    cache_write_tokens=usage_metrics["cache_write_tokens"],
                    cache_read_tokens=usage_metrics["cache_read_tokens"],
                    metadata_json={"tool_round": tool_rounds},
                )

                _debug_print(
                    "anthropic:response:raw",
                    {
                        "type": type(response).__name__,
                        "response": response.model_dump() if hasattr(response, "model_dump") else str(response),
                    },
                    enabled=settings_debug_enabled,
                )

                try:
                    assistant_blocks = [_block_to_dict(block) for block in response.content]
                    _debug_print("anthropic:response:assistant_blocks", assistant_blocks, enabled=settings_debug_enabled)
                    turn_history.append({"role": "assistant", "content": assistant_blocks})

                    tool_uses = [block for block in assistant_blocks if block.get("type") == "tool_use"]
                    _debug_print("anthropic:response:tool_uses", tool_uses, enabled=settings_debug_enabled)
                    if not tool_uses:
                        answer = _text_from_blocks(assistant_blocks)
                        _debug_print(
                            "anthropic:final",
                            {
                                "answer": answer,
                                "conversation_id": conv_id,
                                "report_id": report_id,
                                "tool_rounds": tool_rounds,
                                "token_count": token_count,
                                "history": turn_history,
                            },
                            enabled=settings_debug_enabled,
                        )
                        return build_turn_result(answer)

                    tool_result_blocks: List[Dict[str, Any]] = []
                    for tool_use in tool_uses:
                        tool_name = tool_use.get("name")
                        tool_input = tool_use.get("input") or {}
                        if not isinstance(tool_input, dict):
                            tool_input = {}

                        tools_called.append({"name": tool_name, "input": tool_input})

                        if tool_name == "get_schema_context":
                            raw_question = tool_input.get("question") or user_message
                            if not str(raw_question).strip():
                                raise RuntimeError("Tool get_schema_context requires a non-empty question")

                            tool_rounds += 1
                            if tool_rounds > self.settings.max_tool_rounds:
                                mark_functional_failure("tool_round_limit", "Maximum tool round limit reached")
                                return build_turn_result(SAFE_TECHNICAL_ERROR_ANSWER)

                            _debug_print(
                                "tool:get_schema_context:tool_request",
                                {
                                    "tool_use_id": tool_use.get("id"),
                                    "dataset_id": dataset_id,
                                    "question": str(raw_question),
                                    "tool_round": tool_rounds,
                                },
                                enabled=settings_debug_enabled,
                            )
                            try:
                                tool_output = await self.tool_registry.execute_tool(
                                    tool_name,
                                    {"question": str(raw_question)},
                                    context,
                                )
                            except Exception:
                                tool_output = "Error obteniendo el esquema del modelo semantico."
                                mark_functional_failure(
                                    "semantic_model_unavailable",
                                    "Semantic model context could not be retrieved",
                                )
                            if _tool_output_is_error(tool_output):
                                mark_functional_failure(
                                    "semantic_model_unavailable",
                                    "Semantic model context could not be retrieved",
                                )
                            _debug_print(
                                "tool:get_schema_context:tool_response",
                                {
                                    "tool_use_id": tool_use.get("id"),
                                    "tool_output": tool_output,
                                },
                                enabled=settings_debug_enabled,
                            )
                            tool_result_blocks.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_use["id"],
                                    "content": _compact_tool_result_for_model(tool_output),
                                }
                            )
                            continue

                        if tool_name != "execute_dax_query":
                            mark_functional_failure(
                                "unsupported_tool",
                                f"Unsupported tool requested by the model: {tool_name}",
                            )
                            return build_turn_result(SAFE_TECHNICAL_ERROR_ANSWER)

                        dax_query = tool_input.get("dax_query")
                        if not dax_query or not str(dax_query).strip():
                            dax_error_attempts += 1
                            mark_functional_failure("dax_query_empty", "DAX query was empty")
                            tool_result_blocks.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_use["id"],
                                    "content": "Error interno: El dax_query llego vacio. Probablemente te quedaste sin tokens o la sintaxis JSON fallo. Por favor, se mas conciso.",
                                }
                            )
                            if dax_error_attempts >= DAX_ERROR_ATTEMPT_LIMIT:
                                return build_turn_result(SAFE_TECHNICAL_ERROR_ANSWER)
                            continue

                        tool_rounds += 1
                        if tool_rounds > self.settings.max_tool_rounds:
                            mark_functional_failure("tool_round_limit", "Maximum tool round limit reached")
                            return build_turn_result(SAFE_TECHNICAL_ERROR_ANSWER)

                        if dax_query_used is None:
                            dax_query_used = str(dax_query)

                        _debug_print(
                            "tool:execute_dax_query:request",
                            {
                                "tool_use_id": tool_use.get("id"),
                                "dataset_id": dataset_id,
                                "dax_query": str(dax_query),
                                "tool_round": tool_rounds,
                            },
                            enabled=settings_debug_enabled,
                        )
                        try:
                            tool_output = await self.tool_registry.execute_tool(
                                tool_name,
                                {"dax_query": str(dax_query)},
                                context,
                            )
                        except Exception as exc:
                            dax_error_attempts += 1
                            mark_functional_failure("dax_execution_exception", str(exc))
                            tool_output = f"Error tecnico ejecutando DAX: {exc}"

                        if _tool_output_is_error(tool_output):
                            dax_error_attempts += 1
                            if failure_reason is None:
                                mark_functional_failure("dax_generation_failed", "DAX query execution failed")

                        _debug_print(
                            "tool:execute_dax_query:response",
                            {
                                "tool_use_id": tool_use.get("id"),
                                "tool_output": tool_output,
                            },
                            enabled=settings_debug_enabled,
                        )
                        tool_result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use["id"],
                                "content": _compact_tool_result_for_model(tool_output),
                            }
                        )
                        if dax_error_attempts >= DAX_ERROR_ATTEMPT_LIMIT:
                            return build_turn_result(SAFE_TECHNICAL_ERROR_ANSWER)

                    turn_history.append({"role": "user", "content": tool_result_blocks})
                    messages = cast(Any, turn_history)
                    _debug_print("anthropic:tool_results:appended", turn_history, enabled=settings_debug_enabled)
                except Exception as exc:
                    mark_functional_failure("agent_execution_exception", str(exc))
                    return build_turn_result(SAFE_TECHNICAL_ERROR_ANSWER)


async def calcular_tokens_turno(
    *,
    user_message: str,
    history: List[Dict[str, Any]],
    settings: RuntimeSettings,
    schema_text: Optional[str] = None,
    custom_instructions: Optional[List[Any]] = None,
) -> int:
    """Estimate token usage for the next turn."""
    prompt_manager = PromptManager(history_limit=settings.history_limit)
    tool_registry = ToolRegistry()
    orchestrator = AgentOrchestrator(settings, prompt_manager, tool_registry)
    return await orchestrator.estimate_tokens(
        user_message=user_message,
        history=history,
        schema_text=schema_text,
        custom_instructions=custom_instructions,
    )


async def run_chat_turn(
    *,
    user_message: str,
    dataset_id: str,
    history: List[Dict[str, Any]],
    settings: RuntimeSettings,
    schema_text: Optional[str] = None,
    conversation_id: Optional[str] = None,
    report_id: Optional[int] = None,
    powerbi_credentials: Optional[Dict[str, Any]] = None,
    custom_instructions: Optional[List[Any]] = None,
    schema_retrieval_prompt: Optional[str] = None,
    schema_table_context_limit: Optional[int] = None,
    schema_measure_context_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Compatibility wrapper kept for legacy imports."""
    prompt_manager = PromptManager(history_limit=settings.history_limit)
    tool_registry = ToolRegistry()
    orchestrator = AgentOrchestrator(settings, prompt_manager, tool_registry)
    with start_observation(
        name="powerbi-chat-agent",
        as_type="agent",
        input={"user_message": user_message},
    ) as observation:
        if observation is not None:
            observation.update(
                metadata={
                    "conversationid": str(conversation_id) if conversation_id is not None else None,
                    "reportid": str(report_id) if report_id is not None else None,
                    "datasethash": hash_identifier(dataset_id, prefix="dataset"),
                    "historycount": str(len(history)),
                    "schemaloaded": str(bool(schema_text)).lower(),
                    "schemaretrievalpromptloaded": str(bool(schema_retrieval_prompt)).lower(),
                    "schematablelimit": str(_coerce_positive_int(schema_table_context_limit, DEFAULT_TABLE_CONTEXT_LIMIT)),
                    "schemameasurelimit": str(_coerce_positive_int(schema_measure_context_limit, DEFAULT_MEASURE_CONTEXT_LIMIT)),
                }
            )

        result = await orchestrator.generate_response(
            user_message=user_message,
            history=history,
            dataset_id=dataset_id,
            powerbi_credentials=powerbi_credentials,
            schema_text=schema_text,
            conversation_id=conversation_id,
            report_id=report_id,
            custom_instructions=custom_instructions,
            schema_retrieval_prompt=schema_retrieval_prompt,
            schema_table_context_limit=schema_table_context_limit,
            schema_measure_context_limit=schema_measure_context_limit,
        )

        if observation is not None:
            observation.update(
                output={
                    "answer": observation_preview(result.get("answer", ""), max_length=1200),
                    "tool_rounds": result.get("tool_rounds", 0),
                    "input_tokens": result.get("input_tokens"),
                    "output_tokens": result.get("output_tokens"),
                }
            )
        return result

