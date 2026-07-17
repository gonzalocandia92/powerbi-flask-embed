"""
WhatsApp webhook for KLARA, backed by Meta Cloud API.

Access flow:
  1. An admin pre-authorizes which phone numbers can query which reports,
     scoped to a single empresa (see WhatsAppAuthorizedNumber).
  2. On first contact, an authorized number is connected straight to its
     report if it only has one, or shown a numbered menu if it has several.
  3. The user can send "menu"/"cambiar" at any time to switch reports.
  4. Every message is forwarded to the same chatbot agent used by the web
     chat (chatbot_service.procesar_interaccion_completa).

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
import threading
import time
import unicodedata

import requests
from flask import Blueprint, jsonify, request

from app import db
from app.models import Empresa, PublicLink, Report, WhatsAppAuthorizedNumber, WhatsAppContact
from app.services import chatbot_service, meta_whatsapp_client
from app.utils.decorators import retry_on_db_error

bp = Blueprint("whatsapp", __name__)

# In-memory dedup cache: message_id → expiry timestamp
# Prevents processing the same webhook twice when Meta retries delivery.
_seen_message_ids: dict[str, float] = {}
_seen_lock = threading.Lock()
_SEEN_TTL = 120  # seconds


def _is_duplicate(message_id: str) -> bool:
    now = time.monotonic()
    with _seen_lock:
        # Evict expired entries
        expired = [k for k, v in _seen_message_ids.items() if v < now]
        for k in expired:
            del _seen_message_ids[k]
        if message_id in _seen_message_ids:
            return True
        _seen_message_ids[message_id] = now + _SEEN_TTL
        return False


_MENU_COMMANDS = {"menu", "cambiar"}

_NO_ACCESS_MESSAGE = (
    "No tenes acceso habilitado a ningun tablero desde este numero. "
    "Si crees que esto es un error, contacta a tu administrador."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.lower().strip())
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", "", normalized).strip()


def _is_menu_command(text: str) -> bool:
    words = set(_normalize_text(text).split())
    return bool(words & _MENU_COMMANDS)


def _build_menu_text(authorized) -> str:
    lines = ["Tenes acceso a varios tableros. Respondeme con el numero del que queres consultar:"]
    for i, entry in enumerate(authorized, start=1):
        lines.append(f"{i}. {entry.report.name}")
    return "\n".join(lines)


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
    return WhatsAppContact.query.filter_by(phone_number=phone_number).first()


@retry_on_db_error(max_retries=3, delay=1)
def _authorized_entries_sync(phone_number: str):
    """Reports this number may access: empresa must have WhatsApp enabled and active,
    and the report itself must have KLARA (chatbot_enabled) turned on."""
    return (
        WhatsAppAuthorizedNumber.query
        .join(Empresa, WhatsAppAuthorizedNumber.empresa_id_fk == Empresa.id)
        .join(Report, WhatsAppAuthorizedNumber.report_id_fk == Report.id)
        .filter(
            WhatsAppAuthorizedNumber.phone_number == phone_number,
            Empresa.whatsapp_enabled.is_(True),
            Empresa.estado_activo.is_(True),
            Report.chatbot_enabled.is_(True),
        )
        .order_by(WhatsAppAuthorizedNumber.report_id_fk)
        .all()
    )


@retry_on_db_error(max_retries=3, delay=1)
def _create_contact_sync(phone_number: str, report_id: int = None, awaiting_selection: bool = False):
    contact = WhatsAppContact(
        phone_number=phone_number,
        report_id_fk=report_id,
        awaiting_report_selection=awaiting_selection,
    )
    db.session.add(contact)
    db.session.commit()
    return contact


@retry_on_db_error(max_retries=3, delay=1)
def _select_report_sync(contact_id: int, report_id: int):
    contact = db.session.get(WhatsAppContact, contact_id)
    if contact is None:
        return None
    contact.report_id_fk = report_id
    contact.awaiting_report_selection = False
    db.session.commit()
    return contact


@retry_on_db_error(max_retries=3, delay=1)
def _reset_to_menu_sync(contact_id: int):
    contact = db.session.get(WhatsAppContact, contact_id)
    if contact is None:
        return
    contact.report_id_fk = None
    contact.awaiting_report_selection = True
    db.session.commit()


@retry_on_db_error(max_retries=3, delay=1)
def _delete_contact_sync(contact_id: int):
    contact = db.session.get(WhatsAppContact, contact_id)
    if contact is None:
        return
    db.session.delete(contact)
    db.session.commit()


@retry_on_db_error(max_retries=3, delay=1)
def _active_slug_sync(report_id: int):
    link = (
        PublicLink.query
        .filter_by(report_id_fk=report_id, is_active=True)
        .first()
    )
    return link.custom_slug if link else None


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


def _md_to_wa(text: str) -> str:
    """Convert standard markdown to WhatsApp-compatible formatting.

    WhatsApp supports: *bold*, _italic_, ~strikethrough~, `mono`, and plain bullet •
    It does NOT render: **double asterisk**, ##headers, [links](url), ```code blocks```
    """
    # Remove triple-backtick code blocks, keep the content
    text = re.sub(r"```[^\n]*\n?([\s\S]*?)```", lambda m: m.group(1).strip(), text)
    # **bold** or __bold__ → *bold*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"*\1*", text, flags=re.DOTALL)
    # # Headings → just the text (bold)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    # - list / * list → bullet •  (only at start of line)
    text = re.sub(r"^[\-\*]\s+", "• ", text, flags=re.MULTILINE)
    # [text](url) links → text
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    # Horizontal rules
    text = re.sub(r"^---+$", "", text, flags=re.MULTILINE)
    # Collapse more than 2 consecutive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def _send_reply(phone_number: str, text: str):
    try:
        await asyncio.to_thread(
            meta_whatsapp_client.send_text_message, phone_number, _md_to_wa(text)
        )
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

    if message_id and _is_duplicate(message_id):
        return jsonify({"ok": True}), 200

    if message_id:
        try:
            await asyncio.to_thread(meta_whatsapp_client.mark_as_read, message_id)
        except Exception:
            pass

    contact = await asyncio.to_thread(_find_contact_sync, phone_number)

    # First contact: look up what this number is authorized to see.
    if contact is None:
        authorized = await asyncio.to_thread(_authorized_entries_sync, phone_number)
        if not authorized:
            await _send_reply(phone_number, _NO_ACCESS_MESSAGE)
            return jsonify({"ok": True}), 200

        if len(authorized) == 1:
            entry = authorized[0]
            await asyncio.to_thread(_create_contact_sync, phone_number, entry.report_id_fk, False)
            await _send_reply(
                phone_number,
                f'Listo, quedaste conectado al tablero de "{entry.report.name}". '
                "Ya podes preguntarme lo que necesites.",
            )
        else:
            await asyncio.to_thread(_create_contact_sync, phone_number, None, True)
            await _send_reply(phone_number, _build_menu_text(authorized))
        return jsonify({"ok": True}), 200

    # Explicit request to switch reports.
    if _is_menu_command(text):
        authorized = await asyncio.to_thread(_authorized_entries_sync, phone_number)
        if not authorized:
            await asyncio.to_thread(_delete_contact_sync, contact.id)
            await _send_reply(phone_number, _NO_ACCESS_MESSAGE)
            return jsonify({"ok": True}), 200
        if len(authorized) == 1:
            await asyncio.to_thread(_select_report_sync, contact.id, authorized[0].report_id_fk)
            await _send_reply(phone_number, f'Ya estas conectado al tablero de "{authorized[0].report.name}".')
        else:
            await asyncio.to_thread(_reset_to_menu_sync, contact.id)
            await _send_reply(phone_number, _build_menu_text(authorized))
        return jsonify({"ok": True}), 200

    # Waiting for the user to pick a report from the menu.
    if contact.awaiting_report_selection:
        authorized = await asyncio.to_thread(_authorized_entries_sync, phone_number)
        if not authorized:
            await asyncio.to_thread(_delete_contact_sync, contact.id)
            await _send_reply(phone_number, _NO_ACCESS_MESSAGE)
            return jsonify({"ok": True}), 200

        choice = text.strip()
        index = int(choice) - 1 if choice.isdigit() else -1
        if 0 <= index < len(authorized):
            entry = authorized[index]
            await asyncio.to_thread(_select_report_sync, contact.id, entry.report_id_fk)
            await _send_reply(
                phone_number,
                f'Listo, quedaste conectado al tablero de "{entry.report.name}". '
                "Ya podes preguntarme lo que necesites.",
            )
        else:
            await _send_reply(phone_number, _build_menu_text(authorized))
        return jsonify({"ok": True}), 200

    # Guard against access revoked after the contact was created.
    still_authorized = await asyncio.to_thread(_authorized_entries_sync, phone_number)
    if not any(e.report_id_fk == contact.report_id_fk for e in still_authorized):
        await asyncio.to_thread(_delete_contact_sync, contact.id)
        await _send_reply(phone_number, _NO_ACCESS_MESSAGE)
        return jsonify({"ok": True}), 200

    if not await asyncio.to_thread(_try_acquire_lock_sync, contact.id):
        await _send_reply(phone_number, "Todavia estoy respondiendo tu mensaje anterior, dame un momento.")
        return jsonify({"ok": True}), 200

    slug = await asyncio.to_thread(_active_slug_sync, contact.report_id_fk)
    if not slug:
        await asyncio.to_thread(_release_lock_and_save_sync, contact.id, None)
        await _send_reply(phone_number, "Tu tablero no tiene un link publico activo en este momento.")
        return jsonify({"ok": True}), 200

    try:
        resultado = await chatbot_service.procesar_interaccion_completa(
            text,
            slug=slug,
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
