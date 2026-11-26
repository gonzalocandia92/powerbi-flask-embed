"""Public view of a chat run: the ONLY shape the browser ever receives about a run.

Never includes the owner, the question, the slug, the report, the worker or any internal identifier: just the
state, what the agent is doing, and either the public answer (``result_json`` already holds only the public
payload) or a public error.
"""
from __future__ import annotations

from typing import Any, Dict

from app.persistence.chat_run_states import (
    ERROR_PROCESSING, OPEN_STATUSES, STATUS_COMPLETED, TERMINAL_STATUSES,
)

GENERIC_ERROR_MESSAGE = "Error al procesar la consulta."


def run_payload(run, *, poll_after_ms: int) -> Dict[str, Any]:
    body: Dict[str, Any] = {"run_id": run.id, "status": run.status, "stage": run.stage}
    if run.status in OPEN_STATUSES:
        body["poll_after_ms"] = poll_after_ms
    elif run.status == STATUS_COMPLETED:
        body["result"] = dict(run.result_json or {})
    elif run.status in TERMINAL_STATUSES:
        error: Dict[str, Any] = {"code": run.error_code or ERROR_PROCESSING,
                                 "message": run.error_message or GENERIC_ERROR_MESSAGE}
        if run.diagnostic_id:
            error["diagnostic_id"] = run.diagnostic_id
        body["error"] = error
    return body
