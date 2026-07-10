"""
WhatsApp webhook for KLARA, backed by Meta Cloud API.

Registration flow:
  1. User sends the public slug of their report as their first message.
  2. The bot confirms the connection and stores the phone↔report binding.
  3. Every subsequent message is forwarded to the same chatbot agent used
     by the web chat (chatbot_service.procesar_interaccion_completa).

Webhook setup (Meta Developer Console):
  - Callback URL: https://<your-domain>/webhook/whatsapp
  - Verify Token: value of META_WA_VERIFY_TOKEN in .env
  - Subscribed fields: messages
"""
import asyncio
import hashlib
import hmac
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
from app.services import chatbot_service, meta_whatsapp_client
from app.utils.decorators import retry_on_db_error

bp = Blueprint("whatsapp", __name__)

_GREETINGS = {
    "hola", "holis", "buenas", "buen dia", "buenos dias", "buenas tardes",
    "buenas noches", "que tal", "hello", "hi", "hey",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _contact_ttl_hours() -> float:
    try:
        return float(os.getenv("WHATSAPP_CONTACT_TTL_HOURS") or 0)
    except ValueError:
        return 0


def _is_greeting(text: str) -> bool:
    normalized = unicodedata.normalize("NFKD", text.lower().strip())
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    normalized = re.sub(r"[^a-z ]", "", normalized).strip()
    return normalized in _GREETINGS


def _verify_meta_signature(payload: bytes, signature_header: str) -> bool:
    """Verify the X-Hub-Signature-256 header Meta sends on every webhook POST."""
    app_secret = os.getenv("META_WA_APP_SECRET", "")
    if not app_secret:
        return True  # skip verification if secret not configured yet
    expected = "sha256=" + hmac.new(
        app_secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header or "")


def _extract_incoming_message(payload: dict):
    """Return (phone_number, text, message_id) or (None, None, None)."""
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]
        value = change["value"]
        if change.get("field") != "messages":
            return None, None, None
        messages = value.get("messages")
        if not messages:
            return None, None, None
        msg = messages[0]
        if msg.get("type") != "text":
            return None, None, None
        phone_number = msg["from"]
        text = (msg.get("text") or {}).get("body", "").strip()
        message_id = msg.get("id")
        return phone_number, (text or None), message_id
    except (KeyError, IndexError, TypeError):
        return None, None, None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

@retry_on_db_error(max_retries=3, delay=1)
def _find_contact_sync(phone_number: str):
    contact = WhatsAppContact.query.filter_by(phone_number=phone_number).first()
    if contact is None:
        return None

    ttl_hours = _contact_ttl_hours()
    if ttl_hours > 0:
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
        await asyncio.to_thread(meta_whatsapp_client.send_text_message, phone_number, text)
    except (meta_whatsapp_client.MetaWhatsAppError, requests.exceptions.RequestException):
        logging.exception("[WhatsApp] Failed to send reply to %s", phone_number)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/webhook/whatsapp", methods=["GET"])
def whatsapp_verify():
    """Meta webhook verification handshake."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == os.environ.get("META_WA_VERIFY_TOKEN"):
        return challenge, 200
    return "Forbidden", 403


@bp.route("/webhook/whatsapp", methods=["POST"])
async def whatsapp_webhook():
    raw_body = request.get_data()
    if not _verify_meta_signature(raw_body, request.headers.get("X-Hub-Signature-256", "")):
        return jsonify({"error": "invalid signature"}), 403

    payload = request.get_json(silent=True) or {}
    phone_number, text, message_id = _extract_incoming_message(payload)

    if not phone_number or not text:
        return jsonify({"ok": True}), 200

    if message_id:
        try:
            await asyncio.to_thread(meta_whatsapp_client.mark_as_read, message_id)
        except Exception:
            pass

    contact = await asyncio.to_thread(_find_contact_sync, phone_number)

    if contact is None:
        new_contact, report_name = await asyncio.to_thread(_register_contact_sync, phone_number, text)
        if new_contact:
            await _send_reply(
                phone_number,
                f'Listo, quedaste conectado al tablero de "{report_name}". '
                "Ya podes preguntarme lo que necesites.",
            )
        elif _is_greeting(text):
            await _send_reply(
                phone_number,
                "Hola! Soy KLARA. Para conectarte con tu tablero, mandame el "
                "link/slug publico que usas para verlo.",
            )
        else:
            await _send_reply(
                phone_number,
                "No reconozco ese codigo. Mandame el mismo link/slug publico que usas "
                "para ver tu tablero, asi te conecto con KLARA.",
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
        await _send_reply(phone_number, "Tuve un problema para responder tu consulta, proba de nuevo en un momento.")
    except Exception:
        logging.exception("[WhatsApp] Unexpected error for %s", phone_number)
        await asyncio.to_thread(_release_lock_and_save_sync, contact.id, None)
        await _send_reply(phone_number, "Tuve un problema para responder tu consulta, proba de nuevo en un momento.")

    return jsonify({"ok": True}), 200
