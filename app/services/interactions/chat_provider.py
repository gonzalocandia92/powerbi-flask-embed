"""Reconstructs KLARA chat/WhatsApp interactions from ChatSession/ChatMessage.

Message pairing
----------------
A logical interaction is "user question + its assistant reply" (the reply
may be missing if the turn never completed). ``ChatMessage.reply_to_message_id``
is the explicit, non-fragile link used for every turn recorded going
forward (both success and error paths — see ``chatbot_service``): pairing
never has to guess from ordering for new data, regardless of concurrency.

Historical rows predate that column and have it NULL. For those we fall
back to positional pairing: the nearest-preceding user message in the same
session (equivalent to "the previous row", via a window LAG). This matches
the architecture's real guarantee — WhatsApp turns are serialized per
contact by ``WhatsAppContact.is_processing`` and a single browser tab issues
one request at a time — so strict alternation is the overwhelmingly common
case. The one scenario this fallback cannot disambiguate is two concurrent,
interleaved requests against the *same* conversation_id from the same
browser session finishing out of order (e.g. a double-submit racing a slow
first request): a historical row like that could be paired with the wrong
question. This is a known, documented limitation of the historical
reconstruction only — it cannot happen for new data.

Scalability
-----------
Pairing and "was this the last, still-unanswered message" are computed with
SQL window functions (LAG/LEAD) over an indexed (session_id, id) ordering, so
this never materializes full session histories in Python — it is one
filtered, paginated query, safe to grow with the table.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.orm import aliased

from app import db
from app.models import ChatMessage, ChatSession, Empresa, Report

from .contracts import InteractionFilters, InteractionRow

SOURCE_KEY = "chat"


def _channel_filter_applies(channel: Optional[str]) -> bool:
    return channel in (None, "", "klara_chat", "whatsapp", "unknown")


def _anchor_subquery():
    replied = aliased(ChatMessage, name="replied_to_msg")

    msg_window = (
        select(
            ChatMessage.id.label("msg_id"),
            ChatMessage.session_id.label("session_id"),
            ChatMessage.role.label("role"),
            ChatMessage.content.label("content"),
            ChatMessage.created_at.label("created_at"),
            ChatMessage.total_cost_usd.label("total_cost_usd"),
            ChatMessage.model_key.label("model_key"),
            ChatMessage.latency_ms.label("latency_ms"),
            ChatMessage.had_error.label("had_error"),
            replied.content.label("linked_question"),
            func.lag(ChatMessage.content)
            .over(partition_by=ChatMessage.session_id, order_by=ChatMessage.id)
            .label("prev_content"),
            func.lag(ChatMessage.role)
            .over(partition_by=ChatMessage.session_id, order_by=ChatMessage.id)
            .label("prev_role"),
            func.lead(ChatMessage.id)
            .over(partition_by=ChatMessage.session_id, order_by=ChatMessage.id)
            .label("next_id"),
        )
        .select_from(ChatMessage)
        .outerjoin(replied, replied.id == ChatMessage.reply_to_message_id)
    ).subquery("msg_window")

    anchor = (
        select(
            msg_window.c.msg_id,
            msg_window.c.session_id,
            msg_window.c.created_at,
            msg_window.c.total_cost_usd,
            msg_window.c.model_key,
            msg_window.c.latency_ms,
            msg_window.c.had_error,
            case(
                (
                    msg_window.c.role == "assistant",
                    func.coalesce(msg_window.c.linked_question, msg_window.c.prev_content),
                ),
                else_=msg_window.c.content,
            ).label("question"),
            case(
                (msg_window.c.role == "assistant", msg_window.c.content),
                else_=literal(None),
            ).label("answer"),
        )
        .where(
            or_(
                msg_window.c.role == "assistant",
                # A dangling last user message: the turn never got a reply
                # (in-flight, or a crash before the error path could log
                # one). Surfaced with answer=None rather than hidden.
                and_(msg_window.c.role == "user", msg_window.c.next_id.is_(None)),
            )
        )
    ).subquery("interaction_anchor")
    return anchor


def base_query(filters: InteractionFilters):
    """Return a filtered, not-yet-paginated Select in the common column shape."""
    if filters.channel and not _channel_filter_applies(filters.channel):
        return None

    anchor = _anchor_subquery()
    query = (
        select(
            anchor.c.msg_id.label("raw_id"),
            anchor.c.created_at.label("occurred_at"),
            ChatSession.empresa_id.label("empresa_id"),
            Empresa.nombre.label("empresa_name"),
            ChatSession.report_id_fk.label("report_id"),
            Report.name.label("report_name"),
            func.coalesce(ChatSession.channel, literal("unknown")).label("channel"),
            anchor.c.question.label("question"),
            anchor.c.answer.label("answer"),
            anchor.c.total_cost_usd.label("total_cost_usd"),
            anchor.c.model_key.label("model_key"),
            anchor.c.latency_ms.label("latency_ms"),
            anchor.c.had_error.label("had_error"),
        )
        .select_from(anchor)
        .join(ChatSession, ChatSession.id == anchor.c.session_id)
        .outerjoin(Empresa, Empresa.id == ChatSession.empresa_id)
        .outerjoin(Report, Report.id == ChatSession.report_id_fk)
    )

    if filters.date_from is not None:
        query = query.where(anchor.c.created_at >= filters.date_from)
    if filters.date_to is not None:
        query = query.where(anchor.c.created_at < filters.date_to)
    if filters.empresa_id is not None:
        query = query.where(ChatSession.empresa_id == filters.empresa_id)
    if filters.report_id is not None:
        query = query.where(ChatSession.report_id_fk == filters.report_id)
    if filters.channel == "unknown":
        query = query.where(ChatSession.channel.is_(None))
    elif filters.channel in ("klara_chat", "whatsapp"):
        query = query.where(ChatSession.channel == filters.channel)
    if filters.search:
        term = f"%{filters.search}%"
        query = query.where(or_(anchor.c.question.ilike(term), anchor.c.answer.ilike(term)))

    return query


def get_detail(raw_id: int) -> Optional[dict]:
    message = db.session.get(ChatMessage, raw_id)
    if message is None:
        return None
    session = message.session

    if message.role == "assistant":
        question = None
        if message.reply_to_message_id:
            replied = db.session.get(ChatMessage, message.reply_to_message_id)
            question = replied.content if replied else None
        if question is None:
            prev = (
                ChatMessage.query
                .filter(
                    ChatMessage.session_id == message.session_id,
                    ChatMessage.role == "user",
                    ChatMessage.id < message.id,
                )
                .order_by(ChatMessage.id.desc())
                .first()
            )
            question = prev.content if prev else None
        answer = message.content
        input_tokens, output_tokens = message.input_tokens, message.output_tokens
        provider, actual_model = message.model_provider, message.actual_model
        tools_called, dax_query = message.tools_called, message.dax_query
        error_message = message.error_message if message.had_error else None
        total_cost_usd, model_key, latency_ms, had_error = (
            message.total_cost_usd, message.model_key, message.latency_ms, message.had_error,
        )
    else:
        question = message.content
        answer = None
        input_tokens = output_tokens = None
        provider = actual_model = tools_called = dax_query = error_message = None
        total_cost_usd = model_key = latency_ms = had_error = None

    return {
        "id": f"{SOURCE_KEY}:{message.id}",
        "occurred_at": message.created_at,
        "empresa_id": session.empresa_id if session else None,
        "empresa_name": session.empresa.nombre if session and session.empresa else None,
        "report_id": session.report_id_fk if session else None,
        "report_name": session.report.name if session and session.report else None,
        "channel": session.channel or "unknown" if session else "unknown",
        "question": question,
        "answer": answer,
        "total_cost_usd": total_cost_usd,
        "model_key": model_key,
        "latency_ms": latency_ms,
        "had_error": had_error,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "provider": provider,
        "actual_model": actual_model,
        "tools_called": tools_called,
        "dax_query": dax_query,
        "error_message": error_message,
        "slug": session.slug if session else None,
    }
