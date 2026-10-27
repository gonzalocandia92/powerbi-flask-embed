"""PostgreSQL-backed persistence for report definitions and durable runs.

This is the only reporting module that talks SQLAlchemy about runs. The pipeline
stays persistence-agnostic (it reports through ``progress.ReportProgress``) and the
worker/executor use this store through a small, explicit surface.

Queue semantics (``report_runs.status``):

    queued -> running -> completed | completed_with_errors | failed
    queued | running -> cancel_requested -> cancelled

A claim is atomic: ``SELECT ... FOR UPDATE SKIP LOCKED`` picks a candidate and a
compare-and-swap ``UPDATE ... WHERE status = 'queued'`` takes it, so two workers can
never both own a run (the CAS also protects databases that ignore row locks, such as
the SQLite used in tests). Every state-changing write after the claim is fenced by
``worker_id`` so a worker that lost its lease cannot overwrite the new owner.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select, update

from app import db
from app.models import AnalyticalReportDefinition, AnalyticalReportQuestion, ReportRun, ReportRunSection

from .contracts import ReportDefinition, ReportQuestion, ReportSection
from .evidence import load_evidence
from .progress import STAGE_PREFLIGHT
from .snapshot import SNAPSHOT_SCHEMA_VERSION, snapshot_from_definition
from .structure_store import StructureStore

LOG = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_COMPLETED_WITH_ERRORS = "completed_with_errors"
STATUS_FAILED = "failed"
STATUS_CANCEL_REQUESTED = "cancel_requested"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES = (STATUS_RUNNING, STATUS_CANCEL_REQUESTED)
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_COMPLETED_WITH_ERRORS, STATUS_FAILED, STATUS_CANCELLED)

DEFAULT_LEASE_SECONDS = 1800
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_NUMBERED_KEY_RE = re.compile(r"^section_(\d+)$")


class ReportRunError(Exception):
    pass


class DefinitionNotRunnableError(ReportRunError):
    """The definition does not exist or is inactive."""


class LeaseLostError(ReportRunError):
    """This worker no longer owns the run (lease expired and recovered by another)."""


class RunCancelledError(ReportRunError):
    """Cancellation was requested; the pipeline should stop at the next safe point."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_valid_question_key(key: object) -> bool:
    return isinstance(key, str) and bool(_KEY_RE.match(key))


def allocate_question_key(used: Iterable[str]) -> str:
    """Next free ``section_NNN`` key. Never reuses a key, even one whose question was removed."""
    taken = set(used)
    highest = max((int(m.group(1)) for k in taken if (m := _NUMBERED_KEY_RE.match(k))), default=0)
    number = highest + 1
    while f"section_{number:03}" in taken:
        number += 1
    return f"section_{number:03}"


