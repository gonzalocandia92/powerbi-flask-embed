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
DEFAULT_HISTORY_LIMIT = 10
DEFAULT_MAX_TOOL_ROUNDS = 10
DEFAULT_DEBUG_ENABLED = True
DEFAULT_PROMPT_CACHING_ENABLED = True
DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS = 20
VOYAGE_QUERY_EMBEDDING_MODEL = "voyage-4"
VOYAGE_QUERY_EMBEDDING_COST_PER_MILLION_USD = 0.06

DATE_TABLE_GUIDANCE = (
    '"Date": ["Date (Date)", "Anio (Integer)", "Mes (Integer)", "Day (Integer)", '
    '"SemanaAnio (Integer)", "Semana Inicio y Fin (Text)", "Semana Inicio y Fin Resumido (Text)", '
    '"AnioMes (Integer)", "AnioSemana (Integer)", "SemanaAnioID (Integer)", "fecha2 (Date)", '
    '"FechaMaxDolar (Integer)", "weekday (Text)", "weekday_nro (Integer)", "Trimestre (Integer)", '
    '"AnioTrimestre (Integer)", "AnioMesTexto (Text)"'
)

TEMPORAL_CONTEXT_TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")
TEMPORAL_CONTEXT_LOCATION = "Resistencia, Chaco, Argentina"

