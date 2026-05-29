"""
Chat orchestration for secure MCP-backed DAX analysis.

This module keeps the LLM isolated from tenant-routing data:
- The model sees execute_dax_query(dax_query) and can request reranked schema context via get_schema_context(question).
- The backend resolves dataset_id and injects it when calling MCP.
- Conversation history is stored server-side per user/report/conversation.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import shlex
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, cast

from .schema_rerank import build_schema_context_json

ANTHROPIC_API_KEY=os.environ.get("ANTHROPIC_API_KEY")

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_HISTORY_LIMIT = 10
DEFAULT_MAX_TOOL_ROUNDS = 6
DEFAULT_DEBUG_ENABLED = False
DEFAULT_PROMPT_CACHING_ENABLED = True
DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS = 20

DATE_TABLE_GUIDANCE = (
    '"Date": ["Date (Date)", "Anio (Integer)", "Mes (Integer)", "Day (Integer)", '
    '"SemanaAnio (Integer)", "Semana Inicio y Fin (Text)", "Semana Inicio y Fin Resumido (Text)", '
    '"AnioMes (Integer)", "AnioSemana (Integer)", "SemanaAnioID (Integer)", "fecha2 (Date)", '
    '"FechaMaxDolar (Integer)", "weekday (Text)", "weekday_nro (Integer)", "Trimestre (Integer)", '
    '"AnioTrimestre (Integer)", "AnioMesTexto (Text)"'
)

MONETARY_FILTER_GUIDANCE = """
- FILTRO DE MONEDA OBLIGATORIO: Medidas como [Ticket Promedio] devolverán BLANK/NULL si omites la moneda. 
Por defecto, SIEMPRE inyecta 'Moneda'[Moneda Campos] = "'Medidas'[Ventas ARS]" dentro de tus CALCULATE. 
Usa 'Moneda'[Moneda Campos] = "'Medidas'[Ventas USD]" SOLO si el usuario pide explícitamente dólares (USD).
"""

_CHAT_STATE_LOCK = threading.RLock()
_CHAT_STATES: dict[str, "ChatConversationState"] = {}
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEBUG_FILE_LOCK = threading.RLock()
_DEBUG_FILE_DEFAULT = _PROJECT_ROOT / "chat_mcp_debug.txt"


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
    """Print debug info in a readable way for chat orchestration tracing."""
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
    message = f"\n{timestamp} [chat_mcp] {label}\n{rendered}\n"
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
        # No dejamos que falle la ejecución por un problema de escritura de debug.
        logging.exception("Failed to write chat debug output to file: %s", debug_file)
        print(f"[chat_mcp] debug-file-error: {exc}", flush=True)


@dataclass
class ChatConversationState:
    """Server-side chat state for one user/report conversation."""

    conversation_id: str
    user_key: str
    report_id: int
    messages: List[Dict[str, Any]] = field(default_factory=list)
    schema_text: str = ""
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RuntimeSettings:
    """Configuration for Anthropic + MCP runtime."""

    anthropic_api_key: str = ANTHROPIC_API_KEY
    anthropic_model: str = DEFAULT_MODEL
    anthropic_max_tokens: int = DEFAULT_MAX_TOKENS
    history_limit: int = DEFAULT_HISTORY_LIMIT
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS
    debug_enabled: bool = DEFAULT_DEBUG_ENABLED
    prompt_caching_enabled: bool = DEFAULT_PROMPT_CACHING_ENABLED
    schema_context_timeout_seconds: int = DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS
    mcp_command: Optional[str] = None
    mcp_args: List[str] = field(default_factory=list)
    mcp_cwd: Optional[str] = None
    mcp_env: Dict[str, str] = field(default_factory=dict)


class MCPBridge:
    """Async context manager that opens an MCP stdio connection and exposes tools."""

    def __init__(self, command: str, args: Iterable[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None):
        self.command = command
        self.args = list(args)
        self.cwd = cwd
        self.env = env or {}
        self._stdio_cm = None
        self._session = None
        self._stdio_read = None
        self._stdio_write = None
        self._ClientSession: Any = None
        self._StdioServerParameters: Any = None
        self._stdio_client: Any = None

    async def __aenter__(self) -> "MCPBridge":
        try:
            mcp_runtime = _import_external_mcp_runtime()
            ClientSession = getattr(mcp_runtime, "ClientSession", None)
            StdioServerParameters = getattr(mcp_runtime, "StdioServerParameters", None)
            stdio_client = getattr(mcp_runtime, "stdio_client", None)

            if ClientSession is None or StdioServerParameters is None or stdio_client is None:
                from mcp.client.session import ClientSession  # type: ignore
                from mcp.client.stdio import StdioServerParameters, stdio_client  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency missing in test env
            raise RuntimeError(
                "The 'mcp' package is required to use the chat endpoint. Install it in production."
            ) from exc

        self._ClientSession = ClientSession
        self._StdioServerParameters = StdioServerParameters
        self._stdio_client = stdio_client

        client_session_cls = cast(Any, self._ClientSession)
        stdio_client_fn = cast(Any, self._stdio_client)
        server_params = self._build_server_params()
        self._stdio_cm = stdio_client_fn(server_params)
        self._stdio_read, self._stdio_write = await self._stdio_cm.__aenter__()
        self._session = client_session_cls(self._stdio_read, self._stdio_write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self

    def _build_server_params(self):
        """Create StdioServerParameters with best-effort compatibility."""
        server_params_cls = cast(Any, self._StdioServerParameters)
        attempts = [
            {"command": self.command, "args": self.args, "cwd": self.cwd, "env": self.env or None},
            {"command": self.command, "args": self.args, "env": self.env or None},
            {"command": self.command, "args": self.args},
        ]
        last_error = None
        for kwargs in attempts:
            try:
                return server_params_cls(**{k: v for k, v in kwargs.items() if v is not None})
            except TypeError as exc:
                last_error = exc
        raise last_error  # pragma: no cover - only reached if SDK signature is unexpected

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self._session is not None:
                try:
                    await self._session.__aexit__(exc_type, exc, tb)
                except Exception as cleanup_exc:
                    logging.warning("MCP session cleanup failed: %s", cleanup_exc)
                self._session = None
        finally:
            if self._stdio_cm is not None:
                try:
                    await self._stdio_cm.__aexit__(exc_type, exc, tb)
                except Exception as cleanup_exc:
                    logging.warning("MCP stdio cleanup failed: %s", cleanup_exc)
                self._stdio_cm = None

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError("MCP session is not initialized")
        try:
            return await self._session.call_tool(name, arguments=arguments)
        except TypeError:
            return await self._session.call_tool(name, arguments)

    async def get_semantic_model_schema(self, dataset_id: str) -> str:
        result = await self.call_tool("get_semantic_model_schema", {"dataset_id": dataset_id})
        return _mcp_result_to_text(result)

    async def execute_dax_query(self, dataset_id: str, dax_query: str) -> str:
        result = await self.call_tool("execute_dax_query", {"dataset_id": dataset_id, "dax_query": dax_query})
        return _mcp_result_to_text(result)


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
    debug_enabled_raw = _cfg("CHAT_DEBUG_ENABLED", "false") or "false"
    prompt_caching_enabled_raw = _cfg("ANTHROPIC_PROMPT_CACHING_ENABLED", _cfg("CHAT_PROMPT_CACHING_ENABLED", "true")) or "true"
    schema_context_timeout_raw = _cfg("CHAT_SCHEMA_CONTEXT_TIMEOUT_SECONDS", str(DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS)) or str(DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS)

    mcp_command = _cfg("MCP_SERVER_COMMAND")
    mcp_args_raw = _cfg("MCP_SERVER_ARGS", "") or ""
    mcp_cwd = _cfg("MCP_SERVER_CWD")
    mcp_env_raw = _cfg("MCP_SERVER_ENV_JSON", "") or ""

    mcp_args = shlex.split(mcp_args_raw) if mcp_args_raw.strip() else []
    mcp_env: Dict[str, str] = {}
    if mcp_env_raw.strip():
        parsed_env = json.loads(mcp_env_raw)
        if not isinstance(parsed_env, dict):
            raise RuntimeError("MCP_SERVER_ENV_JSON must be a JSON object")
        mcp_env = {str(k): str(v) for k, v in parsed_env.items()}

    return RuntimeSettings(
        anthropic_api_key=str(anthropic_api_key),
        anthropic_model=str(model),
        anthropic_max_tokens=int(max_tokens_raw),
        history_limit=int(history_limit_raw),
        max_tool_rounds=int(max_tool_rounds_raw),
        debug_enabled=_parse_bool(debug_enabled_raw, default=DEFAULT_DEBUG_ENABLED),
        prompt_caching_enabled=_parse_bool(prompt_caching_enabled_raw, default=DEFAULT_PROMPT_CACHING_ENABLED),
        schema_context_timeout_seconds=int(schema_context_timeout_raw),
        mcp_command=str(mcp_command) if mcp_command else None,
        mcp_args=mcp_args,
        mcp_cwd=str(mcp_cwd) if mcp_cwd else None,
        mcp_env=mcp_env,
    )


def _debug_enabled() -> bool:
    """Best-effort global debug switch for prints/file logs."""
    return _parse_bool(os.getenv("CHAT_DEBUG_ENABLED"), default=DEFAULT_DEBUG_ENABLED)


def get_conversation_state(user_key: str, report_id: int, conversation_id: Optional[str] = None, reset: bool = False) -> ChatConversationState:
    """Get or create a conversation state isolated by user + report."""
    normalized_conversation_id = conversation_id or str(uuid.uuid4())
    state_key = _conversation_key(user_key, report_id, normalized_conversation_id)
    with _CHAT_STATE_LOCK:
        if reset:
            _CHAT_STATES.pop(state_key, None)
        state = _CHAT_STATES.get(state_key)
        if state is None:
            state = ChatConversationState(
                conversation_id=normalized_conversation_id,
                user_key=user_key,
                report_id=report_id,
            )
            _CHAT_STATES[state_key] = state
        return state


def clear_conversation_state(user_key: str, report_id: int, conversation_id: str) -> None:
    """Remove a stored conversation state."""
    with _CHAT_STATE_LOCK:
        _CHAT_STATES.pop(_conversation_key(user_key, report_id, conversation_id), None)


async def _rewrite_query_for_reranker(
    *,
    user_message: str,
    settings: RuntimeSettings,
    debug_enabled: bool,
) -> str:
    """
    Micro-agente que traduce la pregunta del usuario a palabras clave tecnicas
    para maximizar la precision del Reranker.
    """
    try:
        from anthropic import AsyncAnthropic  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependency missing in test env
        logging.warning("El paquete 'anthropic' no esta disponible para reescritura")
        return user_message

    system_prompt = (
        "Eres un experto en bases de datos y Power BI. Tu unica tarea es extraer y deducir "
        "los terminos tecnicos mas probables de la pregunta del usuario. "
        "Reglas: Devuelve SOLO una lista de 5 a 8 palabras clave separadas por comas. "
        "No incluyas saludos, explicaciones ni vinietas."
        "IMPORTANTE: Si la pregunta involucra ventas, facturación o tickets, INCLUYE SIEMPRE la palabra 'sales_order'. "
        "Si involucra tiempo, incluye 'Date'."
    )

    _debug_print(
        "micro_agent:rewriter:start",
        {"original_query": user_message},
        enabled=debug_enabled,
    )

    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            temperature=0.0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        optimized_query = response.content[0].text.strip()
        _debug_print(
            "micro_agent:rewriter:success",
            {"optimized_query": optimized_query},
            enabled=debug_enabled,
        )
        return optimized_query or user_message
    except Exception as exc:
        logging.warning("El Micro-Agente reescritor fallo. Usando query original: %s", exc)
        _debug_print(
            "micro_agent:rewriter:error",
            {"error": repr(exc)},
            enabled=debug_enabled,
        )
        return user_message


async def _fetch_schema_context(
    *,
    bridge: MCPBridge,
    dataset_id: str,
    question: str,
    settings: RuntimeSettings,
    debug_enabled: bool,
    required: bool,
) -> str:
    _debug_print(
        "mcp:get_schema_context:request",
        {"dataset_id": dataset_id, "question": question},
        enabled=debug_enabled,
    )
    try:
        fetched_schema_text = await asyncio.wait_for(
            asyncio.to_thread(build_schema_context_json, question, 3, 5),
            timeout=max(1, int(settings.schema_context_timeout_seconds)),
        )
        _debug_print("mcp:get_schema_context:response", fetched_schema_text, enabled=debug_enabled)
        return fetched_schema_text.strip()
    except asyncio.TimeoutError as exc:
        logging.warning(
            "Timed out fetching reranked schema context locally after %s seconds",
            settings.schema_context_timeout_seconds,
        )
        _debug_print("mcp:get_schema_context:timeout", {"seconds": settings.schema_context_timeout_seconds}, enabled=debug_enabled)
        return ""
    except Exception as exc:
        logging.exception("Failed to fetch reranked schema context locally")
        _debug_print("mcp:get_schema_context:error", repr(exc), enabled=debug_enabled)
        return ""


async def run_chat_turn(
    *,
    user_message: str,
    dataset_id: str,
    state: ChatConversationState,
    settings: RuntimeSettings,
    schema_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute one user turn against Anthropic + MCP and persist the resulting history."""
    try:
        from anthropic import AsyncAnthropic  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependency missing in test env
        raise RuntimeError(
            "The 'anthropic' package is required to use the chat endpoint. Install it in production."
        ) from exc

    if not settings.mcp_command:
        raise RuntimeError("MCP_SERVER_COMMAND is required to connect to the MCP server")

    schema_text = schema_text or state.schema_text or ""
    settings_debug_enabled = settings.debug_enabled or _debug_enabled()

    if settings_debug_enabled:
        _debug_print(
            "run_chat_turn:start",
            {
                "conversation_id": state.conversation_id,
                "user_key": state.user_key,
                "report_id": state.report_id,
                "dataset_id": dataset_id,
                "user_message": user_message,
                "settings": {
                    "anthropic_model": settings.anthropic_model,
                    "anthropic_max_tokens": settings.anthropic_max_tokens,
                    "history_limit": settings.history_limit,
                    "max_tool_rounds": settings.max_tool_rounds,
                    "debug_enabled": settings.debug_enabled,
                    "mcp_command": settings.mcp_command,
                    "mcp_args": settings.mcp_args,
                    "mcp_cwd": settings.mcp_cwd,
                    "mcp_env_keys": sorted(settings.mcp_env.keys()),
                },
                "existing_history_count": len(state.messages),
                "schema_loaded": bool(schema_text),
            },
            enabled=settings_debug_enabled,
        )

    async with MCPBridge(
        command=settings.mcp_command,
        args=settings.mcp_args,
        cwd=settings.mcp_cwd,
        env=settings.mcp_env,
    ) as bridge:
        search_query = user_message
        if not schema_text:
            search_query = await _rewrite_query_for_reranker(
                user_message=user_message,
                settings=settings,
                debug_enabled=settings_debug_enabled,
            )

        fetched_schema_text = await _fetch_schema_context(
            bridge=bridge,
            dataset_id=dataset_id,
            question=search_query,
            settings=settings,
            debug_enabled=settings_debug_enabled,
            required=not bool(schema_text),
        )
        if fetched_schema_text:
            schema_text = fetched_schema_text

        state.schema_text = schema_text
        system_prompt = _build_system_prompt(schema_text)
        history = _build_turn_history(state.messages, user_message)

        tools: Any = [_schema_context_tool_spec(), _anthropic_tool_spec()]
        _debug_print(
            "anthropic:request:prepared",
            {
                "system_prompt": system_prompt,
                "messages": history,
                "tools": tools,
            },
            enabled=settings_debug_enabled,
        )

        token_count = await calcular_tokens_turno(
            user_message=user_message,
            state=state,
            settings=settings,
            schema_text=schema_text,
        )
        _debug_print(
            "anthropic:token_count",
            {
                "tokens": token_count,
                "conversation_id": state.conversation_id,
                "report_id": state.report_id,
            },
            enabled=settings_debug_enabled,
        )

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        tool_rounds = 0
        messages = cast(Any, history)
        system_payload = cast(Any, system_prompt)
        anthropic_messages = client.messages

        while True:
            _debug_print(
                "anthropic:request:send",
                {
                    "model": settings.anthropic_model,
                    "max_tokens": settings.anthropic_max_tokens,
                    "system": system_prompt,
                    "messages": messages,
                    "tools": tools,
                    "tool_rounds": tool_rounds,
                },
                enabled=settings_debug_enabled,
            )
            response = await anthropic_messages.create(
                model=settings.anthropic_model,
                max_tokens=settings.anthropic_max_tokens,
                system=system_payload,
                messages=messages,
                tools=tools,
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
            history.append({"role": "assistant", "content": assistant_blocks})

            tool_uses = [block for block in assistant_blocks if block.get("type") == "tool_use"]
            _debug_print("anthropic:response:tool_uses", tool_uses, enabled=settings_debug_enabled)
            if not tool_uses:
                answer = _text_from_blocks(assistant_blocks)
                state.messages = _trim_history(history, settings.history_limit)
                state.updated_at = datetime.now(timezone.utc)
                _debug_print(
                    "anthropic:final",
                    {
                        "answer": answer,
                        "conversation_id": state.conversation_id,
                        "report_id": state.report_id,
                        "tool_rounds": tool_rounds,
                        "token_count": token_count,
                        "stored_history": state.messages,
                    },
                    enabled=settings_debug_enabled,
                )
                return {
                    "answer": answer,
                    "conversation_id": state.conversation_id,
                    "report_id": state.report_id,
                    "tool_rounds": tool_rounds,
                    "input_tokens": token_count,
                }

            tool_result_blocks: List[Dict[str, Any]] = []
            for tool_use in tool_uses:
                tool_name = tool_use.get("name")

                if tool_name == "get_schema_context":
                    tool_input = tool_use.get("input") or {}
                    raw_question = tool_input.get("question") or user_message
                    if not str(raw_question).strip():
                        raise RuntimeError("Tool get_schema_context requires a non-empty question")

                    tool_rounds += 1
                    if tool_rounds > settings.max_tool_rounds:
                        raise RuntimeError("Maximum tool round limit reached")

                    optimized_tool_query = await _rewrite_query_for_reranker(
                        user_message=str(raw_question),
                        settings=settings,
                        debug_enabled=settings_debug_enabled,
                    )

                    _debug_print(
                        "mcp:get_schema_context:tool_request",
                        {
                            "tool_use_id": tool_use.get("id"),
                            "dataset_id": dataset_id,
                            "question": str(raw_question),
                            "optimized_question": optimized_tool_query,
                            "tool_round": tool_rounds,
                        },
                        enabled=settings_debug_enabled,
                    )
                    tool_output = await _fetch_schema_context(
                        bridge=bridge,
                        dataset_id=dataset_id,
                        question=optimized_tool_query,
                        settings=settings,
                        debug_enabled=settings_debug_enabled,
                        required=False,
                    )
                    _debug_print(
                        "mcp:get_schema_context:tool_response",
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
                            "content": tool_output,
                        }
                    )
                    continue

                if tool_name != "execute_dax_query":
                    raise RuntimeError(f"Unsupported tool requested by the model: {tool_name}")

                tool_input = tool_use.get("input") or {}
                dax_query = tool_input.get("dax_query")
                if not dax_query or not str(dax_query).strip():
                    #raise RuntimeError("Tool execute_dax_query requires a non-empty dax_query")
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use["id"],
                        "content": "Error interno: El dax_query llegó vacío. Probablemente te quedaste sin tokens o la sintaxis JSON falló. Por favor, sé más conciso."
                    })
                    continue

                tool_rounds += 1
                if tool_rounds > settings.max_tool_rounds:
                    raise RuntimeError("Maximum tool round limit reached")

                _debug_print(
                    "mcp:execute_dax_query:request",
                    {
                        "tool_use_id": tool_use.get("id"),
                        "dataset_id": dataset_id,
                        "dax_query": str(dax_query),
                        "tool_round": tool_rounds,
                    },
                    enabled=settings_debug_enabled,
                )
                tool_output = await bridge.execute_dax_query(dataset_id=dataset_id, dax_query=str(dax_query))
                _debug_print(
                    "mcp:execute_dax_query:response",
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
                        "content": tool_output,
                    }
                )

            history.append({"role": "user", "content": tool_result_blocks})
            _debug_print("anthropic:tool_results:appended", history, enabled=settings_debug_enabled)


