"""Persistence of the compiled structure on ``AnalyticalReportDefinition`` (the only SQLAlchemy here).

The store keeps a spec, its input hash, version and compile metadata. It knows nothing about how
the spec was produced (LLM, default, fake, a future manual edit): ``save_compiled`` only needs a
``StructurePlanningResult``.

Staleness is DERIVED, never stored: ``status`` compares the hash saved with the spec against the hash
of the definition's CURRENT prompt + active questions. So editing the prompt, a question's text/title,
its key, its position, or adding/removing a question all invalidate the spec with no extra bookkeeping
and without touching the saved spec (it stays recoverable). A stale spec is never served to a run.

    not_required  no prompt            -> default structure, nothing to compile
    not_compiled  prompt, no spec yet  -> default structure + warning (no LLM is called implicitly)
    stale         hash mismatch        -> default structure + warning
    interpreted   hash matches, planner succeeded
    fallback      hash matches, planner had failed (default structure; a new compile retries it)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app import db
from app.models import AnalyticalReportDefinition

from .snapshot import structure_block
from .structure_contracts import (
    SOURCE_DEFAULT, SOURCE_FALLBACK, SOURCE_INTERPRETED, WARNING_NOT_COMPILED, WARNING_STALE, StructureError,
    StructureQuestion, StructurePlanningResult, StructureWarning,
)
from .structure_spec import CURRENT_STRUCTURE_SCHEMA, ReportStructureSpec
from .structure_validation import default_structure_spec, structure_input_hash, validate_spec

STATUS_NOT_REQUIRED = "not_required"
STATUS_NOT_COMPILED = "not_compiled"
STATUS_STALE = "stale"
STATUS_INTERPRETED = "interpreted"
STATUS_FALLBACK = "fallback"
USABLE_STATUSES = (STATUS_INTERPRETED, STATUS_FALLBACK)


class StructureChangedDuringCompileError(StructureError):
    """The prompt/questions changed while the planner was running; the result must not be saved."""
    code = "structure_input_changed_during_compile"


@dataclass(frozen=True)
class StructureState:
    """What a definition currently has. ``spec`` is ALWAYS usable (the default one when not up to date)."""
    status: str
    prompt: str | None
    input_hash: str
    stored_hash: str | None
    source: str
    spec: ReportStructureSpec
    warnings: tuple[StructureWarning, ...]
    questions: tuple[StructureQuestion, ...]
    compiled_at: datetime | None = None
    planner: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    # The saved (possibly stale) spec, so the UI can still show what was understood before the edit.
    saved_spec: ReportStructureSpec | None = None

    @property
    def stale(self) -> bool:
        return self.status == STATUS_STALE

    @property
    def needs_compile(self) -> bool:
        return self.status in (STATUS_NOT_COMPILED, STATUS_STALE, STATUS_FALLBACK)


def questions_of(model: AnalyticalReportDefinition) -> tuple[StructureQuestion, ...]:
    active = sorted((q for q in model.questions if q.is_active), key=lambda q: (q.position, q.id or 0))
    return tuple(StructureQuestion(key=q.key, title=q.title, question=q.question, position=q.position)
                 for q in active)


def _warnings(raw) -> tuple[StructureWarning, ...]:
    return tuple(StructureWarning.model_validate(item) for item in raw or ())


class StructureStore:
    def state(self, model: AnalyticalReportDefinition) -> StructureState:
        questions = questions_of(model)
        prompt = (model.structure_prompt or "").strip() or None
        current_hash = structure_input_hash(prompt, questions)
        stored_hash = model.structure_input_hash
        meta = dict(model.structure_compile_json or {})
        default = default_structure_spec(questions)
        common = dict(prompt=prompt, input_hash=current_hash, stored_hash=stored_hash, questions=questions,
                      compiled_at=model.structure_compiled_at)

        if prompt is None:
            return StructureState(status=STATUS_NOT_REQUIRED, source=SOURCE_DEFAULT, spec=default, warnings=(),
                                  **common)
        saved = self._load_saved(model, questions)
        if model.structure_spec_json is None or saved is None:
            return StructureState(
                status=STATUS_NOT_COMPILED, source=SOURCE_DEFAULT, spec=default, saved_spec=saved,
                warnings=(StructureWarning(code=WARNING_NOT_COMPILED, severity="info", message=(
                    "El prompt de estructura todavía no fue interpretado; se usa la estructura estándar."),),),
                **common)
        if stored_hash != current_hash:
            return StructureState(
                status=STATUS_STALE, source=SOURCE_DEFAULT, spec=default, saved_spec=saved,
                warnings=(StructureWarning(code=WARNING_STALE, message=(
                    "El prompt o las preguntas cambiaron desde la última interpretación; "
                    "recompilá la estructura. Mientras tanto se usa la estructura estándar."),),),
                **common)
        source = meta.get("source") if meta.get("source") in (SOURCE_INTERPRETED, SOURCE_FALLBACK) else SOURCE_INTERPRETED
        return StructureState(
            status=STATUS_FALLBACK if source == SOURCE_FALLBACK else STATUS_INTERPRETED, source=source,
            spec=saved, saved_spec=saved, warnings=_warnings(meta.get("warnings")),
            planner=dict(meta.get("planner") or {}), error=meta.get("error"), **common)

    @staticmethod
    def _load_saved(model: AnalyticalReportDefinition, questions) -> ReportStructureSpec | None:
        if model.structure_spec_json is None:
            return None
        try:
            # Validated by the spec's OWN version; keys checked against the current questions only
            # when it is served (a stale spec may legitimately reference removed keys).
            return validate_spec(model.structure_spec_json, {key for key in _all_referenced(model.structure_spec_json)},
                                 schema_version=model.structure_schema_version)
        except StructureError:
            return None

    def save_compiled(self, definition_id: int, result: StructurePlanningResult, input_hash: str) -> AnalyticalReportDefinition:
        """Atomically store spec + hash + version + metadata, only if the input is still the same.

        ``input_hash`` is the hash the planner was given. If the definition changed meanwhile the
        write is refused, so a new prompt can never end up paired with the spec of the old one.
        """
        try:
            model = db.session.execute(
                select(AnalyticalReportDefinition).where(AnalyticalReportDefinition.id == definition_id)
                .with_for_update()).scalar_one_or_none()
            if model is None:
                raise StructureError(f"Definition not found: {definition_id}", code="definition_not_found")
            db.session.refresh(model)
            prompt = (model.structure_prompt or "").strip() or None
            if structure_input_hash(prompt, questions_of(model)) != input_hash:
                raise StructureChangedDuringCompileError(
                    "El prompt o las preguntas cambiaron mientras se interpretaba la estructura.")
            model.structure_spec_json = result.spec.to_json()
            model.structure_schema_version = result.spec.schema_version
            model.structure_input_hash = input_hash
            model.structure_compiled_at = datetime.now(timezone.utc)
            model.structure_compile_json = {
                "source": SOURCE_FALLBACK if result.fallback_used else SOURCE_INTERPRETED,
                "warnings": [w.model_dump(mode="json") for w in result.warnings],
                "planner": dict(result.planner), "error": result.error,
            }
            db.session.commit()
            return model
        except Exception:
            db.session.rollback()
            raise

    def snapshot_block(self, model: AnalyticalReportDefinition) -> dict[str, Any]:
        """The frozen structure for a new run: what the definition has NOW, never an implicit LLM call."""
        state = self.state(model)
        return structure_block(
            prompt=state.prompt, input_hash=state.input_hash, spec=state.spec, source=state.source,
            status=state.status, warnings=state.warnings)


def _all_referenced(raw) -> set[str]:
    keys: set[str] = set()
    for item in (raw or {}).get("items", []) if isinstance(raw, dict) else []:
        keys.update(item.get("source_question_keys", ()) if isinstance(item, dict) else ())
    return keys
