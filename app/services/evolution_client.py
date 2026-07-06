"""
Thin client for the self-hosted Evolution API (WhatsApp).

Only the calls needed for the KLARA WhatsApp MVP live here: sending a text
message back to a contact. Receiving messages is handled by the webhook
route in app/routes/whatsapp.py.
"""
import os

import requests


class EvolutionClientError(Exception):
    """Raised when a call to Evolution API fails or is misconfigured."""


def _get_config() -> tuple[str, str, str]:
    base_url = os.getenv("EVOLUTION_API_URL")
    api_key = os.getenv("EVOLUTION_API_KEY")
    instance = os.getenv("EVOLUTION_INSTANCE")
    if not base_url or not api_key or not instance:
        raise EvolutionClientError(
            "EVOLUTION_API_URL, EVOLUTION_API_KEY y EVOLUTION_INSTANCE deben estar definidos en .env"
        )
    return base_url.rstrip("/"), api_key, instance


def send_text_message(phone_number: str, text: str, timeout: int = 15) -> None:
    """Send a plain text WhatsApp message to phone_number via Evolution API."""
    base_url, api_key, instance = _get_config()

    response = requests.post(
        f"{base_url}/message/sendText/{instance}",
        headers={"apikey": api_key, "Content-Type": "application/json"},
        json={"number": phone_number, "text": text},
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise EvolutionClientError(
            f"Evolution API respondio {response.status_code}: {response.text[:300]}"
        )
