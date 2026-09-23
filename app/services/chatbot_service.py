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
from app.services import ai_billing, model_catalog
from app.services.agent_core import DEFAULT_MODEL
from app.services.analytics import (
    AnalyticsBillingLimitExceededError,
    AnalyticsExecutor,
    AnalyticsModelError,
    AnalyticsReportNotFoundError,
    AnalyticsRequest,
    build_analytics_engine,
)
from app.services.observability import (
    hash_identifier,
    observation_preview,
    propagate_trace_attributes,
    start_observation,
    trace_user_id,
)
from app.utils.decorators import retry_on_db_error


class ChatbotServiceError(Exception):
    """Base exception for chatbot service failures."""


class ChatbotNotFoundError(ChatbotServiceError):
    """Raised when the public slug cannot be resolved to a report."""


class ChatbotLimitExceededError(ChatbotServiceError):
    """Raised when the configured AI spend limit has already been reached."""


class ChatbotModelNotAllowedError(ChatbotServiceError):
    """Raised when a client requests a model outside the effective allowlist."""


def _chat_message_to_history_message(message: ChatMessage) -> Optional[Dict[str, Any]]:
    role = (message.role or "").strip().lower()
    content = message.content or ""

    if role == "user":
        return {"role": "user", "content": content}

    if role != "assistant":
        return None

    # Historical tool_use blocks are intentionally not replayed because the
    # matching tool_result payloads are not persisted today.
    return {"role": "assistant", "content": content}


