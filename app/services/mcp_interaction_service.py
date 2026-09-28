"""Best-effort persistence of the question behind an MCP interaction.

See ``app.models.McpInteraction`` for why only the question is stored (never
an answer, cost, tokens or model — KLARA never computes those for MCP; the
host client executes DAX directly against Power BI and may produce the final
answer with a different model entirely). This module owns the
correlation/deduplication strategy for turning several broker calls that
belong to the same logical question into a single persisted row.

Deduplication strategy and its limitation
------------------------------------------
A single user question can drive multiple calls into the ``/internal/mcp``
broker (list-skills, select-skills, relevant-schema, sometimes more than
once). ``mcp-aklara`` (the external MCP host that calls this broker) does not
send any explicit request/execution/trace/correlation id today — the only
stable identifier available on every call is the OAuth session's
``public_id``, which is long-lived (one login, potentially many questions).

We were asked to prefer explicit correlation over temporal deduplication,
but no explicit id exists to correlate on, so the documented fallback is: a
question is deduplicated against the same OAuth session when the normalized
text matches exactly and the previous occurrence was recorded within a short
recency window (``_DEDUP_WINDOW``). This safely collapses the
"select_skills -> relevant_schema" fan-out for one reasoning turn, at the
cost of merging two genuinely identical questions asked far apart in the
same session into one row only within that window (acceptable: outside the
window they are recorded as separate interactions, which is the more common
and more useful behaviour for an audit screen).

If ``mcp-aklara`` is ever updated to pass an explicit request id, replace the
(session, normalized question, window) key below with that id and drop the
time window entirely.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import db
from app.models import McpInteraction

LOG = logging.getLogger(__name__)

_DEDUP_WINDOW = timedelta(minutes=10)


def _normalize(question: Optional[str]) -> str:
    if not isinstance(question, str):
        return ""
    return " ".join(question.split()).strip()


def record_mcp_question(
    *,
    question: Optional[str],
    oauth_session,
    grant,
    source_tool: str,
) -> Optional[McpInteraction]:
    """Persist one ``McpInteraction`` per logical question, best-effort.

    Never raises and never rolls back the caller's own pending work: it runs
    inside its own SAVEPOINT, so a failure here (e.g. a transient DB error)
    only discards this optional bookkeeping, never the broker operation the
    caller is otherwise committing. Callers should invoke this after the
    real operation succeeded, before their own ``db.session.commit()``.
    """
    normalized = _normalize(question)
    if not normalized or oauth_session is None or grant is None:
        return None

    try:
        with db.session.begin_nested():
            cutoff = datetime.now(timezone.utc) - _DEDUP_WINDOW
            existing = (
                McpInteraction.query
                .filter(
                    McpInteraction.mcp_session_public_id == oauth_session.public_id,
                    McpInteraction.question == normalized,
                    McpInteraction.created_at >= cutoff,
                )
                .order_by(McpInteraction.id.desc())
                .first()
            )
            if existing is not None:
                return existing

            config = grant.config
            report_id = (
                getattr(config, "skill_report_id_fk", None)
                or getattr(config, "credential_report_id_fk", None)
            )
            interaction = McpInteraction(
                question=normalized,
                empresa_id=grant.empresa_id,
                report_id_fk=report_id,
                user_id=oauth_session.user_id,
                mcp_session_public_id=oauth_session.public_id,
                grant_public_id=grant.public_id,
                source_tool=source_tool,
            )
            db.session.add(interaction)
            db.session.flush()
        return interaction
    except Exception:
        LOG.exception("[MCP] Failed to record MCP interaction question (best-effort, non-fatal)")
        return None
