"""
Chatbot service — public MCP-backed chat wrapper.

Architecture:
  Browser → POST /chat (Flask) → chat_mcp.run_chat_turn() → response

This module adapts the MCP orchestration to the public chatbot endpoint while
capturing telemetry useful for DB logging (tokens, tool calls, DAX, latency).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

from app.services import chat_mcp


def _run_async(coro):
    """Run an async coroutine from a sync Flask context."""
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        if "asyncio.run() cannot be called from a running event loop" not in str(exc):
            raise
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _extract_tool_calls(
    state: chat_mcp.ChatConversationState,
    user_message: str,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Extract tool calls and DAX query from the latest turn history."""
    tools_called: List[Dict[str, Any]] = []
    dax_query: Optional[str] = None
    user_message_norm = (user_message or "").strip()
    found_user = False

    for message in reversed(state.messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            if message["content"].strip() == user_message_norm:
                found_user = True
                break

        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_entry = {
                "name": block.get("name"),
                "input": block.get("input"),
            }
            tools_called.append(tool_entry)
            if dax_query is None and tool_entry.get("name") == "execute_dax_query":
                tool_input = tool_entry.get("input") or {}
                if isinstance(tool_input, dict):
                    dax_query = tool_input.get("dax_query") or tool_input.get("query")

    if not found_user and not tools_called:
        for message in reversed(state.messages[-8:]):
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_entry = {
                    "name": block.get("name"),
                    "input": block.get("input"),
                }
                tools_called.append(tool_entry)
                if dax_query is None and tool_entry.get("name") == "execute_dax_query":
                    tool_input = tool_entry.get("input") or {}
                    if isinstance(tool_input, dict):
                        dax_query = tool_input.get("dax_query") or tool_input.get("query")

    tools_called.reverse()
    return tools_called, dax_query


def procesar_pregunta(
    pregunta: str,
    *,
    dataset_id: str,
    user_key: str,
    report_id: int,
    conversation_id: Optional[str] = None,
    reset_history: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Execute a public chat turn using the MCP orchestrator.

    Returns a /api/chat-like payload plus telemetry for DB logging.
    """
    if not dataset_id:
        raise RuntimeError("dataset_id is required")

    state = chat_mcp.get_conversation_state(
        user_key=user_key,
        report_id=report_id,
        conversation_id=conversation_id,
        reset=reset_history,
    )
    settings = chat_mcp.build_runtime_settings(config or dict(current_app.config))

    start = time.monotonic()
    result = _run_async(
        chat_mcp.run_chat_turn(
            user_message=pregunta,
            dataset_id=dataset_id,
            state=state,
            settings=settings,
        )
    )
    latency_ms = int((time.monotonic() - start) * 1000)

    tools_called, dax_query = _extract_tool_calls(state, pregunta)
    mcp_used = bool(settings.mcp_command)

    return {
        "answer": result.get("answer", ""),
        "conversation_id": result.get("conversation_id") or state.conversation_id,
        "report_id": report_id,
        "tool_rounds": result.get("tool_rounds", 0),
        "input_tokens": result.get("input_tokens"),
        "output_tokens": None,
        "model": settings.anthropic_model,
        "latency_ms": latency_ms,
        "mcp_used": mcp_used,
        "tools_called": tools_called,
        "dax_query": dax_query,
    }