def _load_history(
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
        entry = _chat_message_to_history_message(message)
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
def _resolve_report_sync(slug: str) -> Report:
    """Resolve the Chat-specific public slug without preparing analytics context."""
    link = PublicLink.query.filter_by(custom_slug=slug, is_active=True).first()
    if not link:
        raise ChatbotNotFoundError(f"Slug not found or inactive: {slug}")
    return link.report


@retry_on_db_error(max_retries=3, delay=1)
def _prepare_turn_sync(
    *,
    billing_context: ai_billing.BillingContext,
    slug: str,
    conversation_id: Optional[str],
    reset_history: bool,
    question: str,
    requested_model_key: Optional[str] = None,
) -> Tuple[int, List[Dict[str, Any]]]:
    session = _ensure_session_for_turn(
        slug=slug,
        conversation_id=conversation_id,
        reset_history=reset_history,
        question=question,
    )
    session.workspace_id_fk = billing_context.workspace_id
    session.report_id_fk = billing_context.report_id
    session.empresa_id = billing_context.empresa_id

    user_message = ChatMessage(
        session_id=session.id,
        role="user",
        content=question,
        requested_model_key=requested_model_key,
    )
    _prepare_sqlite_id(user_message, ChatMessage)
    db.session.add(user_message)
    db.session.flush()

    history = _load_history(
        session_id=session.id,
        exclude_message_id=user_message.id,
    )
    session.total_messages = (session.total_messages or 0) + 1
    session.last_message_at = datetime.now(timezone.utc)
    db.session.commit()
    return session.id, history


def _record_result_usage_events(
    *,
    result: Dict[str, Any],
    session_id: int,
    message_id: int,
    billing_context: ai_billing.BillingContext,
    model: str,
    had_error: bool,
) -> int:
    persisted_events = 0
    cost_events = []
    for raw_event in result.get("ai_usage_events") or []:
        event_payload = dict(raw_event)
        metadata_json = event_payload.pop("metadata_json", None)
        recorded_event = ai_billing.record_ai_usage_event(
            session_id=session_id,
            message_id=message_id,
            workspace_id=billing_context.workspace_id,
            report_id=billing_context.report_id,
            empresa_id=billing_context.empresa_id,
            billing_scope_type=billing_context.billing_scope_type,
            billing_scope_id=billing_context.billing_scope_id,
            metadata_json=metadata_json,
            **event_payload,
        )
        cost_events.append(recorded_event)
        persisted_events += 1

    input_tokens = result.get("input_tokens")
    output_tokens = result.get("output_tokens")
    if persisted_events == 0 and (input_tokens is not None or output_tokens is not None):
        recorded_event = ai_billing.record_ai_usage_event(
            session_id=session_id,
            message_id=message_id,
            workspace_id=billing_context.workspace_id,
            report_id=billing_context.report_id,
            empresa_id=billing_context.empresa_id,
            billing_scope_type=billing_context.billing_scope_type,
            billing_scope_id=billing_context.billing_scope_id,
            provider=result.get("provider") or "anthropic",
            model=model,
            event_type="generation",
            source_type="chat",
            trigger_type="user_request",
            operation_name="chat-response-fallback",
            status="error" if had_error else "success",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=int(input_tokens or 0) + int(output_tokens or 0),
            metadata_json={
                "component": "main_agent",
                "execution_source": "chat",
                "estimated_usage": True,
                "fallback_reason": "missing_ai_usage_events",
            },
        )
        cost_events.append(recorded_event)
        persisted_events += 1

    result["cost_breakdown"] = ai_billing.summarize_pipeline_costs(cost_events)
    return persisted_events


@retry_on_db_error(max_retries=3, delay=1)
def _persist_success_sync(
    *,
    session_id: int,
    billing_context: ai_billing.BillingContext,
    report_id: int,
    result: Dict[str, Any],
    latency_ms: int,
    anthropic_model: str,
    requested_model_key: Optional[str] = None,
) -> Dict[str, Any]:
    session = db.session.get(ChatSession, session_id)
    if session is None:
        raise ChatbotServiceError(f"Chat session not found: {session_id}")

    had_error = bool(result.get("had_error"))
    assistant_message = ChatMessage(
        session_id=session.id,
        role="assistant",
        content=result.get("answer", ""),
        latency_ms=latency_ms,
        model_used=result.get("model") or anthropic_model,
        requested_model_key=requested_model_key,
        model_key=result.get("model_key"),
        model_provider=result.get("provider"),
        physical_model=result.get("model"),
        model_gateway=result.get("gateway"),
        service_tier=result.get("service_tier"),
        actual_model=result.get("actual_model"),
        input_tokens=result.get("input_tokens"),
        output_tokens=result.get("output_tokens"),
        # NOTA: Se utiliza el campo heredado mcp_used para almacenar si el agente ejecutó herramientas (tools_called) en este turno, evitando migraciones de DB.
        mcp_used=bool(result.get("tools_called")),
        tools_called=result.get("tools_called") or None,
        dax_query=result.get("dax_query"),
        had_error=had_error,
        error_message=result.get("error_message") if had_error else None,
    )
    _prepare_sqlite_id(assistant_message, ChatMessage)
    db.session.add(assistant_message)
    db.session.flush()

    _record_result_usage_events(
        result=result,
        session_id=session.id,
        message_id=assistant_message.id,
        billing_context=billing_context,
        model=result.get("model") or anthropic_model,
        had_error=had_error,
    )
    session.total_messages = (session.total_messages or 0) + 1
    session.last_message_at = datetime.now(timezone.utc)
    session.had_errors = bool(session.had_errors or had_error)
    session.last_model_key = result.get("model_key") or session.last_model_key

    db.session.commit()

    try:
        ai_billing.update_message_usage_totals(assistant_message.id)
        db.session.commit()
        db.session.refresh(assistant_message)
    except Exception:
        db.session.rollback()
        logging.exception(
            "[ChatbotService] Failed to refresh aggregated usage totals for assistant message %s",
            assistant_message.id,
        )

    return {
        "answer": result.get("answer", ""),
        "conversation_id": session.id,
        "report_id": report_id,
        "tool_rounds": result.get("tool_rounds", 0),
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
        "model": result.get("model") or anthropic_model,
        "model_key": result.get("model_key"),
        "provider": result.get("provider"),
        "gateway": result.get("gateway"),
        "service_tier": result.get("service_tier"),
        "actual_model": result.get("actual_model"),
        "latency_ms": assistant_message.latency_ms,
        "mcp_used": bool(result.get("tools_called")),
        "tools_called": result.get("tools_called") or [],
        "dax_query": result.get("dax_query"),
        "had_error": had_error,
        "error_message": result.get("error_message") if had_error else None,
        "failure_reason": result.get("failure_reason") if had_error else None,
        "route_metadata_json": result.get("route_metadata_json"),
        "route_validation_warnings": result.get("route_validation_warnings") or [],
        "total_cost_usd": assistant_message.total_cost_usd or 0.0,
        "cost_breakdown": result.get("cost_breakdown"),
        "latency_by_component_ms": result.get("latency_by_component_ms"),
    }


@retry_on_db_error(max_retries=3, delay=1)
def _persist_error_sync(
    *,
    session_id: Optional[int],
    slug: str,
    question: str,
    error_message: str,
    billing_context: Optional[ai_billing.BillingContext] = None,
    result: Optional[Dict[str, Any]] = None,
    requested_model_key: Optional[str] = None,
) -> None:
    db.session.rollback()

    session = db.session.get(ChatSession, session_id) if session_id is not None else None
    if session is None:
        session = ChatSession(slug=slug, title=_build_session_title(question))
        _prepare_sqlite_id(session, ChatSession)
        db.session.add(session)
        db.session.flush()
        if billing_context is not None:
            session.workspace_id_fk = billing_context.workspace_id
            session.report_id_fk = billing_context.report_id
            session.empresa_id = billing_context.empresa_id

    error_log = ChatMessage(
        session_id=session.id,
        role="assistant",
        content=f"Error al procesar la consulta: {error_message}",
        latency_ms=result.get("latency_ms") if result else None,
        model_used=result.get("model") if result else None,
        requested_model_key=requested_model_key,
        model_key=result.get("model_key") if result else None,
        model_provider=result.get("provider") if result else None,
        physical_model=result.get("model") if result else None,
        model_gateway=result.get("gateway") if result else None,
        service_tier=result.get("service_tier") if result else None,
        actual_model=result.get("actual_model") if result else None,
        input_tokens=result.get("input_tokens") if result else None,
        output_tokens=result.get("output_tokens") if result else None,
        mcp_used=bool(result.get("tools_called")) if result else None,
        tools_called=result.get("tools_called") if result else None,
        dax_query=result.get("dax_query") if result else None,
        had_error=True,
        error_message=error_message,
    )
    _prepare_sqlite_id(error_log, ChatMessage)
    db.session.add(error_log)
    db.session.flush()

    if billing_context is not None and result is not None:
        _record_result_usage_events(
            result=result,
            session_id=session.id,
            message_id=error_log.id,
            billing_context=billing_context,
            model=result.get("model") or DEFAULT_MODEL,
            had_error=True,
        )
    # If we are reusing an existing session, the user message was already
    # committed during turn preparation, so only the assistant error is added.
    session.total_messages = (session.total_messages or 0) + 1
    session.last_message_at = datetime.now(timezone.utc)
    session.had_errors = True
    if result and result.get("model_key"):
        session.last_model_key = result["model_key"]

    db.session.commit()

    if billing_context is not None and result is not None:
        try:
            ai_billing.update_message_usage_totals(error_log.id)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logging.exception(
                "[ChatbotService] Failed to refresh aggregated usage totals for error message %s",
                error_log.id,
            )


async def procesar_interaccion_completa(
    pregunta: str,
    *,
    slug: str,
    user_key: str,
    conversation_id: Optional[str] = None,
    reset_history: bool = False,
    config: Optional[Dict[str, Any]] = None,
    source: str = "chat",
    model_key: Optional[str] = None,
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

    runtime_config = config or dict(current_app.config)
    start = time.monotonic()
    session_id: Optional[int] = None
    report: Optional[Report] = None
    billing_context: Optional[ai_billing.BillingContext] = None
    result: Optional[Dict[str, Any]] = None
    user_turn_persisted = False
    trace_version = os.getenv("RELEASE_VERSION") or os.getenv("GIT_SHA")

    async def persist_execution_error(exc: Exception) -> None:
        if not user_turn_persisted:
            return
        try:
            await asyncio.to_thread(
                _persist_error_sync,
                session_id=session_id,
                slug=slug,
                question=pregunta,
                error_message=str(exc),
                billing_context=billing_context,
                result=result,
                requested_model_key=model_key,
            )
        except Exception:
            logging.exception("[ChatbotService] Failed to log error to DB")

    with start_observation(
        name="public-chat-request",
        as_type="span",
        input={"message": pregunta},
    ) as root_observation:
        try:
            report = await asyncio.to_thread(_resolve_report_sync, slug)
            billing_context = await asyncio.to_thread(ai_billing.resolve_report_billing_context, report)
            previous_model_key = await asyncio.to_thread(
                model_catalog.session_model_key, conversation_id, report_id=report.id
            )
            try:
                model_selection = await asyncio.to_thread(
                    model_catalog.resolve_client_selection,
                    requested_model_key=model_key,
                    session_model_key=previous_model_key,
                    report_id=report.id,
                    empresa_id=billing_context.empresa_id,
                    config=runtime_config,
                )
            except model_catalog.ModelSelectionError as exc:
                raise ChatbotModelNotAllowedError(str(exc)) from exc
            analytics: AnalyticsExecutor = build_analytics_engine(runtime_config)
            prepared = await analytics.prepare(AnalyticsRequest(
                report_id=report.id,
                question=pregunta,
                source=source,
                model_key=model_selection.model.model_key if model_selection else None,
            ))
            session_id, history = await asyncio.to_thread(
                _prepare_turn_sync,
                billing_context=billing_context,
                slug=slug,
                conversation_id=conversation_id,
                reset_history=reset_history,
                question=pregunta,
                requested_model_key=model_key,
            )
            user_turn_persisted = True

            trace_metadata = {
                "feature": "publicchat",
                "reportid": str(report.id),
                "reportname": report.name,
                "empresa": str(billing_context.empresa_id),
                "source": source,
                "slughash": hash_identifier(slug, prefix="slug"),
                "resethistory": str(bool(reset_history)).lower(),
                "hashistory": str(bool(history)).lower(),
                "requestedmodelkey": model_key,
                "selectedmodelkey": model_selection.model.model_key if model_selection else None,
                "modelselectionsource": model_selection.source if model_selection else "legacy_default",
            }
            if root_observation is not None:
                root_observation.update(metadata=trace_metadata)

            with propagate_trace_attributes(
                user_id=trace_user_id(user_key),
                session_id=str(session_id),
                trace_name="public-chat-request",
                metadata=trace_metadata,
                tags=[source, "powerbi"],
                version=trace_version,
            ):
                analytics_result = await analytics.execute_prepared(
                    prepared,
                    history=history,
                    execution_id=str(session_id),
                    trace_context=trace_metadata,
                )
                result = analytics_result.to_dict()

            latency_ms = int((time.monotonic() - start) * 1000)
            persisted = await asyncio.to_thread(
                _persist_success_sync,
                session_id=session_id,
                billing_context=billing_context,
                report_id=report.id,
                result=result,
                latency_ms=latency_ms,
                anthropic_model=DEFAULT_MODEL,
                requested_model_key=model_key,
            )

            if root_observation is not None:
                root_observation.update(
                    output={
                        "answer": observation_preview(persisted.get("answer", ""), max_length=1200),
                        "tool_rounds": persisted.get("tool_rounds", 0),
                        "report_id": persisted.get("report_id"),
                        "cost_breakdown": persisted.get("cost_breakdown"),
                    }
                )
            return persisted

        except ChatbotNotFoundError:
            if root_observation is not None:
                root_observation.update(output={"error": "slug_not_found"})
            raise
        except AnalyticsReportNotFoundError as exc:
            if root_observation is not None:
                root_observation.update(output={"error": "report_not_found"})
            raise ChatbotNotFoundError(str(exc)) from exc
        except ChatbotModelNotAllowedError:
            if root_observation is not None:
                root_observation.update(output={"error": "model_not_allowed"})
            raise
        except AnalyticsModelError as exc:
            if root_observation is not None:
                root_observation.update(output={"error": "model_not_allowed"})
            await persist_execution_error(exc)
            raise ChatbotModelNotAllowedError(str(exc)) from exc
        except (ai_billing.BillingLimitExceeded, AnalyticsBillingLimitExceededError) as exc:
            if root_observation is not None:
                root_observation.update(output={"error": "billing_limit_exceeded"})
            await persist_execution_error(exc)
            raise ChatbotLimitExceededError(str(exc)) from exc
        except Exception as exc:
            if root_observation is not None:
                root_observation.update(output={"error": observation_preview(str(exc), max_length=500)})
            await persist_execution_error(exc)
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
    source: str = "chat",
    model_key: Optional[str] = None,
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
        source=source,
        model_key=model_key,
    )

# Transitional aliases for integrations importing the former helper names.
_chat_message_to_anthropic_message = _chat_message_to_history_message
_load_anthropic_history = _load_history
