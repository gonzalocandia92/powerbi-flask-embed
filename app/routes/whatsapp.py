"""
WhatsApp webhook for KLARA, backed by a self-hosted Evolution API instance.

Flow per incoming message:
  1. Evolution API POSTs the raw message event here.
  2. If the sender's phone number isn't registered yet, the message is treated
     as a registration code: it must match an active PublicLink.custom_slug,
     the same slug already used for that report's public link / web widget.
  3. Once registered, every message is forwarded to the same agent used by the
     web chat (chatbot_service.procesar_interaccion_completa) and the answer
     is sent back through Evolution API.

NOTE: the exact webhook payload shape below (event name, key/message fields)
follows Evolution API's common "messages.upsert" format, but should be
double-checked against the real payloads once the instance is live, since
the schema can vary slightly between Evolution API versions.
"""
import asyncio
import logging
import os
import re
import unicodedata
from datetime import timedelta

import requests
from flask import Blueprint, jsonify, request
from sqlalchemy import func

from app import db
from app.models import PublicLink, WhatsAppContact
from app.services import chatbot_service, evolution_client
from app.utils.decorators import retry_on_db_error

bp = Blueprint("whatsapp", __name__)


def _contact_ttl_hours() -> float:
    """Hours after which a WhatsAppContact registration expires.

    Set via WHATSAPP_CONTACT_TTL_HOURS for testing, so the same number can
    re-register without manually deleting rows. Unset or 0 means permanent
    (the production default).
    """
    try:
        return float(os.getenv("WHATSAPP_CONTACT_TTL_HOURS") or 0)
    except ValueError:
        return 0


_GREETINGS = {
    "hola", "holis", "buenas", "buen dia", "buenos dias", "buenas tardes",
    "buenas noches", "que tal", "hello", "hi", "hey",
}


def _is_greeting(text: str) -> bool:
    normalized = unicodedata.normalize("NFKD", text.lower().strip())
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    normalized = re.sub(r"[^a-z ]", "", normalized).strip()
    return normalized in _GREETINGS


def _extract_incoming_message(payload: dict):
    """Return (phone_number, text) or (None, None) if the event should be ignored."""
    if (payload.get("event") or "").lower() != "messages.upsert":
        return None, None

    data = payload.get("data") or {}
    key = data.get("key") or {}

    if key.get("fromMe"):
        return None, None

    remote_jid = key.get("remoteJid") or ""
    phone_number = remote_jid.split("@")[0].strip()
    if not phone_number:
        return None, None

    message = data.get("message") or {}
    text = (
        message.get("conversation")
        or (message.get("extendedTextMessage") or {}).get("text")
        or ""
    ).strip()

    return phone_number, (text or None)


@retry_on_db_error(max_retries=3, delay=1)
def _find_contact_sync(phone_number: str):
    contact = WhatsAppContact.query.filter_by(phone_number=phone_number).first()
    if contact is None:
        return None

    ttl_hours = _contact_ttl_hours()
    if ttl_hours > 0:
        # Compare entirely server-side (func.now() vs created_at) so this is
        # correct regardless of the DB session's configured timezone — the
        # created_at column has no tzinfo, and naively re-labeling it as UTC
        # in Python breaks when the session timezone isn't UTC (it isn't here).
        expired = db.session.query(
            db.session.query(WhatsAppContact.id)
            .filter(
                WhatsAppContact.id == contact.id,
                func.now() - WhatsAppContact.created_at > timedelta(hours=ttl_hours),
            )
            .exists()
        ).scalar()
        if expired:
            db.session.delete(contact)
            db.session.commit()
            return None

    return contact


@retry_on_db_error(max_retries=3, delay=1)
def _register_contact_sync(phone_number: str, slug: str):
    link = PublicLink.query.filter_by(custom_slug=slug, is_active=True).first()
    if not link:
        return None, None

    contact = WhatsAppContact(
        phone_number=phone_number,
        report_id_fk=link.report_id_fk,
        slug=slug,
    )
    db.session.add(contact)
    db.session.commit()
    return contact, link.report.name


@retry_on_db_error(max_retries=3, delay=1)
def _try_acquire_lock_sync(contact_id: int) -> bool:
    """Atomically claim the contact for processing. Returns False if already busy."""
    updated = (
        WhatsAppContact.query
        .filter_by(id=contact_id, is_processing=False)
        .update({"is_processing": True})
    )
    db.session.commit()
    return bool(updated)


@retry_on_db_error(max_retries=3, delay=1)
def _release_lock_and_save_sync(contact_id: int, conversation_id):
    contact = db.session.get(WhatsAppContact, contact_id)
    if contact is None:
        return
    contact.is_processing = False
    if conversation_id is not None:
        contact.conversation_id = int(conversation_id)
    db.session.commit()


async def _send_reply(phone_number: str, text: str):
    try:
        await asyncio.to_thread(evolution_client.send_text_message, phone_number, text)
    except (evolution_client.EvolutionClientError, requests.exceptions.RequestException):
        logging.exception("[WhatsApp] Failed to send reply to %s", phone_number)


@bp.route("/webhook/whatsapp", methods=["POST"], defaults={"event_suffix": None})
@bp.route("/webhook/whatsapp/<path:event_suffix>", methods=["POST"])
async def whatsapp_webhook(event_suffix):
    payload = request.get_json(silent=True) or {}
    phone_number, text = _extract_incoming_message(payload)

    if not phone_number or not text:
        return jsonify({"ok": True}), 200

    contact = await asyncio.to_thread(_find_contact_sync, phone_number)

    if contact is None:
        new_contact, report_name = await asyncio.to_thread(_register_contact_sync, phone_number, text)
        if new_contact:
            await _send_reply(
                phone_number,
                f"Listo, quedaste conectado al tablero de \"{report_name}\". "
                "Ya podes preguntarme lo que necesites.",
            )
        elif _is_greeting(text):
            await _send_reply(
                phone_number,
                "Hola! Soy KLARA. Para conectarte con tu tablero, mandame el mismo "
                "link/slug publico que usas para verlo.",
            )
        else:
            await _send_reply(
                phone_number,
                "No reconozco ese codigo. Mandame el mismo link/slug publico que usas para "
                "ver tu tablero, asi te conecto con KLARA.",
            )
        return jsonify({"ok": True}), 200

    if not await asyncio.to_thread(_try_acquire_lock_sync, contact.id):
        await _send_reply(phone_number, "Todavia estoy respondiendo tu mensaje anterior, dame un momento.")
        return jsonify({"ok": True}), 200

    try:
        resultado = await chatbot_service.procesar_interaccion_completa(
            text,
            slug=contact.slug,
            user_key=f"whatsapp:{phone_number}",
            conversation_id=str(contact.conversation_id) if contact.conversation_id else None,
        )
        await asyncio.to_thread(
            _release_lock_and_save_sync, contact.id, resultado.get("conversation_id")
        )
        await _send_reply(phone_number, resultado.get("answer") or "No tengo una respuesta para eso.")
    except chatbot_service.ChatbotServiceError:
        logging.exception("[WhatsApp] Agent failed for %s", phone_number)
        await asyncio.to_thread(_release_lock_and_save_sync, contact.id, None)
        await _send_reply(phone_number, "Tuve un problema para responder tu consulta, probá de nuevo en un momento.")
    except Exception:
        logging.exception("[WhatsApp] Unexpected error for %s", phone_number)
        await asyncio.to_thread(_release_lock_and_save_sync, contact.id, None)
        await _send_reply(phone_number, "Tuve un problema para responder tu consulta, probá de nuevo en un momento.")

    return jsonify({"ok": True}), 200
