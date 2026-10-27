"""Database-enforced immutability rules for report reviews.

Why triggers: the review service moves a review between statuses with a conditional Core
``UPDATE`` (atomic under concurrency), which does not fire ORM ``before_update`` events. A
rule that must hold "even if the code is wrong" therefore lives in the database. SQLite
supports triggers too, so the exact same contract is verified by the unit tests.

Rules (``tests/test_report_review_models.py`` exercises every one):

* ``report_reviews``      identity never changes; an artifact reference or actor, once set, never
                          changes to another value; status follows ``TRANSITIONS``; a decided review
                          is frozen; only a draft can be deleted.
* ``report_review_events`` strictly append-only.
* ``report_run_artifacts`` a row referenced by a review can be neither updated nor deleted.

``ON DELETE SET NULL`` is executed by PostgreSQL as an ``UPDATE`` and fires these triggers, so
"actor/user column becomes NULL" is always allowed (the audit trail outlives its users).

This module only builds SQL strings. It knows column NAMES but imports no model, and the
column lists are checked against the real tables by a test, so a new column cannot be added
without deciding whether it is frozen.

Alembic migrations keep their own frozen copy of the SQL (a migration must not change when
this module does); a future rule change = new migration that re-creates the triggers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy import event

from .review_states import DECIDED_STATUSES, TRANSITIONS

REVIEWS = "report_reviews"
EVENTS = "report_review_events"
ARTIFACTS = "report_run_artifacts"

# ── column classification (verified against the real tables by a test) ───────────────────────
REVIEW_IDENTITY = ("id", "report_run_id", "round", "created_at")
REVIEW_PINNED = ("pdf_artifact_id", "pdf_sha256", "final_report_artifact_id", "html_artifact_id")
REVIEW_ACTORS = ("submitted_by_user_id", "decided_by_user_id")
REVIEW_EDITABLE_UNTIL_DECIDED = ("status", "submitted_at", "decided_at", "rejection_reason", "updated_at")
REVIEW_COLUMNS = REVIEW_IDENTITY + REVIEW_PINNED + REVIEW_ACTORS + REVIEW_EDITABLE_UNTIL_DECIDED

EVENT_FROZEN = ("id", "review_id", "event_type", "previous_status", "new_status", "comment", "created_at")
EVENT_JSON = ("metadata_json",)
EVENT_ACTORS = ("actor_user_id",)
EVENT_COLUMNS = EVENT_FROZEN + EVENT_JSON + EVENT_ACTORS

_DIALECTS = ("postgresql", "sqlite")


def _lit(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _differs(dialect: str, column: str, *, json: bool = False) -> str:
    """``NEW.col`` differs from ``OLD.col``, NULL-safe. PostgreSQL ``json`` has no equality: compare text."""
    new, old = f"NEW.{column}", f"OLD.{column}"
    if dialect == "postgresql":
        if json:
            new, old = f"{new}::text", f"{old}::text"
        return f"{new} IS DISTINCT FROM {old}"
    return f"{new} IS NOT {old}"


def _any_differs(dialect: str, columns, *, json: bool = False) -> str:
    return " OR ".join(_differs(dialect, c, json=json) for c in columns)


def _changed_to_other_value(dialect: str, column: str) -> str:
    """Setting a column to NULL is always allowed (FK ``SET NULL``); any other change is not."""
    return f"({_differs(dialect, column)} AND NEW.{column} IS NOT NULL)"


# A rule is (SQL predicate over OLD/NEW, message). The message becomes the error text.
Rule = tuple[str, str]


def _review_update_rules(dialect: str) -> list[Rule]:
    pinned = " OR ".join(f"(OLD.{c} IS NOT NULL AND {_differs(dialect, c)})" for c in REVIEW_PINNED)
    actors = " OR ".join(f"(OLD.{c} IS NOT NULL AND {_changed_to_other_value(dialect, c)})" for c in REVIEW_ACTORS)
    allowed = " OR ".join(
        f"(OLD.status = '{src}' AND NEW.status IN ({_lit(dst)}))" for src, dst in TRANSITIONS.items() if dst)
    transition = f"NEW.status <> OLD.status AND NOT ({allowed})" if allowed else "NEW.status <> OLD.status"
    frozen = _any_differs(dialect, REVIEW_IDENTITY + REVIEW_PINNED + REVIEW_EDITABLE_UNTIL_DECIDED)
    decided_actors = " OR ".join(_changed_to_other_value(dialect, c) for c in REVIEW_ACTORS)
    return [
        (_any_differs(dialect, REVIEW_IDENTITY), "report_reviews: identity columns are immutable"),
        (pinned, "report_reviews: a pinned artifact reference is immutable"),
        (actors, "report_reviews: an actor cannot be reassigned"),
        (transition, "report_reviews: status transition not allowed"),
        (f"OLD.status IN ({_lit(DECIDED_STATUSES)}) AND (({frozen}) OR {decided_actors})",
         "report_reviews: a decided review is immutable"),
    ]


def _review_delete_rules(dialect: str) -> list[Rule]:
    return [("OLD.status <> 'draft'", "report_reviews: only a draft can be deleted")]


def _event_update_rules(dialect: str) -> list[Rule]:
    changed = " OR ".join([
        _any_differs(dialect, EVENT_FROZEN),
        _any_differs(dialect, EVENT_JSON, json=True),
        *(_changed_to_other_value(dialect, c) for c in EVENT_ACTORS),
    ])
    return [(changed, "report_review_events: append-only, rows cannot be modified")]


def _event_delete_rules(dialect: str) -> list[Rule]:
    return [("TRUE", "report_review_events: append-only, rows cannot be deleted")]


def _referenced_artifact() -> str:
    return (f"EXISTS (SELECT 1 FROM {REVIEWS} r WHERE r.pdf_artifact_id = OLD.id "
            "OR r.html_artifact_id = OLD.id OR r.final_report_artifact_id = OLD.id)")


def _artifact_update_rules(dialect: str) -> list[Rule]:
    return [(_referenced_artifact(), "report_run_artifacts: an artifact under review is immutable")]


def _artifact_delete_rules(dialect: str) -> list[Rule]:
    return [(_referenced_artifact(), "report_run_artifacts: an artifact under review cannot be deleted")]


@dataclass(frozen=True)
class _Guard:
    table: str
    event: str  # UPDATE | DELETE
    rules: Callable[[str], list[Rule]]

    @property
    def name(self) -> str:
        return f"trg_{self.table}_guard_{self.event.lower()}"

    @property
    def function(self) -> str:
        return f"fn_{self.table}_guard_{self.event.lower()}"


_GUARDS = (
    _Guard(REVIEWS, "UPDATE", _review_update_rules),
    _Guard(REVIEWS, "DELETE", _review_delete_rules),
    _Guard(EVENTS, "UPDATE", _event_update_rules),
    _Guard(EVENTS, "DELETE", _event_delete_rules),
    _Guard(ARTIFACTS, "UPDATE", _artifact_update_rules),
    _Guard(ARTIFACTS, "DELETE", _artifact_delete_rules),
)
TABLES = (REVIEWS, EVENTS, ARTIFACTS)


def _create(dialect: str, guard: _Guard) -> list[str]:
    rules = guard.rules(dialect)
    if dialect == "sqlite":
        body = "\n".join(f"  SELECT CASE WHEN ({predicate}) THEN RAISE(ABORT, '{message}') END;"
                         for predicate, message in rules)
        return [
            f"DROP TRIGGER IF EXISTS {guard.name}",
            f"CREATE TRIGGER {guard.name} BEFORE {guard.event} ON {guard.table} FOR EACH ROW\nBEGIN\n{body}\nEND",
        ]
    row = "NEW" if guard.event == "UPDATE" else "OLD"
    body = "\n".join(f"  IF {predicate} THEN RAISE EXCEPTION '{message}' USING ERRCODE = '23000'; END IF;"
                     for predicate, message in rules)
    return [
        f"CREATE OR REPLACE FUNCTION {guard.function}() RETURNS trigger AS $$\nBEGIN\n{body}\n  RETURN {row};\nEND;\n$$ LANGUAGE plpgsql",
        f"DROP TRIGGER IF EXISTS {guard.name} ON {guard.table}",
        f"CREATE TRIGGER {guard.name} BEFORE {guard.event} ON {guard.table} FOR EACH ROW EXECUTE FUNCTION {guard.function}()",
    ]


def _drop(dialect: str, guard: _Guard) -> list[str]:
    if dialect == "sqlite":
        return [f"DROP TRIGGER IF EXISTS {guard.name}"]
    return [f"DROP TRIGGER IF EXISTS {guard.name} ON {guard.table}", f"DROP FUNCTION IF EXISTS {guard.function}()"]


def create_statements(dialect: str, table: str | None = None) -> list[str]:
    """Idempotent statements that (re)create the triggers living on ``table`` (all tables if None).

    Unsupported dialects return ``[]``; the database is then not protected, only the service is.
    """
    if dialect not in _DIALECTS:
        return []
    return [sql for g in _GUARDS if table in (None, g.table) for sql in _create(dialect, g)]


def drop_statements(dialect: str, table: str | None = None) -> list[str]:
    if dialect not in _DIALECTS:
        return []
    return [sql for g in _GUARDS if table in (None, g.table) for sql in _drop(dialect, g)]


def install(*, reviews, events) -> None:
    """Create the triggers whenever ``db.create_all()`` creates the review tables.

    ``reviews`` is the last table needed by the artifact triggers (they read it), so both the
    review and the artifact triggers are created right after it. Production uses the Alembic
    migration instead; this keeps test and dev databases equivalent.
    """
    def run(*tables):
        def listener(target, connection, **kwargs):
            for table in tables:
                for statement in create_statements(connection.dialect.name, table):
                    connection.exec_driver_sql(statement)
        return listener

    event.listen(reviews, "after_create", run(REVIEWS, ARTIFACTS))
    event.listen(events, "after_create", run(EVENTS))