async def calcular_tokens_turno(
    *,
    user_message: str,
    state: ChatConversationState,
    settings: RuntimeSettings,
    schema_text: Optional[str] = None,
) -> int:
    """
    Calcula los tokens estimados para el próximo turno, incluyendo historial,
    system prompt dinámico y herramientas.
    """
    try:
        from anthropic import AsyncAnthropic  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependency missing in test env
        raise RuntimeError("El paquete 'anthropic' es requerido.") from exc

    schema_actual = schema_text or state.schema_text or ""
    system_prompt = _build_system_prompt(schema_actual)
    history = _build_turn_history(state.messages, user_message)
    tools = [_schema_context_tool_spec(), _anthropic_tool_spec()]
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    anthropic_messages = client.messages
    try:
        count_method = getattr(anthropic_messages, "count_tokens", None)
        if count_method is None:
            raise AttributeError("AsyncAnthropic.messages.count_tokens no está disponible")

        respuesta = await count_method(
            model=settings.anthropic_model,
            system=cast(Any, system_prompt),
            messages=history,
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
            + json.dumps(history, ensure_ascii=False, default=str)
            + json.dumps(tools, ensure_ascii=False, default=str)
        )
        return max(1, len(texto_completo) // 4)


def _conversation_key(user_key: str, report_id: int, conversation_id: str) -> str:
    return f"{user_key}:{report_id}:{conversation_id}"


def _build_turn_history(messages: List[Dict[str, Any]], user_message: str) -> List[Dict[str, Any]]:
    history = _sanitize_history(messages)
    history.append({"role": "user", "content": user_message})
    return history


def _import_external_mcp_runtime():
    """Import the installed `mcp` package even if the project has a local `mcp/` folder.

    The repository now contains a local folder named `mcp` for the Power BI MCP
    server implementation. That folder can shadow the third-party dependency with
    the same import name, so we temporarily remove the project root from
    `sys.path` while importing the installed package.
    """

    original_sys_path = list(sys.path)
    filtered_sys_path = []
    for entry in original_sys_path:
        if not entry:
            continue
        try:
            resolved = Path(entry).resolve()
        except Exception:
            filtered_sys_path.append(entry)
            continue

        if resolved == _PROJECT_ROOT:
            continue
        filtered_sys_path.append(entry)

    try:
        sys.path = filtered_sys_path
        return importlib.import_module("mcp")
    finally:
        sys.path = original_sys_path



def _minify_schema_text(schema_text: str) -> str:
    schema_text = schema_text.strip()
    if not schema_text:
        return "(No schema available)"
    try:
        schema_obj = json.loads(schema_text)
        return json.dumps(schema_obj, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return schema_text


def _build_system_prompt(schema_text: str) -> List[Dict[str, Any]]:
    schema_block = _minify_schema_text(schema_text)
    return [
        {
            "type": "text",
            "text": (
                "Eres un asistente de analítica para Power BI."
                "Reglas estrictas de seguridad y optimización:"
                "- ¡CRÍTICO! Si el 'Contexto del modelo semántico actual' ya contiene las medidas necesarias, DEBES usar la herramienta execute_dax_query directamente. NO uses get_schema_context a menos que la medida solicitada no exista en el contexto."
                "- Cuando uses execute_dax_query, usa SIEMPRE la instrucción EVALUATE."
                "- La tabla de hechos principal es 'sales_order'. Se relaciona con la tabla de fechas mediante 'sales_order'[date_order] -> 'Date'[Date]."
                "- OPTIMIZACIÓN DAX: Si necesitas calcular múltiples valores (ej. ARS y USD, o distintos periodos), DEBES agruparlos en una única consulta utilizando múltiples columnas dentro de EVALUATE ROW. Nunca hagas llamadas separadas a la herramienta."
                "  Ejemplo de estructura esperada:"
                "  EVALUATE ROW("
                "    \"Métrica 1\", CALCULATE(...),"
                "    \"Métrica 2\", CALCULATE(...)"
                "  )"
                "- Nunca pidas, menciones ni intentes inferir dataset_id o workspace_id."
                f"- La tabla de fechas es {DATE_TABLE_GUIDANCE}."
                f"- {MONETARY_FILTER_GUIDANCE}"
                "- SINTAXIS DAX CRÍTICA (¡Cuidado con SQL!):"
                "  1. Lógica: Usa '&&' (AND) y '||' (OR). Jamás uses AND/OR como operadores."
                "  2. Condicionales: No existe CASE WHEN. Usa SWITCH(TRUE(), ...)."
                "  3. Nulos: Usa ISBLANK() o BLANK(), nunca IS NULL."
                "  4. Texto: Usa CONTAINSSTRING() en lugar de LIKE."
                "  5. Concatenación: Usa '&', nunca '||'."
                "  6. Relaciones: Confía en las relaciones del modelo. Usa RELATED() si iteras, no intentes forzar JOINs manuales."
                "- TABLAS Y EVOLUCIÓN: Si el usuario pide una evolución temporal, tendencias, o agrupaciones, NUNCA uses ADDCOLUMNS(SUMMARIZE(...)). Usa SIEMPRE la función SUMMARIZECOLUMNS. Ejemplo: EVALUATE SUMMARIZECOLUMNS('Date'[Anio], 'Date'[Mes], 'Filtro', FILTER('Date', ...), 'Ticket ARS', CALCULATE(...)) ORDER BY 'Date'[Anio], 'Date'[Mes]"
                "- Responde en español y sé preciso."
                "- Nunca uses markdown (asteriscos, almohadillas, guiones bajos). "
                "- PROTECCIÓN DE VOLUMEN: Si la consulta agrupa por dimensiones descriptivas (ej. productos, clientes, sucursales), ESTÁS OBLIGADO a envolver SUMMARIZECOLUMNS con la función TOPN (ej. TOPN(15, SUMMARIZECOLUMNS(...), [Tu Medida], DESC)) para devolver máximo 15 resultados, a menos que el usuario pida un número distinto."
                #"- ¡CRÍTICO! Si el 'Contexto del modelo semántico actual' ya contiene las medidas solicitadas (ej. [Ticket Promedio]), DEBES usarlas directamente en tu consulta (ej. CALCULATE([Nombre De La Medida])). NO intentes recrear la lógica matemática con SUMX, AVERAGEX, etc. Usa las medidas ya existentes."
            ),
        },
        {
            "type": "text",
            "text": (
                "Contexto del modelo semántico actual:"
                f"{schema_block}"
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _anthropic_tool_spec() -> Dict[str, Any]:
    return {
        "name": "execute_dax_query",
        "description": (
            "Ejecuta una consulta DAX contra el dataset actual. "
            "Solo proporciona dax_query; el backend inyecta el dataset_id."
        ),
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
        "description": (
            "Recupera contexto relevante del esquema mediante reranking. "
            "Solo proporciona question; el backend inyecta el dataset_id. "
            "Úsala si necesitas más contexto para construir el DAX."
        ),
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


def _mcp_result_to_text(result: Any) -> str:
    if result is None:
        return ""

    # NUEVO: Si es un string, intentamos ver si es un JSON formateado y lo compactamos
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            return json.dumps(parsed, separators=(',', ':'), ensure_ascii=False)
        except Exception:
            return result  # Si no es un JSON válido, devolvemos el texto original

    if isinstance(result, bytes):
        try:
            parsed = json.loads(result.decode("utf-8", errors="replace"))
            return json.dumps(parsed, separators=(',', ':'), ensure_ascii=False)
        except Exception:
            return result.decode("utf-8", errors="replace")

    if isinstance(result, dict):
        if "content" in result:
            return _content_to_text(result["content"])
        # Añadido separators
        return json.dumps(result, separators=(',', ':'), ensure_ascii=False, default=str)

    if hasattr(result, "content"):
        return _content_to_text(getattr(result, "content"))

    if hasattr(result, "model_dump"):
        dumped = result.model_dump()
        if isinstance(dumped, dict) and "content" in dumped:
            return _content_to_text(dumped["content"])
        # Añadido separators
        return json.dumps(dumped, separators=(',', ':'), ensure_ascii=False, default=str)

    if hasattr(result, "dict"):
        dumped = result.dict()
        if isinstance(dumped, dict) and "content" in dumped:
            return _content_to_text(dumped["content"])
        # Añadido separators
        return json.dumps(dumped, separators=(',', ':'), ensure_ascii=False, default=str)

    return str(result)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                elif "content" in item:
                    parts.append(_content_to_text(item["content"]))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str))
            elif hasattr(item, "text"):
                parts.append(str(getattr(item, "text")))
            elif hasattr(item, "content"):
                parts.append(_content_to_text(getattr(item, "content")))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if "text" in content:
            return str(content["text"])
        return json.dumps(content, ensure_ascii=False, default=str)
    return str(content)


def _text_from_blocks(blocks: Iterable[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for block in blocks:
        if block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts).strip()


def _trim_history(messages: List[Dict[str, Any]], history_limit: int) -> List[Dict[str, Any]]:
    if history_limit <= 0:
        return _sanitize_history(messages)
    return _sanitize_history(messages[-history_limit:])


def _sanitize_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop leading orphan tool_result messages so the next Anthropic turn stays valid."""

    sanitized = list(messages)
    while sanitized and _message_is_orphan_tool_result(sanitized[0]):
        sanitized.pop(0)
    return sanitized


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

