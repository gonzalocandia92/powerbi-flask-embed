"""Single source of truth for the report-delivery vocabulary (distribution of approved reports).

Pure constants: no imports, no logic. Deliberately NOT enforced by ``CHECK`` constraints: new channels,
statuses and error codes are expected, and must not need a migration. ``DeliveryStore`` validates every
status it writes against ``STATUSES``.

Lifecycle of one delivery (one approved review x one channel x one recipient)::

    pending --claim--> sending --provider accepted--> sent
       ^                  |  \\--permanent error / attempts exhausted--> failed
       \\--transient error-/   (a ``sending`` row whose lease expired is claimed again)
"""

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"

STATUSES = (STATUS_PENDING, STATUS_SENDING, STATUS_SENT, STATUS_FAILED)
FINAL_STATUSES = (STATUS_SENT, STATUS_FAILED)

CHANNEL_WHATSAPP = "whatsapp"
CHANNELS = (CHANNEL_WHATSAPP,)

# ── error codes stored in ``last_error_code`` (not constrained in the database) ───────────────
ERROR_PDF_UNAVAILABLE = "pdf_unavailable"           # the approved PDF is missing or failed its integrity check
ERROR_PROVIDER_TRANSIENT = "provider_transient"     # e.g. Meta 5xx / rate limit / network: retried
ERROR_PROVIDER_REJECTED = "provider_rejected"       # e.g. Meta 4xx (template params, invalid number): final
ERROR_ATTEMPTS_EXHAUSTED = "attempts_exhausted"
ERROR_UNEXPECTED = "unexpected_error"
