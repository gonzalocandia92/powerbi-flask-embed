import os
import requests

_BASE_URL = "https://graph.facebook.com/v20.0"


class MetaWhatsAppError(Exception):
    pass


def _normalize_ar_number(number: str) -> str:
    """Convert Argentine mobile numbers from wa_id format (549XXXXXXXXX) to
    the old format (54XXXXX15XXXX) that Meta's test-mode allowed list expects.
    In production this conversion is not needed; Meta accepts both formats.
    Only applied when META_WA_TEST_MODE=true is set in env.
    """
    if os.getenv("META_WA_TEST_MODE") == "true" and number.startswith("549") and len(number) == 13:
        area_and_num = number[3:]  # strip 549 → e.g. 3624297130
        area = area_and_num[:3]    # e.g. 362
        num = area_and_num[3:]     # e.g. 4297130
        return f"54{area}15{num}"  # → 54362154297130
    return number


def send_text_message(to: str, text: str, timeout: int = 15) -> dict:
    """Send a plain-text WhatsApp message via Meta Cloud API.

    `to` is the recipient phone number in international format without +
    (e.g. '5491126770450').
    """
    phone_number_id = os.environ["META_WA_PHONE_NUMBER_ID"]
    access_token = os.environ["META_WA_ACCESS_TOKEN"]
    to = _normalize_ar_number(to)

    url = f"{_BASE_URL}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if not resp.ok:
        raise MetaWhatsAppError(f"Meta API error {resp.status_code}: {resp.text}")
    return resp.json()


def mark_as_read(message_id: str, timeout: int = 10) -> None:
    """Mark an incoming message as read (shows double blue tick on sender's side)."""
    phone_number_id = os.environ["META_WA_PHONE_NUMBER_ID"]
    access_token = os.environ["META_WA_ACCESS_TOKEN"]

    url = f"{_BASE_URL}/{phone_number_id}/messages"
    requests.post(
        url,
        json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