MONETARY_FILTER_GUIDANCE = """
- FILTRO DE MONEDA OBLIGATORIO: Medidas como [Ticket Promedio] devolverán BLANK/NULL si omites la moneda.
Por defecto, SIEMPRE inyecta 'Moneda'[Moneda Campos] = "'Medidas'[Ventas ARS]" dentro de tus CALCULATE.
Usa 'Moneda'[Moneda Campos] = "'Medidas'[Ventas USD]" SOLO si el usuario pide explícitamente dólares (USD).
"""

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

        system_prompt = (
            "Eres un experto en bases de datos y Power BI. Tu unica tarea es extraer y deducir "
            "los terminos tecnicos mas probables de la pregunta del usuario. "
            "Reglas: Devuelve SOLO una lista de 5 a 8 palabras clave separadas por comas. "
            "No incluyas saludos, explicaciones ni vinietas. "
            "IMPORTANTE: Si la pregunta involucra ventas, facturación o tickets, INCLUYE SIEMPRE la palabra 'sales_order'. "
            "Si involucra tiempo, incluye 'Date'."
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
                .limit(3)
                .all()
            )
            measure_results = (
                SchemaEmbedding.query
                .filter(
                    SchemaEmbedding.dataset_id == dataset_id,
                    SchemaEmbedding.item_type == "measure",
                )
                .order_by(SchemaEmbedding.embedding.cosine_distance(query_vector_list))
                .limit(5)
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


def _build_system_prompt(schema_text: str) -> List[Dict[str, Any]]:
    schema_block = _minify_schema_text(schema_text)
    temporal_context_line = _build_temporal_context_line()
    return [
        {
            "type": "text",
            "text": (
                "Tu nombre es Klara. Eres un asistente experto de analítica para Power BI. Reglas estrictas de seguridad, sintaxis y optimización DAX:\n"
                "- 1. USO DE MEDIDAS (¡CRÍTICO!): Si el 'Contexto del modelo semántico actual' ya contiene las medidas solicitadas (ej. [Ticket Promedio], [Ventas por producto]), DEBES usarlas directamente en tu consulta invocándolas entre corchetes (ej. CALCULATE([Nombre De La Medida])). NO intentes recrear la lógica matemática con SUMX, AVERAGEX, FILTER, etc. Usa la herramienta execute_dax_query directamente. NO uses get_schema_context si la medida ya existe en tu contexto.\n"
                "- 2. INSTRUCCIÓN BASE: Cuando uses execute_dax_query, usa SIEMPRE la instrucción EVALUATE. Nunca pidas, menciones ni intentes inferir dataset_id o workspace_id.\n"
                "- 3. TABLA DE HECHOS: La principal es 'sales_order'. Se relaciona con la tabla de fechas mediante 'sales_order'[date_order] -> 'Date'[Date].\n"
                "- 4. OPTIMIZACIÓN DAX (ESCALARES): Si necesitas calcular múltiples valores escalares (ej. ARS y USD, o distintos periodos), DEBES agruparlos en una única consulta utilizando múltiples columnas dentro de EVALUATE ROW. Nunca hagas llamadas separadas a la herramienta.\n"
                "  Ejemplo: EVALUATE ROW(\"Métrica 1\", CALCULATE(...), \"Métrica 2\", CALCULATE(...))\n"
                f"- 5. TABLA DE FECHAS: {DATE_TABLE_GUIDANCE}\n"
                f"- 6. FILTRO DE MONEDA OBLIGATORIO: {MONETARY_FILTER_GUIDANCE}\n"
                "- 7. SINTAXIS DAX CRÍTICA (¡Cuidado con SQL!):\n"
                "  a. Lógica: Usa '&&' (AND) y '||' (OR). Jamás uses AND/OR como palabras.\n"
                "  b. Condicionales: No existe CASE WHEN. Usa SWITCH(TRUE(), ...).\n"
                "  c. Nulos: Usa ISBLANK() o BLANK(), nunca IS NULL.\n"
                "  d. Texto: Usa CONTAINSSTRING() en lugar de LIKE.\n"
                "  e. Concatenación: Usa '&', nunca '||'.\n"
                "  f. Relaciones: Confía en las relaciones del modelo. Usa RELATED() si iteras, no intentes forzar JOINs manuales.\n"
                "- 8. TABLAS Y EVOLUCIÓN: Si el usuario pide una evolución temporal, tendencias, o agrupaciones, NUNCA uses ADDCOLUMNS(SUMMARIZE(...)). Usa SIEMPRE la función SUMMARIZECOLUMNS.\n"
                "  Ejemplo: EVALUATE SUMMARIZECOLUMNS('Date'[Anio], 'Date'[Mes], \"Filtro\", FILTER('Date', ...), \"Ticket ARS\", CALCULATE([Medida Existente])) ORDER BY 'Date'[Anio], 'Date'[Mes]\n"
                "- 9.!CRITICO! PROTECCIÓN DE VOLUMEN (TOPN): Si la consulta agrupa por dimensiones descriptivas (ej. productos, clientes, sucursales), ESTÁS OBLIGADO a envolver SUMMARIZECOLUMNS con la función TOPN para devolver un máximo de 15 resultados, a menos que el usuario pida un número distinto.\n"
                "  Ejemplo de sintaxis estricta: EVALUATE TOPN(15, SUMMARIZECOLUMNS('Tabla'[Dimension], \"Metrica\", [Medida Existente]), [Metrica], DESC)\n"
                "- 10. FORMATO DE RESPUESTA: Responde en español y sé preciso. Nunca uses markdown (asteriscos, almohadillas, guiones bajos) en tu respuesta final.\n"
                "- 11. MANEJO DE ERRORES Y SEGURIDAD (CRÍTICO): Si ocurre cualquier tipo de fallo técnico (ej. error al ejecutar la consulta DAX, imposibilidad de recuperar el dataset_id o errores de conexión), "
                "tu respuesta al usuario debe ser extremadamente corta, concisa y en un tono de disculpa genérico. "
                "ESTÁ ESTRICTAMENTE PROHIBIDO exponer en el mensaje de error cualquier detalle interno del modelo o la arquitectura. "
                "Nunca menciones el 'dataset_id', el 'workspace_id', el código DAX generado, ni reveles nombres de tablas, columnas o medidas."
                f"{temporal_context_line}"
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

    def get_system_prompt(self, schema_text: str) -> List[Dict[str, Any]]:
        return _build_system_prompt(schema_text)


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
        usage_totals: Optional[Dict[str, int]] = None,
        ai_usage_events: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        return await _rewrite_query_for_reranker(
            user_message=user_message,
            settings=settings,
            debug_enabled=debug_enabled,
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
    ) -> Dict[str, Any]:
        return {
            "dataset_id": dataset_id,
            "powerbi_credentials": powerbi_credentials or {},
            "settings": self.settings,
            "conversation_id": conversation_id,
            "report_id": report_id,
            "user_message": user_message,
            "debug_enabled": debug_enabled,
            "usage_totals": _new_usage_totals(),
            "ai_usage_events": [],
        }

    async def estimate_tokens(
        self,
        *,
        user_message: str,
        history: List[Dict[str, Any]],
        schema_text: Optional[str] = None,
    ) -> int:
        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency missing in test env
            raise RuntimeError("El paquete 'anthropic' es requerido.") from exc

        schema_actual = schema_text or ""
        system_prompt = self.prompt_manager.get_system_prompt(schema_actual)
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
        )

        if not schema_text:
            # We call the tool directly to ensure only one rewrite happens.
            # execute_tool will handle the rewrite if question_is_rewritten is not passed.
            fetched_schema_text = await self.tool_registry.execute_tool(
                "get_schema_context",
                {"question": user_message}, # No question_is_rewritten passed -> execute_tool will rewrite it
                context,
            )
            if fetched_schema_text:
                schema_text = fetched_schema_text

        system_prompt = self.prompt_manager.get_system_prompt(schema_text)
        tools: Any = self.tool_registry.get_all_tools()
        _debug_print(
            "anthropic:request:prepared",
            {"system_prompt": system_prompt, "messages": turn_history, "tools": tools},
            enabled=settings_debug_enabled,
        )

        token_count = await self.estimate_tokens(
            user_message=user_message,
            history=history,
            schema_text=schema_text,
        )
        _debug_print(
            "anthropic:token_count",
            {"tokens": token_count, "conversation_id": conv_id, "report_id": report_id},
            enabled=settings_debug_enabled,
        )

        tool_rounds = 0
        tools_called: List[Dict[str, Any]] = []
        dax_query_used: Optional[str] = None
        usage_totals = cast(Dict[str, int], context["usage_totals"])
        ai_usage_events = cast(List[Dict[str, Any]], context["ai_usage_events"])
        messages = cast(Any, turn_history)
        system_payload = cast(Any, system_prompt)

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
                response = await anthropic_messages.create(
                    model=self.settings.anthropic_model,
                    max_tokens=self.settings.anthropic_max_tokens,
                    system=system_payload,
                    messages=messages,
                    tools=tools,
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
                    }

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
                            raise RuntimeError("Maximum tool round limit reached")

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
                        tool_output = await self.tool_registry.execute_tool(
                            tool_name,
                            {"question": str(raw_question)},
                            context,
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
                            {"type": "tool_result", "tool_use_id": tool_use["id"], "content": tool_output}
                        )
                        continue

                    if tool_name != "execute_dax_query":
                        raise RuntimeError(f"Unsupported tool requested by the model: {tool_name}")

                    dax_query = tool_input.get("dax_query")
                    if not dax_query or not str(dax_query).strip():
                        tool_result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use["id"],
                                "content": "Error interno: El dax_query llegó vacío. Probablemente te quedaste sin tokens o la sintaxis JSON falló. Por favor, sé más conciso.",
                            }
                        )
                        continue

                    tool_rounds += 1
                    if tool_rounds > self.settings.max_tool_rounds:
                        raise RuntimeError("Maximum tool round limit reached")

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
                    tool_output = await self.tool_registry.execute_tool(
                        tool_name,
                        {"dax_query": str(dax_query)},
                        context,
                    )
                    _debug_print(
                        "tool:execute_dax_query:response",
                        {
                            "tool_use_id": tool_use.get("id"),
                            "tool_output": tool_output,
                        },
                        enabled=settings_debug_enabled,
                    )
                    tool_result_blocks.append(
                        {"type": "tool_result", "tool_use_id": tool_use["id"], "content": tool_output}
                    )

                turn_history.append({"role": "user", "content": tool_result_blocks})
                _debug_print("anthropic:tool_results:appended", turn_history, enabled=settings_debug_enabled)


async def calcular_tokens_turno(
    *,
    user_message: str,
    history: List[Dict[str, Any]],
    settings: RuntimeSettings,
    schema_text: Optional[str] = None,
) -> int:
    """Estimate token usage for the next turn."""
    prompt_manager = PromptManager(history_limit=settings.history_limit)
    tool_registry = ToolRegistry()
    orchestrator = AgentOrchestrator(settings, prompt_manager, tool_registry)
    return await orchestrator.estimate_tokens(
        user_message=user_message,
        history=history,
        schema_text=schema_text,
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
