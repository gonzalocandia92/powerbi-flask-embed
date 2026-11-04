"""``DeliveryChannel`` over the Meta WhatsApp Cloud API, with an approved template.

A template (not a free-form message) because scheduled reports are business-initiated: Meta only accepts
free-form messages inside the 24h customer service window. The template's header must be a Document; its
body variables are filled, in order, with the ``params`` the service passes.
"""
from __future__ import annotations

from typing import Sequence

import requests

from app.persistence.delivery_states import CHANNEL_WHATSAPP
from app.services import meta_whatsapp_client as wa

from .delivery_contracts import DeliveryPermanentError, DeliveryTransientError

# Meta error codes that are worth retrying: throttling and temporary failures on Meta's side.
# https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
TRANSIENT_META_CODES = frozenset({
    1,        # API unknown
    2,        # API service temporarily unavailable
    4,        # application request limit reached
    80007,    # WhatsApp Business Account rate limit
    130429,   # throughput rate limit
    131000,   # something went wrong
    131048,   # spam rate limit
    131056,   # pair rate limit (too many messages to the same number)
    133004,   # server temporarily unavailable
})


def _classified(exc: Exception) -> Exception:
    if isinstance(exc, requests.RequestException):
        return DeliveryTransientError(f"No se pudo contactar a Meta: {type(exc).__name__}")
    if isinstance(exc, wa.MetaWhatsAppError):
        status = exc.status_code
        transient = (status is not None and (status >= 500 or status == 429)) or exc.error_code in TRANSIENT_META_CODES
        code = f"meta_{exc.error_code}" if exc.error_code is not None else None
        error_type = DeliveryTransientError if transient else DeliveryPermanentError
        return error_type(str(exc), code=code)
    return exc


class WhatsAppTemplateChannel:
    name = CHANNEL_WHATSAPP

    def __init__(self, template_name: str, language: str):
        self.template_name = template_name
        self.language = language

    def upload(self, content: bytes, filename: str) -> str:
        try:
            return wa.upload_media(content, filename, "application/pdf")
        except Exception as exc:  # noqa: BLE001 - classified for the retry policy
            raise _classified(exc) from exc

    def send(self, recipient: str, attachment_id: str, filename: str, params: Sequence[str]) -> str | None:
        try:
            result = wa.send_template(recipient, self.template_name, self.language,
                                      document=(attachment_id, filename), body_params=list(params))
        except Exception as exc:  # noqa: BLE001 - classified for the retry policy
            raise _classified(exc) from exc
        messages = result.get("messages") if isinstance(result, dict) else None
        return messages[0].get("id") if messages and isinstance(messages[0], dict) else None
