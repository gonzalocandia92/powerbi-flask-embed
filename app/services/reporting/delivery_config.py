"""Who receives a schedule's approved report: the delivery part of ``ReportSchedule.config_json``.

Pure: no Flask, no SQLAlchemy. Stored as ``config["delivery"]["whatsapp"]["recipients"]`` so new channels
(email, ...) are new keys under ``delivery`` and never need a migration.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .schedule_contracts import ScheduleValidationError

INVALID_RECIPIENTS = "invalid_recipients"
MAX_RECIPIENTS = 20

_PHONE_RE = re.compile(r"^\d{8,15}$")
_PHONE_NOISE_RE = re.compile(r"[\s\-().+]")


def normalize_phone(raw: Any) -> str:
    """International format without ``+``, spaces or dashes (wa_id style), or ``ScheduleValidationError``."""
    phone = _PHONE_NOISE_RE.sub("", raw) if isinstance(raw, str) else ""
    if not _PHONE_RE.match(phone):
        raise ScheduleValidationError(
            f"Número de WhatsApp inválido: {raw!r}. Usá el formato internacional, ej. 5491112345678.",
            code=INVALID_RECIPIENTS)
    return phone


def parse_whatsapp_recipients(raw: Any) -> tuple[str, ...]:
    """A list of numbers, or a text with one number per line (commas and semicolons also separate).

    Blank entries are ignored, duplicates collapse (first occurrence wins), at most ``MAX_RECIPIENTS``.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        items: Iterable[Any] = re.split(r"[\n,;]+", raw)
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        raise ScheduleValidationError("Los números de WhatsApp deben ser una lista o un texto.",
                                      code=INVALID_RECIPIENTS)
    recipients: list[str] = []
    for item in items:
        if isinstance(item, str) and not item.strip():
            continue
        phone = normalize_phone(item)
        if phone not in recipients:
            recipients.append(phone)
    if len(recipients) > MAX_RECIPIENTS:
        raise ScheduleValidationError(f"Como máximo {MAX_RECIPIENTS} números de WhatsApp por programación.",
                                      code=INVALID_RECIPIENTS)
    return tuple(recipients)


def recipients_from_config(config: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The WhatsApp recipients stored in a schedule's config (``()`` when none or unreadable)."""
    delivery = (config or {}).get("delivery") if isinstance(config, Mapping) else None
    whatsapp = delivery.get("whatsapp") if isinstance(delivery, Mapping) else None
    recipients = whatsapp.get("recipients") if isinstance(whatsapp, Mapping) else None
    try:
        return parse_whatsapp_recipients(recipients)
    except ScheduleValidationError:
        return ()


def with_whatsapp_recipients(config: Mapping[str, Any] | None, recipients: Iterable[str]) -> dict[str, Any]:
    """A copy of ``config`` with the WhatsApp recipients replaced (the key is dropped when there are none)."""
    result = dict(config or {})
    delivery = dict(result.get("delivery") or {})
    recipients = list(recipients)
    if recipients:
        delivery["whatsapp"] = {**dict(delivery.get("whatsapp") or {}), "recipients": recipients}
    else:
        delivery.pop("whatsapp", None)
    if delivery:
        result["delivery"] = delivery
    else:
        result.pop("delivery", None)
    return result
