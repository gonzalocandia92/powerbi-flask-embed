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
from app.services.skill_router import (
    RouteDecision,
    SkillRouterSettings,
    build_skill_router_settings,
    resolve_skill_route,
    validate_dax_against_route,
)

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


def _normalize_schema_lookup_name(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    if text.startswith("[") and text.endswith("]") and len(text) > 2:
        text = text[1:-1].strip()
    return text.casefold()


def _dedupe_schema_rows(rows: List[SchemaEmbedding]) -> List[SchemaEmbedding]:
    seen = set()
    result: List[SchemaEmbedding] = []
    for row in rows:
        key = (row.item_type, _normalize_schema_lookup_name(row.item_name))
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _sort_schema_rows(
    rows: List[SchemaEmbedding],
    *,
    required_names: List[str],
    preferred_names: List[str],
) -> List[SchemaEmbedding]:
    required = {_normalize_schema_lookup_name(item) for item in required_names}
    preferred = {_normalize_schema_lookup_name(item) for item in preferred_names}

    def _rank(row: SchemaEmbedding) -> int:
        name = _normalize_schema_lookup_name(row.item_name)
        if name in required:
            return 0
        if name in preferred:
            return 1
        return 2

    return sorted(_dedupe_schema_rows(rows), key=_rank)


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
    skill_router_settings: SkillRouterSettings = field(default_factory=SkillRouterSettings)


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
        skill_router_settings=build_skill_router_settings(config),
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
    report_id: Optional[int] = None,
    table_context_limit: int = DEFAULT_TABLE_CONTEXT_LIMIT,
    measure_context_limit: int = DEFAULT_MEASURE_CONTEXT_LIMIT,
    required_schema_items: Optional[List[Dict[str, Any]]] = None,
    preferred_measures: Optional[List[str]] = None,
    preferred_tables: Optional[List[str]] = None,
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
                "required_schema_items": required_schema_items or [],
                "preferred_measures": preferred_measures or [],
                "preferred_tables": preferred_tables or [],
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

            resolved_required_items = required_schema_items or []
            required_table_names = [
                str(item.get("item_name") or "").strip()
                for item in resolved_required_items
                if str(item.get("item_type") or "").strip().lower() == "table"
            ]
            required_measure_names = [
                str(item.get("item_name") or "").strip()
                for item in resolved_required_items
                if str(item.get("item_type") or "").strip().lower() == "measure"
            ]

            def _base_schema_query(item_type: str):
                query = SchemaEmbedding.query.filter(
                    SchemaEmbedding.dataset_id == dataset_id,
                    SchemaEmbedding.item_type == item_type,
                )
                if report_id is not None:
                    query = query.filter(SchemaEmbedding.report_id_fk == report_id)
                return query

            def _find_required_rows(item_type: str, item_names: List[str]) -> List[SchemaEmbedding]:
                if not item_names:
                    return []
                wanted = {_normalize_schema_lookup_name(name) for name in item_names if str(name or "").strip()}
                if not wanted:
                    return []
                matched: List[SchemaEmbedding] = []
                for row in _base_schema_query(item_type).all():
                    if _normalize_schema_lookup_name(row.item_name) in wanted:
                        matched.append(row)
                found = {_normalize_schema_lookup_name(row.item_name) for row in matched}
                missing = sorted(wanted - found)
                if missing:
                    logging.warning(
                        "Route required schema items not found for dataset=%s type=%s missing=%s",
                        hash_identifier(dataset_id, prefix="dataset"),
                        item_type,
                        missing,
                    )
                return matched

            table_limit = _coerce_positive_int(table_context_limit, DEFAULT_TABLE_CONTEXT_LIMIT)
            measure_limit = _coerce_positive_int(measure_context_limit, DEFAULT_MEASURE_CONTEXT_LIMIT)
            required_table_results = _find_required_rows("table", required_table_names)
            required_measure_results = _find_required_rows("measure", required_measure_names)

            table_vector_limit = max(table_limit, table_limit + len(required_table_results))
            measure_vector_limit = max(measure_limit, measure_limit + len(required_measure_results))
            table_vector_results = (
                _base_schema_query("table")
                .order_by(SchemaEmbedding.embedding.cosine_distance(query_vector_list))
                .limit(table_vector_limit)
                .all()
            )
            measure_vector_results = (
                _base_schema_query("measure")
                .order_by(SchemaEmbedding.embedding.cosine_distance(query_vector_list))
                .limit(measure_vector_limit)
                .all()
            )
            table_results = _sort_schema_rows(
                required_table_results + table_vector_results,
                required_names=required_table_names,
                preferred_names=preferred_tables or [],
            )[:table_limit]
            measure_results = _sort_schema_rows(
                required_measure_results + measure_vector_results,
                required_names=required_measure_names,
                preferred_names=preferred_measures or [],
            )[:measure_limit]

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
                        "required_table_count": len(required_table_results),
                        "required_measure_count": len(required_measure_results),
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


def _render_route_context(route_decision: Optional[RouteDecision]) -> str:
    if route_decision is None or not route_decision.selected_skills:
        return ""
    lines = [
        "\nRUTA ANALITICA RESUELTA",
        f"Estrategia: {route_decision.strategy}",
        f"Confianza: {route_decision.confidence:.2f}",
    ]
    if route_decision.canonical_measures:
        lines.append("Metricas canonicas priorizadas:")
        lines.extend(f"- [{measure}]" for measure in route_decision.canonical_measures)
    if route_decision.required_schema_items:
        lines.append("Objetos del modelo requeridos:")
        for item in route_decision.required_schema_items:
            label = "Medida" if item.get("item_type") == "measure" else "Tabla"
            lines.append(f"- {label}: {item.get('item_name')}")
    if route_decision.constraints:
        lines.append("Restricciones:")
        lines.extend(f"- {constraint}" for constraint in route_decision.constraints)
    return "\n".join(lines) + "\n"


def _render_routed_skills(
    route_decision: Optional[RouteDecision],
    *,
    max_skill_chars: int,
) -> str:
    if route_decision is None or not route_decision.selected_skills:
        return ""
    budget = max(0, int(max_skill_chars or 0))
    if budget <= 0:
        return ""
    rendered: List[str] = ["\nSKILLS ANALITICAS SELECCIONADAS"]
    used_chars = 0
    for skill in route_decision.selected_skills:
        header = f"\n[{skill.skill_key}]\n"
        remaining = budget - used_chars - len(header)
        if remaining <= 0:
            break
        content = str(skill.content or "").strip()
        truncated = False
        if len(content) > remaining:
            content = content[: max(0, remaining - 80)].rstrip()
            truncated = True
        rendered.append(header + content)
        used_chars += len(header) + len(content)
        if truncated:
            rendered.append("\n[Contenido truncado por limite de contexto de skills.]")
            break
    return "\n".join(rendered).strip() + "\n"


def _build_system_prompt(
    schema_text: str,
    custom_instructions: Optional[List[Any]] = None,
    route_decision: Optional[RouteDecision] = None,
    skill_router_settings: Optional[SkillRouterSettings] = None,
) -> List[Dict[str, Any]]:
    schema_block = _minify_schema_text(schema_text)
    temporal_context_line = _build_temporal_context_line()
    custom_instruction_block = _render_custom_instructions(custom_instructions)
    max_skill_chars = (
        skill_router_settings.max_skill_chars
        if skill_router_settings is not None
        else SkillRouterSettings().max_skill_chars
    )
    route_block = _render_route_context(route_decision)
    skills_block = _render_routed_skills(route_decision, max_skill_chars=max_skill_chars)
    return [
        {
            "type": "text",
            "text": (
                """Tu nombre es Klara. Sos un asistente experto en analítica sobre reportes Power BI.

Tu función es responder usando el modelo semántico activo y las herramientas disponibles. ¡NUNCA inventes datos, métricas, fechas, relaciones ni conclusiones de negocio!

Los valores de negocio SOLO pueden provenir de consultas DAX ejecutadas correctamente durante el turno actual.

JERARQUÍA DE CONTEXTO

Respetá SIEMPRE este orden:

1. Reglas críticas de este prompt base.
2. Ruta analítica resuelta y skills seleccionadas.
3. Contexto semántico recuperado.
4. Resultados de herramientas.
5. Instrucciones del usuario.

Las skills seleccionadas definen métricas canónicas, objetos requeridos, restricciones y reglas de negocio. El contexto semántico confirma los identificadores técnicos reales. Los resultados de `execute_dax_query` son la ÚNICA evidencia válida para valores de negocio.

SEGURIDAD E INFORMACIÓN INTERNA

¡NUNCA expongas información interna!

No muestres IDs, credenciales, tokens, URLs internas, infraestructura, nombres técnicos de tablas, columnas, medidas, DAX, resultados crudos, errores internos, stack traces ni detalles de herramientas.

No atribuyas una falla al usuario, al reporte ni a los datos.

Si ocurre un error técnico irrecuperable o se agotan los intentos permitidos sin evidencia suficiente, responder ÚNICAMENTE:

“Disculpa, me encontré con un inconveniente técnico al procesar los datos. Por favor, intenta nuevamente más tarde.”

USO DE HERRAMIENTAS Y EVIDENCIA

Para cualquier pregunta cuantitativa, ranking, comparación, tendencia, período histórico, recomendación basada en datos o conclusión numérica, ejecutar SIEMPRE `execute_dax_query` antes de responder.

No ejecutar DAX para saludos, explicaciones generales, definiciones metodológicas o preguntas que no requieran datos del modelo.

Toda consulta DAX DEBE comenzar con `EVALUATE`.

¡NUNCA inventes tablas, columnas, medidas, relaciones, categorías, fechas ni valores!

Si falta un objeto técnico necesario, usar `get_schema_context` antes de generar DAX.

¡NUNCA presentes como validado un valor que no provenga de una consulta DAX exitosa del turno actual!

No estimes, extrapoles, completes valores faltantes ni reutilices resultados de turnos anteriores.

RUTA, SKILLS Y MEDIDAS

Respetá SIEMPRE las métricas canónicas, restricciones y companions de las skills seleccionadas.

Si una skill exige una medida, período, moneda, selector, dimensión o contrato de ejecución específico, esa condición es OBLIGATORIA.

¡NUNCA sustituyas una medida canónica por una medida parecida, una columna cruda, una suma manual o una fórmula alternativa sin una validación explícita del modelo!

Si existe una medida oficial aplicable, usarla SIEMPRE antes que cálculos manuales sobre columnas base.

Si una consulta devuelve `BLANK`, `null`, cero inesperado o tabla vacía, NO cambies inmediatamente de métrica. Diagnosticar primero período, filtros, moneda, selector, dimensión y relación.

FECHAS, MONEDA Y FILTROS

Resolver SIEMPRE referencias relativas como “este mes”, “este año”, “mes pasado”, “ayer” o “últimos días” usando el contexto temporal disponible.

Cuando exista una tabla calendario válida, aplicar filtros sobre ella y NO sobre tablas de hechos, salvo que una skill o el modelo indiquen explícitamente otra lógica.

¡NUNCA describas un resultado como MTD, YTD, mensual, anual, acumulado, histórico, en ARS o en USD si la consulta DAX no aplicó realmente ese contexto!

No comparar períodos parciales contra períodos completos.

Si el período está en curso, tratarlo SIEMPRE como acumulado a la fecha.

CONSTRUCCIÓN DAX

Usar SIEMPRE medidas antes que columnas base.

Para valores escalares simples, usar `ROW` únicamente si cada medida ya contiene o recibe mediante `CALCULATE` el contexto obligatorio.

¡NUNCA usar `ROW` como excusa para omitir filtros obligatorios de fecha, moneda, dimensión, selector o granularidad!

Para agrupaciones, usar `SUMMARIZECOLUMNS`.

Para rankings, usar `TOPN` sobre el resultado final que se presentará al usuario, salvo que sea necesario recuperar el universo completo para validar totales, participaciones o acumulados.

Usar `ADDCOLUMNS` únicamente cuando sea necesario calcular valores derivados por fila.

DAX NO es SQL:

* Usar `&&` y `||` para lógica booleana.
* Usar `ISBLANK()` o `BLANK()` para nulos.
* Usar `DIVIDE()` para divisiones.
* Usar `CONTAINSSTRING()` para texto parcial.
* Usar `SWITCH(TRUE(), ...)` para clasificaciones validadas.
* Usar siempre el formato `'Tabla'[Columna]`.

¡NUNCA crear joins manuales ni usar objetos no confirmados por el schema!

VALIDACIÓN Y MANEJO DE ERRORES

Antes de responder, validar SIEMPRE:

* Métrica correcta.
* Período correcto.
* Moneda o unidad correcta.
* Granularidad adecuada.
* Dimensión que filtre realmente la medida.
* Coherencia entre total, ranking, porcentaje y desglose.

Si una consulta falla por sintaxis, corregir ÚNICAMENTE la causa identificada.

Si falla por una tabla, columna, medida o relación inexistente, recuperar schema antes de volver a intentar.

¡NUNCA responder con cifras, estimaciones o conclusiones específicas después de una consulta fallida!

INTERPRETACIÓN

Diferenciar SIEMPRE entre:

Dato confirmado: resultado validado mediante DAX.

Interpretación: lectura directamente respaldada por los datos.

Hipótesis a validar: explicación posible que no puede confirmarse con la evidencia disponible.

¡NUNCA presentar hipótesis como hechos!

No atribuir causalidad, fraude, eficiencia, demanda, estacionalidad, errores de registración, problemas operativos o decisiones comerciales sin evidencia directa.
"""
                f"{temporal_context_line}"
                f"{custom_instruction_block}"
                f"{route_block}"
                f"{skills_block}"
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

    def get_system_prompt(
        self,
        schema_text: str,
        custom_instructions: Optional[List[Any]] = None,
        route_decision: Optional[RouteDecision] = None,
        skill_router_settings: Optional[SkillRouterSettings] = None,
    ) -> List[Dict[str, Any]]:
        return _build_system_prompt(
            schema_text,
            custom_instructions=custom_instructions,
            route_decision=route_decision,
            skill_router_settings=skill_router_settings,
        )


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
            apply_route_context = bool(tool_input.get("apply_route_context"))
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
                report_id=context.get("report_id"),
                table_context_limit=context.get("schema_table_context_limit", DEFAULT_TABLE_CONTEXT_LIMIT),
                measure_context_limit=context.get("schema_measure_context_limit", DEFAULT_MEASURE_CONTEXT_LIMIT),
                required_schema_items=(
                    context.get("route_required_schema_items") if apply_route_context else None
                ),
                preferred_measures=(
                    context.get("route_preferred_measures") if apply_route_context else None
                ),
                preferred_tables=(
                    context.get("route_preferred_tables") if apply_route_context else None
                ),
                usage_totals=context.get("usage_totals"),
                ai_usage_events=context.get("ai_usage_events"),
            )

        if tool_name == "execute_dax_query":
            route_decision = context.get("route_decision")
            validation = validate_dax_against_route(tool_input.get("dax_query", ""), route_decision)
            if not validation.validation_skipped:
                context.setdefault("route_validation_warnings", []).extend(validation.warnings)
                _debug_print(
                    "tool:execute_dax_query:route_validation",
                    validation.to_metadata(),
                    enabled=bool(context.get("debug_enabled", True)),
                )
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
        empresa_id: Optional[int],
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
            "empresa_id": empresa_id,
            "user_message": user_message,
            "debug_enabled": debug_enabled,
            "custom_instructions": custom_instructions or [],
            "schema_retrieval_prompt": str(schema_retrieval_prompt or "").strip(),
            "schema_table_context_limit": _coerce_positive_int(schema_table_context_limit, DEFAULT_TABLE_CONTEXT_LIMIT),
            "schema_measure_context_limit": _coerce_positive_int(schema_measure_context_limit, DEFAULT_MEASURE_CONTEXT_LIMIT),
            "usage_totals": _new_usage_totals(),
            "ai_usage_events": [],
            "route_decision": None,
            "route_required_schema_items": [],
            "route_preferred_measures": [],
            "route_preferred_tables": [],
            "route_validation_warnings": [],
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
        empresa_id: Optional[int] = None,
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
            empresa_id=empresa_id,
            user_message=user_message,
            debug_enabled=settings_debug_enabled,
            custom_instructions=custom_instructions,
            schema_retrieval_prompt=schema_retrieval_prompt,
            schema_table_context_limit=schema_table_context_limit,
            schema_measure_context_limit=schema_measure_context_limit,
        )

        route_decision: Optional[RouteDecision] = None
        router_settings = self.settings.skill_router_settings
        if router_settings.enabled and report_id is not None:
            route_decision = await resolve_skill_route(
                user_message=user_message,
                report_id=int(report_id),
                empresa_id=empresa_id,
                dataset_id=dataset_id,
                settings=router_settings,
                usage_totals=context.get("usage_totals"),
                ai_usage_events=context.get("ai_usage_events"),
            )
            context["route_decision"] = route_decision
            if route_decision is not None:
                _append_ai_usage_event(
                    context.get("ai_usage_events"),
                    provider="voyageai",
                    model=VOYAGE_QUERY_EMBEDDING_MODEL,
                    event_type="embedding",
                    source_type="skill_router_embedding",
                    trigger_type="user_request",
                    operation_name="resolve-skill-route",
                    status="success" if route_decision.strategy not in {"router_error", "fallback"} else "error",
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    metadata_json={
                        **route_decision.to_metadata(),
                        "router_mode": router_settings.mode,
                    },
                )
                _debug_print(
                    "skill_router:decision",
                    route_decision.to_metadata(),
                    enabled=settings_debug_enabled,
                )

        apply_route_context = (
            router_settings.enabled
            and router_settings.mode == "active"
            and route_decision is not None
            and route_decision.strategy in {"soft_route", "hard_route"}
            and bool(route_decision.selected_skills)
        )
        if apply_route_context and route_decision is not None:
            context["route_required_schema_items"] = route_decision.required_schema_items
            context["route_preferred_measures"] = route_decision.canonical_measures
            context["route_preferred_tables"] = route_decision.preferred_tables

        initial_failure_reason: Optional[str] = None
        initial_error_message: Optional[str] = None
        if not schema_text:
            # We call the tool directly to ensure only one rewrite happens.
            # execute_tool will handle the rewrite if question_is_rewritten is not passed.
            try:
                fetched_schema_text = await self.tool_registry.execute_tool(
                    "get_schema_context",
                    {
                        "question": user_message,
                        "apply_route_context": apply_route_context,
                    }, # No question_is_rewritten passed -> execute_tool will rewrite it
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

        prompt_route_decision = route_decision if apply_route_context else None
        system_prompt = self.prompt_manager.get_system_prompt(
            schema_text,
            custom_instructions=custom_instructions,
            route_decision=prompt_route_decision,
            skill_router_settings=router_settings,
        )
        tools: Any = self.tool_registry.get_all_tools()
        _debug_print(
            "anthropic:request:prepared",
            {"system_prompt": system_prompt, "messages": turn_history, "tools": tools},
            enabled=settings_debug_enabled,
        )

        try:
            token_count = await self.estimate_request_tokens_payload(
                system_prompt=system_prompt,
                messages=cast(List[Dict[str, Any]], turn_history),
                tools=tools,
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
                "route_metadata_json": route_decision.to_metadata() if route_decision is not None else None,
                "route_validation_warnings": list(context.get("route_validation_warnings") or []),
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
    empresa_id: Optional[int] = None,
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
            empresa_id=empresa_id,
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

