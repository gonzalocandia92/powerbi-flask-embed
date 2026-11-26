"""Single source of truth for the asynchronous KLARA chat (``chat_runs``) vocabulary.

Pure constants: no imports, no logic. Deliberately NOT enforced by ``CHECK`` constraints: new stages and
error codes are expected and must not need a migration. ``ChatRunStore`` validates every status and stage
it writes against these tuples, and the front translates stage / error codes to text (an unknown code is
shown with a generic message).

Lifecycle of one chat run (one question of one owner)::

    queued --claim--> running --> completed | failed
    queued --(waited longer than the queue timeout)--> expired
    queued --cancel--> cancelled
    running --cancel--> cancel_requested --> cancelled
    running | cancel_requested --(lease expired)--> failed (worker_lost) | cancelled

An interrupted run is never re-queued: re-running it would repeat paid model calls.
"""

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
STATUS_CANCEL_REQUESTED = "cancel_requested"
STATUS_CANCELLED = "cancelled"

STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED, STATUS_EXPIRED,
            STATUS_CANCEL_REQUESTED, STATUS_CANCELLED)
# Owned by a worker (lease / heartbeat apply).
ACTIVE_STATUSES = (STATUS_RUNNING, STATUS_CANCEL_REQUESTED)
# Not finished yet: may still spend tokens, and count against the open-runs limits.
OPEN_STATUSES = (STATUS_QUEUED,) + ACTIVE_STATUSES
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_EXPIRED, STATUS_CANCELLED)
# Terminal statuses a worker may write when it finishes a run it owns (``expired`` is only set by the queue).
WORKER_FINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)

# ── stages (``chat_runs.stage``): what the agent is doing, independent of the status ─────────────
STAGE_QUEUED = "queued"
STAGE_PREPARING = "preparing"
STAGE_SELECTING_SKILLS = "selecting_skills"
STAGE_READING_SCHEMA = "reading_schema"
STAGE_QUERYING_DATA = "querying_data"
STAGE_WRITING_ANSWER = "writing_answer"

STAGES = (STAGE_QUEUED, STAGE_PREPARING, STAGE_SELECTING_SKILLS, STAGE_READING_SCHEMA, STAGE_QUERYING_DATA,
          STAGE_WRITING_ANSWER)

# ── public error codes stored in ``error_code`` (safe to return to the browser) ──────────────────
ERROR_CHATBOT_DISABLED = "chatbot_disabled"
ERROR_CHATBOT_UNAVAILABLE = "chatbot_unavailable"
ERROR_LIMIT_EXCEEDED = "limit_exceeded"
ERROR_MODEL_NOT_ALLOWED = "model_not_allowed"
ERROR_NOT_FOUND = "not_found"
ERROR_PROCESSING = "processing_error"
ERROR_WORKER_LOST = "worker_lost"
ERROR_EXPIRED = "expired"
ERROR_CANCELLED = "cancelled"

ERROR_CODES = (ERROR_CHATBOT_DISABLED, ERROR_CHATBOT_UNAVAILABLE, ERROR_LIMIT_EXCEEDED, ERROR_MODEL_NOT_ALLOWED,
               ERROR_NOT_FOUND, ERROR_PROCESSING, ERROR_WORKER_LOST, ERROR_EXPIRED, ERROR_CANCELLED)

# ── origin of the run (``chat_runs.source``), same strings ``chatbot_service`` already uses ─────
SOURCE_CHAT = "chat"
