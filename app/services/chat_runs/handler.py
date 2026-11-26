"""Default ``ChatTurnHandler``: the same ``chatbot_service`` turn the synchronous ``/chat`` route runs.

Must be called inside a Flask app context (the service reads ``current_app.config`` and the database).
"""
from __future__ import annotations

from typing import Any, Dict

from .contracts import ChatRunRequest, ProgressSink

# Same fallback the synchronous route ends up with when the client IP is unknown.
UNKNOWN_USER_KEY = "public:unknown"


class ChatbotServiceTurnHandler:
    async def handle(self, request: ChatRunRequest, progress: ProgressSink) -> Dict[str, Any]:
        from app.services import chatbot_service  # lazy: keeps the queue package light to import

        return await chatbot_service.procesar_interaccion_completa(
            request.question,
            slug=request.slug,
            user_key=request.user_key or UNKNOWN_USER_KEY,
            conversation_id=request.conversation_id,
            reset_history=request.reset_history,
            source=request.source,
            model_key=request.model_key,
            is_staff=request.is_staff,
            owner_hash=request.owner_hash,
            progress=progress,
        )
