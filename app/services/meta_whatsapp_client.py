import os
import requests

_BASE_URL = "https://graph.facebook.com/v20.0"


class MetaWhatsAppError(Exception):
    pass


def send_text_message(to: str, text: str, timeout: int = 15) -> dict:
    """Send a plain-text WhatsApp message via Meta Cloud API.

    `to` is the recipient phone number in international format without +
    (e.g. '5491126770450').
    """
    phone_number_id = os.environ["META_WA_PHONE_NUMBER_ID"]
    access_token = os.environ["META_WA_ACCESS_TOKEN"]

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
