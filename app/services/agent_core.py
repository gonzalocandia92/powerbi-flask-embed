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
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, cast
from zoneinfo import ZoneInfo

from .powerbi_tools import execute_dax_query_local, get_tables_and_measures_description
from .schema_rerank import build_schema_context_json

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_HISTORY_LIMIT = 10
DEFAULT_MAX_TOOL_ROUNDS = 6
DEFAULT_DEBUG_ENABLED = True
DEFAULT_PROMPT_CACHING_ENABLED = True
DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS = 20
DEFAULT_SCHEMA_CACHE_TTL_SECONDS = 3600
DEFAULT_SCHEMA_CACHE_MAX_ENTRIES = 100

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
_SCHEMA_CACHE_LOCK = threading.RLock()
_SCHEMA_CACHE: Dict[str, Dict[str, Any]] = {}


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


def _parse_int_env(name: str, default: int, minimum: int = 0) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        return max(minimum, int(str(raw_value).strip()))
    except Exception:
        return default


def _get_schema_cache_ttl_seconds() -> int:
    return _parse_int_env(
        "CHAT_SCHEMA_CACHE_TTL_SECONDS",
        DEFAULT_SCHEMA_CACHE_TTL_SECONDS,
        minimum=0,
    )


def _get_schema_cache_max_entries() -> int:
    return _parse_int_env(
        "CHAT_SCHEMA_CACHE_MAX_ENTRIES",
        DEFAULT_SCHEMA_CACHE_MAX_ENTRIES,
        minimum=1,
    )


def _build_schema_cache_key(dataset_id: str, powerbi_credentials: Dict[str, Any]) -> str:
    tenant_id = str(powerbi_credentials.get("TENANT_ID") or "").strip()
    dataset_key = str(dataset_id).strip()
    if tenant_id:
        return f"{tenant_id}:{dataset_key}"
    return dataset_key


def _get_schema_cache_entry(cache_key: str) -> Optional[Dict[str, Any]]:
    with _SCHEMA_CACHE_LOCK:
        entry = _SCHEMA_CACHE.get(cache_key)
        if entry is None:
            return None
        return dict(entry)


def _get_cached_schema_items(cache_key: str, ttl_seconds: int) -> Optional[List[str]]:
    if ttl_seconds <= 0:
        return None

    entry = _get_schema_cache_entry(cache_key)
    if entry is None:
        return None

    fetched_at = float(entry.get("fetched_at", 0))
    if (time.time() - fetched_at) > ttl_seconds:
        return None

    schema_items = entry.get("schema_items") or []
    return [str(item) for item in schema_items]


def _get_stale_schema_items(cache_key: str) -> Optional[List[str]]:
    entry = _get_schema_cache_entry(cache_key)
    if entry is None:
        return None
    schema_items = entry.get("schema_items") or []
    if not schema_items:
        return None
    return [str(item) for item in schema_items]


def _store_schema_cache_entry(cache_key: str, schema_items: List[str]) -> None:
    now = time.time()
    normalized_items = [str(item) for item in schema_items if str(item).strip()]
    max_entries = _get_schema_cache_max_entries()

    with _SCHEMA_CACHE_LOCK:
        _SCHEMA_CACHE[cache_key] = {
            "fetched_at": now,
            "schema_items": normalized_items,
        }
        if len(_SCHEMA_CACHE) <= max_entries:
            return

        oldest_key = min(
            _SCHEMA_CACHE,
            key=lambda key: float(_SCHEMA_CACHE[key].get("fetched_at", 0)),
        )
        _SCHEMA_CACHE.pop(oldest_key, None)

