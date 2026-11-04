"""Delivery of approved scheduled reports (the scheduler's ``deliver`` phase).

1. Enqueue: approved reviews of scheduled runs, decided within ``max_age``, that have no delivery rows yet
   get one ``pending`` row per recipient configured in their schedule. The window keeps a deploy (or a
   schedule that gains recipients later) from sending old reports. Reviews are never touched: the outbox is
   derived from their state, so approving never depends on the provider and a crash loses nothing.
2. Send: due rows are claimed (guarded, with a lease) and sent with the EXACT approved PDF, read through
   ``pdf_loader`` (the review service verifies its SHA-256). The PDF is uploaded once per review per tick.
3. A transient error retries with exponential backoff up to ``max_attempts``; a permanent one, a missing or
   corrupt PDF, or the last attempt marks the row ``failed``.

No Flask, no SQLAlchemy, no HTTP here: store, channel and PDF loader are injected.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from app.persistence.delivery_states import (
    ERROR_ATTEMPTS_EXHAUSTED, ERROR_PDF_UNAVAILABLE, ERROR_UNEXPECTED,
)

from .delivery_config import recipients_from_config
from .delivery_contracts import DeliveryChannel, DeliveryError, DeliveryJob, DeliveryTransientError
from .pdf_export import safe_slug
from .schedule_rules import as_utc

LOG = logging.getLogger(__name__)

PdfLoader = Callable[[int], bytes]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _env_int(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    try:
        value = int(env.get(name) or default)
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = str(env.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_text(env: Mapping[str, str], name: str, default: str) -> str:
    return (env.get(name) or "").strip() or default


# Defaults of the Meta template approved for scheduled reports; each one is overridden by its env var.
DEFAULT_DELIVERY_ENABLED = True
DEFAULT_TEMPLATE_NAME = "envio_informes_"
DEFAULT_TEMPLATE_LANGUAGE = "es"


@dataclass(frozen=True)
class DeliverySettings:
    enabled: bool = DEFAULT_DELIVERY_ENABLED
    template_name: str = DEFAULT_TEMPLATE_NAME
    template_language: str = DEFAULT_TEMPLATE_LANGUAGE
    max_age_hours: int = 24
    max_attempts: int = 5
    batch_size: int = 50
    lease_seconds: int = 300
    backoff_base_minutes: int = 5
    backoff_max_minutes: int = 120

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DeliverySettings":
        env = os.environ if env is None else env
        base = _env_int(env, "REPORT_DELIVERY_BACKOFF_BASE_MINUTES", 5, 1)
        return cls(
            enabled=_env_flag(env, "REPORT_DELIVERY_ENABLED", DEFAULT_DELIVERY_ENABLED),
            template_name=_env_text(env, "META_WA_REPORT_TEMPLATE", DEFAULT_TEMPLATE_NAME),
            template_language=_env_text(env, "META_WA_REPORT_TEMPLATE_LANG", DEFAULT_TEMPLATE_LANGUAGE),
            max_age_hours=_env_int(env, "REPORT_DELIVERY_MAX_AGE_HOURS", 24, 1),
            max_attempts=_env_int(env, "REPORT_DELIVERY_MAX_ATTEMPTS", 5, 1),
            batch_size=_env_int(env, "REPORT_DELIVERY_BATCH", 50, 1),
            lease_seconds=_env_int(env, "REPORT_DELIVERY_LEASE_SECONDS", 300, 30),
            backoff_base_minutes=base,
            backoff_max_minutes=max(base, _env_int(env, "REPORT_DELIVERY_BACKOFF_MAX_MINUTES", 120, 1)),
        )

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.template_name)

    def backoff(self, attempts: int) -> timedelta:
        """Wait after the ``attempts``-th failed send: base, 2*base, 4*base ... capped at max."""
        exponent = min(max(attempts, 1) - 1, 16)
        return timedelta(minutes=min(self.backoff_base_minutes * 2 ** exponent, self.backoff_max_minutes))


@dataclass
class DeliveryResult:
    queued: dict[int, int] = field(default_factory=dict)   # review id -> rows created
    sent: list[int] = field(default_factory=list)
    retrying: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    lost: list[int] = field(default_factory=list)          # another replica claimed or finished it first
    skipped: bool = False                                  # delivery disabled / not configured


def report_filename(schedule_name: str, period_start: datetime | None) -> str:
    """``informe-<schedule>-<YYYY-MM-DD>.pdf``: ASCII only, safe as a WhatsApp/email attachment name."""
    suffix = f"-{as_utc(period_start):%Y-%m-%d}" if period_start is not None else ""
    return f"informe-{safe_slug(schedule_name)}{suffix}.pdf"


class ReportDeliveryService:
    def __init__(self, store, channel: DeliveryChannel | None, pdf_loader: PdfLoader, *,
                 settings: DeliverySettings | None = None, clock: Callable[[], datetime] | None = None):
        self.store = store
        self.channel = channel
        self.pdf_loader = pdf_loader
        self.settings = settings or DeliverySettings()
        self.clock = clock or _utcnow
        self._warned_inactive = False

    def tick(self, now: datetime | None = None) -> DeliveryResult:
        now = as_utc(now or self.clock())
        result = DeliveryResult()
        if not self.settings.active or self.channel is None:
            if not self._warned_inactive:
                LOG.info("[ReportDelivery] disabled (REPORT_DELIVERY_ENABLED=0 or no template configured)")
                self._warned_inactive = True
            result.skipped = True
            return result
        self.enqueue(now, result)
        self.send_due(now, result)
        return result

    # ── approved review -> pending rows ───────────────────────────────────────────────────────

    def enqueue(self, now: datetime, result: DeliveryResult) -> DeliveryResult:
        since = now - timedelta(hours=self.settings.max_age_hours)
        for candidate in self.store.find_undelivered_approved(since, limit=self.settings.batch_size):
            recipients = recipients_from_config(candidate.schedule_config)
            if not recipients:
                continue
            created = self.store.create_pending(candidate, self.channel.name, recipients, now)
            result.queued[candidate.review_id] = created
            LOG.info("[ReportDelivery] queued review=%s run=%s schedule=%s recipients=%d",
                     candidate.review_id, candidate.report_run_id, candidate.schedule_id, created)
        return result

    # ── pending rows -> provider ──────────────────────────────────────────────────────────────

    def send_due(self, now: datetime, result: DeliveryResult) -> DeliveryResult:
        lease = timedelta(seconds=self.settings.lease_seconds)
        uploads: dict[int, tuple[str, str] | DeliveryError] = {}   # review id -> (attachment id, filename)
        for delivery_id in self.store.due_ids(now, self.settings.batch_size):
            job = self.store.claim(delivery_id, now, lease)
            if job is None:
                result.lost.append(delivery_id)
                continue
            self._send_one(job, now, uploads, result)
        return result

    def _send_one(self, job: DeliveryJob, now: datetime, uploads: dict, result: DeliveryResult) -> None:
        delivery = job.delivery
        try:
            upload = uploads.get(delivery.review_id)
            if upload is None:
                upload = uploads[delivery.review_id] = self._upload(job)
            if isinstance(upload, DeliveryError):
                raise upload
            attachment_id, filename = upload
            message_id = self.channel.send(delivery.recipient, attachment_id, filename, [job.schedule_name])
        except DeliveryError as exc:
            self._failed_attempt(job, now, exc, result)
            return
        except Exception as exc:  # noqa: BLE001 - a bug must not stop the other deliveries
            LOG.exception("[ReportDelivery] unexpected error delivery=%s", delivery.id)
            self._finish(result.failed, delivery.id,
                         self.store.mark_failed(delivery.id, now, ERROR_UNEXPECTED, type(exc).__name__), result)
            return
        self._finish(result.sent, delivery.id, self.store.mark_sent(delivery.id, now, message_id), result)
        LOG.info("[ReportDelivery] sent delivery=%s review=%s channel=%s", delivery.id, delivery.review_id,
                 delivery.channel)

    def _upload(self, job: DeliveryJob) -> tuple[str, str] | DeliveryError:
        """Read the approved PDF and upload it once; an error is cached so the other recipients fail alike."""
        filename = report_filename(job.schedule_name, job.period_start)
        try:
            content = self.pdf_loader(job.delivery.review_id)
        except Exception as exc:  # noqa: BLE001 - missing / corrupt PDF: never send something else
            LOG.error("[ReportDelivery] approved PDF unavailable review=%s: %s", job.delivery.review_id, exc)
            return DeliveryError(f"El PDF aprobado no está disponible: {type(exc).__name__}",
                                 code=ERROR_PDF_UNAVAILABLE)
        try:
            return self.channel.upload(content, filename), filename
        except DeliveryError as exc:
            return exc

    def _failed_attempt(self, job: DeliveryJob, now: datetime, exc: DeliveryError, result: DeliveryResult) -> None:
        delivery = job.delivery
        retry = isinstance(exc, DeliveryTransientError) and delivery.attempts < self.settings.max_attempts
        if retry:
            next_at = now + self.settings.backoff(delivery.attempts)
            self._finish(result.retrying, delivery.id,
                         self.store.mark_retry(delivery.id, now, next_at, exc.code, str(exc)), result)
            LOG.warning("[ReportDelivery] retry delivery=%s attempt=%d next=%s code=%s",
                        delivery.id, delivery.attempts, next_at.isoformat(), exc.code)
            return
        code = ERROR_ATTEMPTS_EXHAUSTED if isinstance(exc, DeliveryTransientError) else exc.code
        self._finish(result.failed, delivery.id, self.store.mark_failed(delivery.id, now, code, str(exc)), result)
        LOG.error("[ReportDelivery] failed delivery=%s attempt=%d code=%s", delivery.id, delivery.attempts, code)

    @staticmethod
    def _finish(bucket: list[int], delivery_id: int, applied: bool, result: DeliveryResult) -> None:
        (bucket if applied else result.lost).append(delivery_id)
