"""Retroactively classify ``ChatSession.channel`` for historical conversations.

Run once after deploying the migration that adds ``chat_sessions.channel``
(and again any time later, safely — it is idempotent and only ever touches
rows where ``channel IS NULL``):

    python backfill_chat_channels.py            # applies the classification
    python backfill_chat_channels.py --dry-run  # reports counts, writes nothing

Criteria (evidence-based; a session is left NULL — "Sin identificar" in the
admin UI — rather than guessed):

 1. Direct evidence first: ``WhatsAppContact.conversation_id == session.id``
    unambiguously identifies that exact session as WhatsApp, independent of
    anything recorded on ``AIUsageEvent``.

 2. ``AIUsageEvent`` evidence linked to the session: any event with
    ``source_type == "whatsapp"`` (or ``metadata_json`` carrying
    ``execution_source``/``source`` == ``"whatsapp"``) is treated as
    conclusive WhatsApp evidence and always wins, because...

 3. ...``source_type == "chat"`` is NOT reliable evidence of the web widget
    on its own. Git history shows WhatsApp shipped 2026-07-10
    (``ec174f2``) but the chatbot pipeline only started passing
    ``source="whatsapp"`` (and therefore a correct ``source_type``) on
    2026-09-23 (``86af869``). Every WhatsApp interaction in that ~2.5 month
    window was recorded with ``source_type="chat"`` — the exact
    misclassification this task explicitly warns against. So "chat"
    evidence is only trusted once WhatsApp evidence has been ruled out
    (step 1 and 2), or the session predates WhatsApp's existence entirely
    (created before 2026-07-10, when only klara_chat was possible).

 4. Anything left without qualifying evidence stays NULL.

This script intentionally does NOT touch ``AIUsageEvent`` or any billing
data — only ``ChatSession.channel``.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from app import create_app, db
from app.models import AIUsageEvent, ChatSession, WhatsAppContact

# First commit shipping WhatsApp support (git log --diff-filter=A -- app/routes/whatsapp.py).
WHATSAPP_LAUNCH_AT = datetime(2026, 7, 10, tzinfo=timezone.utc)
BATCH_SIZE = 500


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _session_whatsapp_evidence(session_id: int) -> bool:
    events = AIUsageEvent.query.filter(AIUsageEvent.session_id == session_id).all()
    for event in events:
        if event.source_type == "whatsapp":
            return True
        metadata = event.metadata_json or {}
        if metadata.get("execution_source") == "whatsapp" or metadata.get("source") == "whatsapp":
            return True
    return False


def _session_chat_evidence(session_id: int) -> bool:
    return (
        AIUsageEvent.query
        .filter(AIUsageEvent.session_id == session_id, AIUsageEvent.source_type == "chat")
        .first() is not None
    )


def classify_session(session: ChatSession, whatsapp_session_ids: set[int]) -> str | None:
    if session.id in whatsapp_session_ids:
        return "whatsapp"
    if _session_whatsapp_evidence(session.id):
        return "whatsapp"

    created_at = _as_utc(session.created_at)
    if created_at is not None and created_at < WHATSAPP_LAUNCH_AT:
        return "klara_chat"
    if _session_chat_evidence(session.id):
        return "klara_chat"
    return None


def run(dry_run: bool = False) -> None:
    app = create_app()
    with app.app_context():
        whatsapp_session_ids = {
            row[0] for row in (
                db.session.query(WhatsAppContact.conversation_id)
                .filter(WhatsAppContact.conversation_id.isnot(None))
                .all()
            )
        }

        totals = {"whatsapp": 0, "klara_chat": 0, "unknown": 0, "already_set": 0}
        last_id = 0
        while True:
            batch = (
                ChatSession.query
                .filter(ChatSession.id > last_id)
                .order_by(ChatSession.id)
                .limit(BATCH_SIZE)
                .all()
            )
            if not batch:
                break
            for session in batch:
                last_id = session.id
                if session.channel is not None:
                    totals["already_set"] += 1
                    continue
                channel = classify_session(session, whatsapp_session_ids)
                if channel is None:
                    totals["unknown"] += 1
                    continue
                session.channel = channel
                totals[channel] += 1

            if dry_run:
                db.session.rollback()
            else:
                db.session.commit()
            print(f"...procesado hasta chat_sessions.id={last_id}")

        print("Backfill de channel terminado.")
        print(f"  klara_chat classified: {totals['klara_chat']}")
        print(f"  whatsapp classified:   {totals['whatsapp']}")
        print(f"  left as unknown/NULL:  {totals['unknown']}")
        print(f"  already had a channel: {totals['already_set']}")
        if dry_run:
            print("(--dry-run: no se escribió nada en la base)")


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