async def _rewrite_query_for_reranker(
    *,
    user_message: str,
    settings: RuntimeSettings,
    debug_enabled: bool,
) -> str:
    """Micro-agent that translates a user question into technical keywords."""
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
            optimized_query = response.content[0].text.strip()
            _debug_print("agent:rewriter:success", {"optimized_query": optimized_query}, enabled=debug_enabled)
            return optimized_query or user_message
    except Exception as exc:
        logging.warning("The query rewriter failed. Using original query: %s", exc)
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
) -> str:
    cache_key = _build_schema_cache_key(dataset_id, powerbi_credentials)
    cache_ttl_seconds = _get_schema_cache_ttl_seconds()

    _debug_print(
        "tool:get_schema_context:request",
        {
            "dataset_id": dataset_id,
            "question": question,
            "cache_key": cache_key,
            "cache_ttl_seconds": cache_ttl_seconds,
        },
        enabled=debug_enabled,
    )
    try:
        esquema_dinamico = _get_cached_schema_items(cache_key, cache_ttl_seconds)
        if esquema_dinamico is not None:
            _debug_print(
                "tool:get_schema_context:cache_hit",
                {"cache_key": cache_key, "items": len(esquema_dinamico)},
                enabled=debug_enabled,
            )
        else:
            _debug_print(
                "tool:get_schema_context:cache_miss",
                {"cache_key": cache_key},
                enabled=debug_enabled,
            )
            try:
                esquema_dinamico = await asyncio.to_thread(
                    get_tables_and_measures_description,
                    dataset_id,
                    powerbi_credentials,
                )
                _store_schema_cache_entry(cache_key, esquema_dinamico)
                _debug_print(
                    "tool:get_schema_context:cache_store",
                    {"cache_key": cache_key, "items": len(esquema_dinamico)},
                    enabled=debug_enabled,
                )
            except Exception as exc:
                esquema_dinamico = _get_stale_schema_items(cache_key)
                if esquema_dinamico is None:
                    raise
                logging.warning(
                    "Failed to refresh schema for %s; using stale cached schema: %s",
                    cache_key,
                    exc,
                )
                _debug_print(
                    "tool:get_schema_context:stale_cache_fallback",
                    {"cache_key": cache_key, "items": len(esquema_dinamico), "error": repr(exc)},
                    enabled=debug_enabled,
                )

        fetched_schema_text = await asyncio.wait_for(
            asyncio.to_thread(build_schema_context_json, question, esquema_dinamico, 3, 5),
            timeout=max(1, int(settings.schema_context_timeout_seconds)),
        )
        _debug_print("tool:get_schema_context:response", fetched_schema_text, enabled=debug_enabled)
        return fetched_schema_text.strip()
    except asyncio.TimeoutError:
        logging.warning(
            "Timed out fetching reranked schema context locally after %s seconds",
            settings.schema_context_timeout_seconds,
        )
        _debug_print(
            "tool:get_schema_context:timeout",
            {"seconds": settings.schema_context_timeout_seconds},
            enabled=debug_enabled,
        )
        return ""
    except Exception as exc:
        logging.exception("Failed to fetch reranked schema context locally")
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
                "Eres un asistente experto de analítica para Power BI. Reglas estrictas de seguridad, sintaxis y optimización DAX:\n"
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
    ) -> str:
        return await _rewrite_query_for_reranker(
            user_message=user_message,
            settings=settings,
            debug_enabled=debug_enabled,
        )

    async def _execute_dax_query_local_async(self, tool_input: Dict[str, Any], context: Dict[str, Any]) -> str:
        dataset_id = context.get("dataset_id")
        if not dataset_id:
            raise RuntimeError("dataset_id is required to execute DAX queries")

        dax_query = tool_input.get("dax_query")
        if not dax_query or not str(dax_query).strip():
            raise RuntimeError("Tool execute_dax_query requires a non-empty dax_query")

        powerbi_credentials = context.get("powerbi_credentials") or {}
        return await asyncio.to_thread(
            execute_dax_query_local,
            dataset_id,
            str(dax_query),
            powerbi_credentials,
        )

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

            optimized_question = await self.rewrite_query_for_reranker(
                user_message=str(question),
                settings=settings,
                debug_enabled=debug_enabled,
            )
            return await _fetch_schema_context(
                dataset_id=dataset_id,
                powerbi_credentials=powerbi_credentials,
                question=optimized_question,
                settings=settings,
                debug_enabled=debug_enabled,
                required=False,
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

        search_query = user_message
        if not schema_text:
            search_query = await self.tool_registry.rewrite_query_for_reranker(
                user_message=user_message,
                settings=self.settings,
                debug_enabled=settings_debug_enabled,
            )

        fetched_schema_text = await self.tool_registry.execute_tool(
            "get_schema_context",
            {"question": search_query},
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
        output_tokens: Optional[int] = None
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
                if usage is not None:
                    output_tokens = getattr(usage, "output_tokens", output_tokens)
                    if output_tokens is None and isinstance(usage, dict):
                        output_tokens = usage.get("output_tokens", output_tokens)

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
                        "input_tokens": token_count,
                        "output_tokens": output_tokens,
                        "model": self.settings.anthropic_model,
                        "mcp_used": bool(tools_called),
                        "tools_called": tools_called,
                        "dax_query": dax_query_used,
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
    return await orchestrator.generate_response(
        user_message=user_message,
        history=history,
        dataset_id=dataset_id,
        powerbi_credentials=powerbi_credentials,
        schema_text=schema_text,
        conversation_id=conversation_id,
        report_id=report_id,
    )
