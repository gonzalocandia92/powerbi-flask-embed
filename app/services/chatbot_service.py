"""
Chatbot service - public agent-backed chat wrapper.

The async service keeps network-bound work non-blocking while DB operations
remain synchronous but are isolated through ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import DBAPIError, OperationalError

from app import db
from app.models import ChatMessage, ChatSession, PublicLink, Report
from app.services.chat_credentials import resolve_powerbi_env_for_report
from app.services import chat_mcp
from app.utils.chatbot_context import get_report_and_dataset_by_slug
from app.utils.decorators import retry_on_db_error


class ChatbotServiceError(Exception):
    """Base exception for chatbot service failures."""


class ChatbotNotFoundError(ChatbotServiceError):
    """Raised when the public slug cannot be resolved to a report."""


def _chat_message_to_anthropic_message(message: ChatMessage) -> Optional[Dict[str, Any]]:
    role = (message.role or "").strip().lower()
    content = message.content or ""

    if role == "user":
        return {"role": "user", "content": content}

    if role != "assistant":
        return None

    # Historical tool_use blocks are intentionally not replayed because the
    # matching tool_result payloads are not persisted today.
    return {"role": "assistant", "content": content}


def _load_anthropic_history(
    *,
    session_id: int,
    exclude_message_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    query = (
        ChatMessage.query
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
    )
    if exclude_message_id is not None:
        query = query.filter(ChatMessage.id != exclude_message_id)

    history: List[Dict[str, Any]] = []
    for message in query.all():
        entry = _chat_message_to_anthropic_message(message)
        if entry is not None:
            history.append(entry)
    return history


def _extract_session_id(conversation_id: Optional[str]) -> Optional[int]:
    if not conversation_id:
        return None
    try:
        return int(conversation_id)
    except (TypeError, ValueError):
        return None


def _build_session_title(question: str) -> str:
    text = (question or "").strip()
    if not text:
        return "Chat"
    return text[:80] + ("..." if len(text) > 80 else "")


def _using_sqlite() -> bool:
    bind = db.session.get_bind()
    return bool(bind and bind.dialect.name == "sqlite")


def _next_integer_id(model) -> int:
    current_max = db.session.query(func.max(model.id)).scalar()
    return int(current_max or 0) + 1


def _prepare_sqlite_id(instance, model) -> None:
    if _using_sqlite() and getattr(instance, "id", None) is None:
        setattr(instance, "id", _next_integer_id(model))


def _ensure_session_for_turn(
    *,
    slug: str,
    conversation_id: Optional[str],
    reset_history: bool,
    question: str,
) -> ChatSession:
    session: Optional[ChatSession] = None
    session_id = _extract_session_id(conversation_id)

    if session_id is not None and not reset_history:
        session = db.session.get(ChatSession, session_id)

    if session is None or reset_history:
        session = ChatSession(slug=slug, title=_build_session_title(question))
        _prepare_sqlite_id(session, ChatSession)
        db.session.add(session)
        db.session.flush()
        return session

    if not session.slug:
        session.slug = slug
    if not session.title:
        session.title = _build_session_title(question)
    return session


@retry_on_db_error(max_retries=3, delay=1)
def _resolve_report_and_dataset_sync(slug: str) -> Tuple[Report, str, Dict[str, str]]:
    try:
        resolved = get_report_and_dataset_by_slug(slug)
    except Exception as exc:
        logging.exception("[ChatbotService] Failed to resolve dataset for slug %s", slug)
        resolved = None
        resolution_error = exc
    else:
        resolution_error = None

    if resolved:
        report, dataset_id = resolved
    else:
        link = (
            PublicLink.query
            .filter_by(custom_slug=slug, is_active=True)
            .first()
        )
        if not link:
            raise ChatbotNotFoundError(f"Slug not found or inactive: {slug}") from resolution_error

        report = link.report
        dataset_id = os.getenv("CHATBOT_DATASET_ID") or report.report_id

    powerbi_credentials = resolve_powerbi_env_for_report(report)
    return report, dataset_id, powerbi_credentials


@retry_on_db_error(max_retries=3, delay=1)
def _prepare_turn_sync(
    *,
    slug: str,
    conversation_id: Optional[str],
    reset_history: bool,
    question: str,
) -> Tuple[int, List[Dict[str, Any]]]:
    session = _ensure_session_for_turn(
        slug=slug,
        conversation_id=conversation_id,
        reset_history=reset_history,
        question=question,
    )

    user_message = ChatMessage(
        session_id=session.id,
        role="user",
        content=question,
    )
    _prepare_sqlite_id(user_message, ChatMessage)
    db.session.add(user_message)
    db.session.flush()

    history = _load_anthropic_history(
        session_id=session.id,
        exclude_message_id=user_message.id,
    )
    return session.id, history


@retry_on_db_error(max_retries=3, delay=1)
def _persist_success_sync(
    *,
    session_id: int,
    report_id: int,
    result: Dict[str, Any],
    latency_ms: int,
    anthropic_model: str,
) -> Dict[str, Any]:
    session = db.session.get(ChatSession, session_id)
    if session is None:
        raise ChatbotServiceError(f"Chat session not found: {session_id}")

    assistant_message = ChatMessage(
        session_id=session.id,
        role="assistant",
        content=result.get("answer", ""),
        latency_ms=latency_ms,
        model_used=result.get("model") or anthropic_model,
        input_tokens=result.get("input_tokens"),
        output_tokens=result.get("output_tokens"),
        # NOTA: Se utiliza el campo heredado mcp_used para almacenar si el agente ejecutó herramientas (tools_called) en este turno, evitando migraciones de DB.
        mcp_used=bool(result.get("tools_called")),
        tools_called=result.get("tools_called") or None,
        dax_query=result.get("dax_query"),
        had_error=False,
    )
    _prepare_sqlite_id(assistant_message, ChatMessage)
    db.session.add(assistant_message)

    session.total_messages = (session.total_messages or 0) + 2
    session.last_message_at = datetime.now(timezone.utc)
    session.had_errors = bool(session.had_errors)

    db.session.commit()

    return {
        "answer": result.get("answer", ""),
        "conversation_id": session.id,
        "report_id": report_id,
        "tool_rounds": result.get("tool_rounds", 0),
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
        "model": result.get("model") or anthropic_model,
        "latency_ms": assistant_message.latency_ms,
        "mcp_used": bool(result.get("tools_called")),
        "tools_called": result.get("tools_called") or [],
        "dax_query": result.get("dax_query"),
    }


@retry_on_db_error(max_retries=3, delay=1)
def _persist_error_sync(
    *,
    session_id: Optional[int],
    slug: str,
    question: str,
    error_message: str,
) -> None:
    db.session.rollback()

    session = db.session.get(ChatSession, session_id) if session_id is not None else None
    if session is None:
        session = ChatSession(slug=slug, title=_build_session_title(question))
        _prepare_sqlite_id(session, ChatSession)
        db.session.add(session)
        db.session.flush()

    error_log = ChatMessage(
        session_id=session.id,
        role="assistant",
        content=f"Error al procesar la consulta: {error_message}",
        had_error=True,
        error_message=error_message,
    )
    _prepare_sqlite_id(error_log, ChatMessage)
    db.session.add(error_log)

    session.total_messages = (session.total_messages or 0) + 1
    session.last_message_at = datetime.now(timezone.utc)
    session.had_errors = True

    db.session.commit()


async def procesar_interaccion_completa(
    pregunta: str,
    *,
    slug: str,
    user_key: str,
    conversation_id: Optional[str] = None,
    reset_history: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Execute the full chatbot interaction.

    Sync database work is offloaded with ``asyncio.to_thread`` so the Anthropic
    request can await without blocking the event loop.
    """
    if not slug:
        raise ValueError("slug is required")

    pregunta = (pregunta or "").strip()
    if not pregunta:
        raise ValueError("La pregunta no puede estar vacia")

    settings = chat_mcp.build_runtime_settings(config or dict(current_app.config))
    start = time.monotonic()
    session_id: Optional[int] = None

    try:
        report, dataset_id, powerbi_credentials = await asyncio.to_thread(_resolve_report_and_dataset_sync, slug)
        session_id, history = await asyncio.to_thread(
            _prepare_turn_sync,
            slug=slug,
            conversation_id=conversation_id,
            reset_history=reset_history,
            question=pregunta,
        )

        result = await chat_mcp.run_chat_turn(
            user_message=pregunta,
            dataset_id=dataset_id,
            history=history,
            settings=settings,
            conversation_id=str(session_id),
            report_id=report.id,
            powerbi_credentials=powerbi_credentials,
        )

        latency_ms = int((time.monotonic() - start) * 1000)
        return await asyncio.to_thread(
            _persist_success_sync,
            session_id=session_id,
            report_id=report.id,
            result=result,
            latency_ms=latency_ms,
            anthropic_model=settings.anthropic_model,
        )

    except ChatbotNotFoundError:
        raise
    except Exception as exc:
        try:
            await asyncio.to_thread(
                _persist_error_sync,
                session_id=session_id,
                slug=slug,
                question=pregunta,
                error_message=str(exc),
            )
        except Exception:
            logging.exception("[ChatbotService] Failed to log error to DB")
        raise ChatbotServiceError(f"Error al procesar la consulta: {str(exc)}") from exc


async def procesar_pregunta(
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
    Legacy compatibility wrapper.

    The new controller path should call procesar_interaccion_completa directly.
    """
    report = db.session.get(Report, report_id)
    if not report:
        raise ChatbotNotFoundError(f"Report not found: {report_id}")

    link = (
        PublicLink.query
        .filter_by(report_id_fk=report_id, is_active=True)
        .order_by(PublicLink.id.asc())
        .first()
    )
    if not link:
        raise ChatbotNotFoundError(f"No active public link found for report: {report_id}")

    return await procesar_interaccion_completa(
        pregunta,
        slug=link.custom_slug,
        user_key=user_key,
        conversation_id=conversation_id,
        reset_history=reset_history,
        config=config,
    )
