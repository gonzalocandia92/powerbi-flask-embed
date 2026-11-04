import os
import requests

_BASE_URL = "https://graph.facebook.com/v20.0"


class MetaWhatsAppError(Exception):
    """`status_code` is the HTTP status and `error_code` Meta's numeric code (e.g. 131047), when known."""

    def __init__(self, message: str, status_code: int | None = None, error_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


def _api_error(resp, prefix: str) -> MetaWhatsAppError:
    try:
        error_code = resp.json().get("error", {}).get("code")
    except Exception:  # noqa: BLE001 - non-JSON error page
        error_code = None
    return MetaWhatsAppError(f"{prefix} {resp.status_code}: {resp.text}", status_code=resp.status_code,
                             error_code=error_code if isinstance(error_code, int) else None)


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
        raise _api_error(resp, "Meta API error")
    return resp.json()


# Meta's limit for document media; checked here so we fail before uploading.
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024


def upload_media(content: bytes, filename: str, mime_type: str = "application/pdf",
                 timeout: int = 60) -> str:
    """Upload a file to Meta's media store and return its media id.

    The id can then be sent with `send_document`. Meta keeps uploaded media for 30 days.
    """
    if not content:
        raise MetaWhatsAppError("Cannot upload an empty file")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise MetaWhatsAppError(f"File too large for WhatsApp: {len(content)} bytes (max {MAX_DOCUMENT_BYTES})")
    phone_number_id = os.environ["META_WA_PHONE_NUMBER_ID"]
    access_token = os.environ["META_WA_ACCESS_TOKEN"]

    url = f"{_BASE_URL}/{phone_number_id}/media"
    resp = requests.post(
        url,
        data={"messaging_product": "whatsapp", "type": mime_type},
        files={"file": (filename, content, mime_type)},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if not resp.ok:
        raise _api_error(resp, "Meta media upload error")
    media_id = resp.json().get("id")
    if not media_id:
        raise MetaWhatsAppError(f"Meta media upload returned no id: {resp.text}")
    return media_id


def send_document(to: str, media_id: str, filename: str, caption: str | None = None,
                  timeout: int = 15) -> dict:
    """Send an already uploaded document (see `upload_media`) via Meta Cloud API.

    Free-form like `send_text_message`: Meta only delivers it inside the 24h customer
    service window (the recipient wrote to the number in the last 24h). Outside it,
    a business-initiated send needs an approved template (error 131047 otherwise).
    """
    phone_number_id = os.environ["META_WA_PHONE_NUMBER_ID"]
    access_token = os.environ["META_WA_ACCESS_TOKEN"]
    to = _normalize_ar_number(to)

    document = {"id": media_id, "filename": filename}
    if caption:
        document["caption"] = caption
    url = f"{_BASE_URL}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "document",
        "document": document,
    }
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if not resp.ok:
        raise _api_error(resp, "Meta API error")
    return resp.json()


def send_pdf(to: str, content: bytes, filename: str, caption: str | None = None) -> dict:
    """Upload a PDF and send it as a document in one step."""
    media_id = upload_media(content, filename, "application/pdf")
    return send_document(to, media_id, filename, caption)


def send_template(to: str, template_name: str, language: str,
                  document: tuple[str, str] | None = None,
                  body_params: list[str] | tuple[str, ...] = (),
                  timeout: int = 15) -> dict:
    """Send an approved template message via Meta Cloud API.

    Unlike free-form messages this works outside the 24h customer service window
    (and is billed per message). `document` is `(media_id, filename)` for a template
    whose header is of type Document; `body_params` fill `{{1}}`, `{{2}}`... in order.
    The number of parameters must match the approved template or Meta rejects it.
    """
    phone_number_id = os.environ["META_WA_PHONE_NUMBER_ID"]
    access_token = os.environ["META_WA_ACCESS_TOKEN"]
    to = _normalize_ar_number(to)

    components = []
    if document:
        media_id, filename = document
        components.append({
            "type": "header",
            "parameters": [{"type": "document", "document": {"id": media_id, "filename": filename}}],
        })
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(value)} for value in body_params],
        })
    template = {"name": template_name, "language": {"code": language}}
    if components:
        template["components"] = components
    url = f"{_BASE_URL}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": template,
    }
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if not resp.ok:
        raise _api_error(resp, "Meta API error")
    return resp.json()


def send_pdf_template(to: str, content: bytes, filename: str, template_name: str, language: str,
                      body_params: list[str] | tuple[str, ...] = ()) -> dict:
    """Upload a PDF and send it as the Document header of an approved template."""
    media_id = upload_media(content, filename, "application/pdf")
    return send_template(to, template_name, language, (media_id, filename), body_params)


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
