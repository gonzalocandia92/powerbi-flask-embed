"""JSON shapes of report deliveries (pure: no Flask, no database).

Phone numbers leave masked: the review screen only needs to tell recipients apart, not to read them whole.
"""
from __future__ import annotations

from typing import Any

from app.services.reporting.delivery_contracts import DeliveryView


def mask_recipient(recipient: str) -> str:
    """``5493705054711`` -> ``•••••••••4711`` (the last 4 digits stay; short values are fully masked)."""
    value = recipient or ""
    visible = 4 if len(value) > 6 else 0
    return "•" * (len(value) - visible) + value[len(value) - visible:]


def delivery_payload(delivery: DeliveryView) -> dict[str, Any]:
    iso = lambda value: value.isoformat() if value is not None else None  # noqa: E731
    return {
        "id": delivery.id, "channel": delivery.channel, "recipient": mask_recipient(delivery.recipient),
        "status": delivery.status, "attempts": delivery.attempts,
        "next_attempt_at": iso(delivery.next_attempt_at), "sent_at": iso(delivery.sent_at),
        "error": ({"code": delivery.last_error_code, "message": delivery.last_error_message}
                  if delivery.last_error_code else None),
        "created_at": iso(delivery.created_at), "updated_at": iso(delivery.updated_at),
    }