class ReportRunStore:
    def __init__(self, *, lease_seconds: int = DEFAULT_LEASE_SECONDS):
        self.lease_seconds = lease_seconds

    # ── definitions ────────────────────────────────────────────────────────

    def all_question_keys(self, definition_id: int) -> set[str]:
        """Every key ever used by a definition, including deactivated questions."""
        rows = db.session.execute(
            select(AnalyticalReportQuestion.key).where(AnalyticalReportQuestion.definition_id == definition_id))
        return {row[0] for row in rows}

    def save_definition(self, definition: ReportDefinition, *, definition_id: int | None = None,
                        created_by_user_id: int | None = None) -> AnalyticalReportDefinition:
        """Create or update a definition. Question identity is ``key``; ``order`` is just position.

        Questions absent from ``definition`` are deactivated, not deleted, so their
        keys stay reserved and old data referring to them keeps its meaning.
        """
        try:
            if definition_id is None:
                model = AnalyticalReportDefinition(created_by_user_id=created_by_user_id)
                db.session.add(model)
            else:
                model = db.session.get(AnalyticalReportDefinition, definition_id)
                if model is None:
                    raise DefinitionNotRunnableError(f"Definition not found: {definition_id}")
            model.name = definition.name
            model.report_id_fk = definition.report_id
            model.strategy = "coordinated" if definition.coordination_enabled else "fixed"
            model.analysis_model_key = definition.analysis_model_key
            model.analysis_service_tier = definition.analysis_service_tier
            model.structure_prompt = definition.structure_prompt
            existing = {question.key: question for question in model.questions}
            incoming = set()
            for item in definition.questions:
                incoming.add(item.key)
                question = existing.get(item.key)
                if question is None:
                    question = AnalyticalReportQuestion(key=item.key)
                    model.questions.append(question)
                question.title = item.title
                question.question = item.question
                question.position = item.order
                question.required_skill_keys_json = list(item.required_skill_keys)
                question.is_active = True
            for key, question in existing.items():
                if key not in incoming:
                    question.is_active = False
            db.session.commit()
            return model
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def definition_to_domain(model: AnalyticalReportDefinition) -> ReportDefinition:
        active = sorted((q for q in model.questions if q.is_active), key=lambda q: (q.position, q.id or 0))
        return ReportDefinition(
            report_id=model.report_id_fk, name=model.name,
            questions=[
                ReportQuestion(key=q.key, title=q.title, question=q.question, order=q.position,
                               required_skill_keys=tuple(q.required_skill_keys_json or ()))
                for q in active
            ],
            analysis_model_key=model.analysis_model_key,
            analysis_service_tier=model.analysis_service_tier,
            coordination_enabled=model.strategy == "coordinated",
            structure_prompt=model.structure_prompt,
        )

    # ── enqueue ────────────────────────────────────────────────────────────

    def enqueue_run(self, definition_id: int, *, requested_by_user_id: int | None = None) -> ReportRun:
        """Snapshot the definition as it is NOW and create a ``queued`` run.

        The snapshot also freezes the structure (prompt + the spec the definition currently has, or the
        default one with a warning if it is missing/stale). This only READS the store: it never calls
        the structure planner, so launching a run never pays to re-interpret a prompt.
        """
        model = db.session.get(AnalyticalReportDefinition, definition_id)
        if model is None or not model.is_active:
            raise DefinitionNotRunnableError(f"Definition not found or inactive: {definition_id}")
        definition = self.definition_to_domain(model)  # re-validates (raises ValueError if empty)
        try:
            run = ReportRun(
                definition_id=model.id, report_id_fk=model.report_id_fk,
                requested_by_user_id=requested_by_user_id, status=STATUS_QUEUED,
                progress_current=0, progress_total=len(definition.questions),
                snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
                definition_snapshot_json=snapshot_from_definition(
                    definition, definition_id=model.id, structure=StructureStore().snapshot_block(model)),
            )
            db.session.add(run)
            db.session.commit()
            return run
        except Exception:
            db.session.rollback()
            raise

    # ── claim / lease ──────────────────────────────────────────────────────

    def claim_next_run(self, worker_id: str, *, now: datetime | None = None, attempts: int = 3) -> ReportRun | None:
        """Atomically move the oldest ``queued`` run to ``running`` for ``worker_id``."""
        for _ in range(attempts):
            now = now or _utcnow()
            try:
                candidate = db.session.execute(
                    select(ReportRun.id).where(ReportRun.status == STATUS_QUEUED)
                    .order_by(ReportRun.created_at.asc(), ReportRun.id.asc())
                    .limit(1).with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if candidate is None:
                    db.session.rollback()
                    return None
                claimed = db.session.execute(
                    update(ReportRun)
                    .where(ReportRun.id == candidate, ReportRun.status == STATUS_QUEUED)
                    .values(status=STATUS_RUNNING, current_stage=STAGE_PREFLIGHT, worker_id=worker_id,
                            started_at=now, heartbeat_at=now,
                            lease_expires_at=now + timedelta(seconds=self.lease_seconds))
                ).rowcount
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
            if claimed == 1:
                db.session.expire_all()
                return db.session.get(ReportRun, candidate)
            now = None  # lost the race: try the next candidate
        return None

    def _fenced_update(self, run_id: str, worker_id: str, **values) -> None:
        """UPDATE only while ``worker_id`` still owns an active run; else the worker is told to stop."""
        now = _utcnow()
        try:
            changed = db.session.execute(
                update(ReportRun)
                .where(ReportRun.id == run_id, ReportRun.worker_id == worker_id,
                       ReportRun.status.in_(ACTIVE_STATUSES))
                .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=self.lease_seconds), **values)
            ).rowcount
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        if changed != 1:
            raise LeaseLostError(f"Worker {worker_id} no longer owns report run {run_id}")

    def heartbeat(self, run_id: str, worker_id: str) -> None:
        self._fenced_update(run_id, worker_id)

    def set_stage(self, run_id: str, worker_id: str, stage: str) -> None:
        self._fenced_update(run_id, worker_id, current_stage=stage)

    def raise_if_cancel_requested(self, run_id: str) -> None:
        status = db.session.execute(select(ReportRun.status).where(ReportRun.id == run_id)).scalar_one_or_none()
        db.session.rollback()  # end the read transaction so the next read sees fresh data
        if status == STATUS_CANCEL_REQUESTED:
            raise RunCancelledError(run_id)

    # ── sections ───────────────────────────────────────────────────────────

    def checkpoint(self, run_id: str) -> None:
        """Cooperative stop point (alias of ``raise_if_cancel_requested``) used before starting an analysis."""
        self.raise_if_cancel_requested(run_id)

    def save_section(self, run_id: str, worker_id: str, section: ReportSection, *, position: int) -> None:
        """Persist one finished analysis immediately (idempotent per ``(run, key)``).

        Ownership is checked IN THE SAME TRANSACTION as the write (row lock on the run), so a worker
        that lost its lease can never persist a late result as the previous owner. Completion order
        is irrelevant: readers order by ``position`` (see ``load_sections``), never by insertion.
        """
        try:
            owned = db.session.execute(
                select(ReportRun.id).where(
                    ReportRun.id == run_id, ReportRun.worker_id == worker_id,
                    ReportRun.status.in_(ACTIVE_STATUSES)).with_for_update()
            ).scalar_one_or_none()
            if owned is None:
                db.session.rollback()
                raise LeaseLostError(f"Worker {worker_id} no longer owns report run {run_id}")
            row = db.session.execute(
                select(ReportRunSection).where(ReportRunSection.run_id == run_id,
                                               ReportRunSection.question_key == section.key)
            ).scalar_one_or_none()
            if row is None:
                sequence = db.session.execute(
                    select(func.count(ReportRunSection.id)).where(ReportRunSection.run_id == run_id)).scalar_one()
                row = ReportRunSection(run_id=run_id, question_key=section.key, sequence=sequence)
                db.session.add(row)
            row.title = section.title
            row.question = section.question
            row.position = position
            row.origin = section.origin
            row.status = "failed" if section.had_error else "completed"
            row.answer = section.answer
            row.failure_reason = section.failure_reason
            row.error_message = section.error_message
            row.recovered_errors_json = list(section.recovered_errors)
            row.dax_query = section.dax_query
            row.tools_called_json = list(section.tools_called)
            row.model_key, row.model, row.provider = section.model_key, section.model, section.provider
            row.service_tier, row.actual_service_tier = section.service_tier, section.actual_service_tier
            row.input_tokens, row.output_tokens = section.input_tokens, section.output_tokens
            row.latency_by_component_ms = dict(section.latency_by_component_ms)
            row.trace_id = section.trace_id
            row.semantic_notes_json = list(section.semantic_notes)
            row.skill_routing_json = dict(section.skill_routing)
            row.purpose = section.purpose
            row.related_section_keys_json = list(section.related_section_keys)
            row.evidence_json = section.evidence.to_json() if section.evidence is not None else None
            row.evidence_schema_version = section.evidence.evidence_schema_version if section.evidence else None
            db.session.flush()
            # ``progress_current`` = analyses FINISHED (not the index of the one being run).
            progress = db.session.execute(
                select(func.count(ReportRunSection.id)).where(
                    ReportRunSection.run_id == run_id, ReportRunSection.origin == "definition")).scalar_one()
            now = _utcnow()
            db.session.execute(
                update(ReportRun).where(ReportRun.id == run_id, ReportRun.worker_id == worker_id)
                .values(progress_current=progress, heartbeat_at=now,
                        lease_expires_at=now + timedelta(seconds=self.lease_seconds)))
            db.session.commit()
        except LeaseLostError:
            raise
        except Exception:
            db.session.rollback()
            raise

    def load_sections(self, run_id: str, *, origin: str | None = None) -> list[ReportSection]:
        """Persisted sections as domain objects. ``ai_usage_events`` stay in the ledger, not here."""
        # Logical order (``position``); ``sequence`` (insertion = completion order) only breaks ties.
        query = select(ReportRunSection).where(ReportRunSection.run_id == run_id).order_by(
            ReportRunSection.position.asc(), ReportRunSection.sequence.asc())
        if origin:
            query = query.where(ReportRunSection.origin == origin)
        return [self.section_to_domain(row, run_id) for row in db.session.execute(query).scalars()]

    @staticmethod
    def section_to_domain(row: ReportRunSection, run_id: str | None = None) -> ReportSection:
        return ReportSection(
            key=row.question_key, title=row.title, question=row.question, answer=row.answer or "",
            had_error=row.status == "failed", error_message=row.error_message,
            failure_reason=row.failure_reason, recovered_errors=list(row.recovered_errors_json or []),
            dax_query=row.dax_query, tools_called=list(row.tools_called_json or []),
            model_key=row.model_key, model=row.model, provider=row.provider,
            service_tier=row.service_tier, actual_service_tier=row.actual_service_tier,
            input_tokens=row.input_tokens, output_tokens=row.output_tokens,
            latency_by_component_ms=dict(row.latency_by_component_ms or {}), trace_id=row.trace_id,
            report_run_id=run_id or row.run_id, semantic_notes=list(row.semantic_notes_json or []),
            skill_routing=dict(row.skill_routing_json or {}),
            origin=row.origin, purpose=row.purpose,
            related_section_keys=tuple(row.related_section_keys_json or ()),
            evidence=load_evidence(row.evidence_json, row.evidence_schema_version),
        )

    # ── completion ─────────────────────────────────────────────────────────

    def finish_run(self, run_id: str, worker_id: str, *, status: str, result: dict | None = None,
                   error_code: str | None = None, error_message: str | None = None) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Not a terminal status: {status}")
        now = _utcnow()
        try:
            changed = db.session.execute(
                update(ReportRun)
                .where(ReportRun.id == run_id, ReportRun.worker_id == worker_id,
                       ReportRun.status.in_(ACTIVE_STATUSES))
                .values(status=status, result_json=result, error_code=error_code, error_message=error_message,
                        completed_at=now, heartbeat_at=now, lease_expires_at=None, worker_id=None)
            ).rowcount
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        if changed != 1:
            raise LeaseLostError(f"Worker {worker_id} no longer owns report run {run_id}")

    def request_cancel(self, run_id: str) -> str | None:
        """Cancel a queued run immediately, or flag a running one. Returns the new status (None if not found/terminal)."""
        now = _utcnow()
        try:
            if db.session.execute(
                update(ReportRun).where(ReportRun.id == run_id, ReportRun.status == STATUS_QUEUED)
                .values(status=STATUS_CANCELLED, cancel_requested_at=now, completed_at=now)
            ).rowcount:
                db.session.commit()
                return STATUS_CANCELLED
            if db.session.execute(
                update(ReportRun).where(ReportRun.id == run_id, ReportRun.status == STATUS_RUNNING)
                .values(status=STATUS_CANCEL_REQUESTED, cancel_requested_at=now)
            ).rowcount:
                db.session.commit()
                return STATUS_CANCEL_REQUESTED
            db.session.rollback()
        except Exception:
            db.session.rollback()
            raise
        run = db.session.get(ReportRun, run_id)
        return STATUS_CANCEL_REQUESTED if run is not None and run.status == STATUS_CANCEL_REQUESTED else None

    def recover_stale_runs(self, *, now: datetime | None = None) -> list[str]:
        """Close runs whose worker disappeared (expired lease).

        Deliberately NOT re-queued: re-running a whole interrupted run would repeat
        paid model calls. Persisted sections are kept as-is, so a future "resume"
        can skip them (``ReportGenerator`` already accepts ``completed_sections``).
        """
        now = now or _utcnow()
        stale = db.session.execute(
            select(ReportRun.id, ReportRun.status).where(
                ReportRun.status.in_(ACTIVE_STATUSES), ReportRun.lease_expires_at.isnot(None),
                ReportRun.lease_expires_at < now)
        ).all()
        recovered = []
        try:
            for run_id, status in stale:
                cancelled = status == STATUS_CANCEL_REQUESTED
                changed = db.session.execute(
                    update(ReportRun)
                    .where(ReportRun.id == run_id, ReportRun.status == status, ReportRun.lease_expires_at < now)
                    .values(
                        status=STATUS_CANCELLED if cancelled else STATUS_FAILED,
                        error_code=None if cancelled else "worker_interrupted",
                        error_message=None if cancelled else
                        "La ejecución fue interrumpida y no se repitió para evitar doble facturación. "
                        "Los análisis ya completados se conservaron.",
                        completed_at=now, worker_id=None, lease_expires_at=None)
                ).rowcount
                if changed:
                    recovered.append(run_id)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        if recovered:
            LOG.warning("Recovered interrupted report runs: %s", recovered)
        return recovered

    def get(self, run_id: str) -> ReportRun | None:
        return db.session.get(ReportRun, run_id)
