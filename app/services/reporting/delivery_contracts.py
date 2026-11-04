"""Values and ports of the report-delivery workflow (pure: no Flask, no SQLAlchemy, no HTTP).

``ReportDeliveryService`` talks to the database through ``DeliveryStore`` and to a provider through a
``DeliveryChannel``; both arrive by injection, so another channel (email) is one more implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class DeliveryCandidate:
    """An approved review of a scheduled run that has no delivery rows yet."""

    review_id: int
    report_run_id: str
    schedule_id: int
    schedule_name: str
    schedule_config: Mapping[str, Any] = field(default_factory=dict)
    period_start: datetime | None = None


@dataclass(frozen=True)
class DeliveryView:
    id: int
    review_id: int
    report_run_id: str
    schedule_id: int | None
    channel: str
    recipient: str
    status: str
    attempts: int
    next_attempt_at: datetime | None
    provider_message_id: str | None
    last_error_code: str | None
    last_error_message: str | None
    sent_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class DeliveryJob:
    """A claimed delivery plus what is needed to send it (read from its schedule when it was claimed)."""

    delivery: DeliveryView
    schedule_name: str
    period_start: datetime | None = None


class DeliveryError(Exception):
    code = "delivery_error"

    def __init__(self, message: str = "", *, code: str | None = None):
        super().__init__(message or self.code)
        if code is not None:
            self.code = code


class DeliveryTransientError(DeliveryError):
    """Worth retrying later (provider down, rate limited, network)."""

    code = "provider_transient"


class DeliveryPermanentError(DeliveryError):
    """Retrying cannot fix it (rejected by the provider: bad number, template mismatch...)."""

    code = "provider_rejected"


class DeliveryChannel(Protocol):
    """A provider. ``upload`` once per document, then ``send`` it to each recipient.

    Both raise ``DeliveryTransientError`` / ``DeliveryPermanentError``.
    """

    name: str

    def upload(self, content: bytes, filename: str) -> str: ...

    def send(self, recipient: str, attachment_id: str, filename: str, params: Sequence[str]) -> str | None: ...
