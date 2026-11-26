"""Public contract of a KLARA chat turn: what the browser may see of a result or of a failure.

Single source of truth shared by the synchronous ``POST /chat`` route and the asynchronous chat worker
(``app/services/chat_runs``), so both paths always answer with the same payload, codes and HTTP statuses.
Pure mapping: no Flask, no logging (the caller logs ``unexpected`` failures with its own context).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.persistence import chat_run_states as states
from app.services import chatbot_service

GENERIC_ERROR_MESSAGE = "Error al procesar la consulta."

# Optional identity fields of the effective model, copied only when present.
_OPTIONAL_RESULT_KEYS = ("model_key", "provider", "actual_model")


@dataclass(frozen=True)
class PublicChatError:
    """A failure as the browser sees it.

    ``code`` always identifies the failure (stored in ``chat_runs.error_code``); ``expose_code`` says whether
    the synchronous route includes it in its body (kept exactly as before). ``unexpected`` failures are the
    ones the caller must log with a traceback.
    """

    code: str
    message: str
    http_status: int
    diagnostic_id: Optional[str] = None
    expose_code: bool = False
    unexpected: bool = False

    def body(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {"error": self.message}
        if self.expose_code:
            body["code"] = self.code
        if self.diagnostic_id:
            body["diagnostic_id"] = self.diagnostic_id
        return body


def public_error(exc: BaseException) -> PublicChatError:
    """Map an exception of a chat turn to its public form. Internal details never leave this function."""
    diagnostic_id = getattr(exc, "diagnostic_id", None)
    if isinstance(exc, chatbot_service.ChatbotNotFoundError):
        return PublicChatError(states.ERROR_NOT_FOUND, str(exc), 404, diagnostic_id)
    if isinstance(exc, chatbot_service.ChatbotLimitExceededError):
        return PublicChatError(states.ERROR_LIMIT_EXCEEDED, str(exc), 403, diagnostic_id)
    if isinstance(exc, chatbot_service.ChatbotModelNotAllowedError):
        return PublicChatError(states.ERROR_MODEL_NOT_ALLOWED, str(exc), 400, diagnostic_id, expose_code=True)
    if isinstance(exc, chatbot_service.ChatbotDisabledError):
        return PublicChatError(states.ERROR_CHATBOT_DISABLED, str(exc), 403, diagnostic_id, expose_code=True)
    if isinstance(exc, chatbot_service.ChatbotUnavailableError):
        return PublicChatError(states.ERROR_CHATBOT_UNAVAILABLE, str(exc), 503, diagnostic_id, expose_code=True)
    if isinstance(exc, chatbot_service.ChatbotServiceError):
        # Its message may carry SQL, identifiers or provider errors: never shown.
        return PublicChatError(states.ERROR_PROCESSING, GENERIC_ERROR_MESSAGE, 500, diagnostic_id, unexpected=True)
    return PublicChatError(states.ERROR_PROCESSING, GENERIC_ERROR_MESSAGE, 500, unexpected=True)


def public_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """The answer of a successful turn without internal identifiers (report, dataset, workspace)."""
    payload = {
        "answer": result["answer"],
        "conversation_id": result["conversation_id"],
        "tool_rounds": result.get("tool_rounds", 0),
        "dax_query": result.get("dax_query"),
    }
    if result.get("diagnostic_id"):
        payload["diagnostic_id"] = result["diagnostic_id"]
    for key in _OPTIONAL_RESULT_KEYS:
        if result.get(key) is not None:
            payload[key] = result[key]
    return payload
