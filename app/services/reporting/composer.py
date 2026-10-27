"""ReportComposer: ``ReportDraft`` + ``FrozenStructure`` -> ``ComposedReportInput``.

Deterministic and pure: no LLM, no SQLAlchemy, no Flask, no clock, no randomness. It does not write
prose and it does not render. It decides exactly two things, from data it is handed:

1. WHICH evidence feeds each structural item (below); the structure itself decides WHAT items exist and
   in WHAT ORDER, and the composer copies it unchanged (item absent = component absent);
2. what to flag when that evidence is imperfect (``composition_warnings``).

Evidence policy (the one place it lives; the writer only follows it)
    section            its ``source_question_keys`` (primary) + supplementary coordinator analyses.
    kpi_grid           ONLY its ``source_question_keys``. Never supplementary evidence, never "better" questions.
    executive_summary  the pool = every evidence record that some item (section / kpi_grid) uses, supplementary
    attention_points   included. If no item carries evidence, the pool is the whole draft.
    methodology_notes  the pool records that carry semantic notes or whose analysis did not succeed.
    notes              the user's literal text, carried through untouched.

Coordinator analyses (``origin="coordinator"``) never create or move components. A coordinator analysis
becomes SUPPLEMENTARY evidence of every ``section`` item whose primary evidence contains one of its
``related_section_keys``. If no section matches, it is NOT silently dropped and NOT promoted to a new section:
it is left out of every item and reported as a ``coordinator_evidence_orphaned`` warning.

Failed / cancelled / missing evidence is preserved with its status, so a requested section survives its own
analysis failing and the writer can say so honestly instead of inventing data.
"""
from __future__ import annotations

from .composed_report import (
    COMPOSITION_SCHEMA_VERSION, COMPOSITION_SCHEMA_VERSION_2, CW_COORDINATOR_ORPHANED, CW_ITEM_WITHOUT_EVIDENCE, CW_SOURCE_FAILED, CW_SOURCE_MISSING,
    AttentionPointsInput, ComposedReportInput, CompositionWarning, EvidenceRecord, ExecutiveSummaryInput,
    KpiGridInput, MethodologyNotesInput, NotesInput, SectionInput,
)
from .contracts import ReportDraft, ReportSection
from .report_content import MAX_KPIS
from .structure_contracts import FrozenStructure, StructureQuestion
from .structure_validation import default_structure_spec


class CompositionError(Exception):
    """The structure/draft pair cannot be composed. Never repaired by guessing or by a model."""
    code = "report_composition_failed"


def _record(section: ReportSection, *, structured: bool = False) -> EvidenceRecord:
    if section.had_error:
        return EvidenceRecord(key=section.key, status="failed", origin=section.origin, title=section.title,
                              question=section.question)
    evidence = section.evidence if structured else None
    return EvidenceRecord(
        key=section.key, status="ok", origin=section.origin, title=section.title, question=section.question,
        answer=section.answer, purpose=section.purpose, related_section_keys=list(section.related_section_keys),
        semantic_notes=list(section.semantic_notes),
        facts=list(evidence.facts) if evidence else [], series=list(evidence.series) if evidence else [],
        tables=list(evidence.tables) if evidence else [])


def default_questions_from_draft(draft: ReportDraft) -> list[StructureQuestion]:
    """Definition questions in the draft's order, to derive the default structure when none was frozen."""
    originals = [s for s in draft.sections if s.origin == "definition"]
    return [StructureQuestion(key=s.key, title=s.title, question=s.question, position=index)
            for index, s in enumerate(originals, start=1)]


def _unique(keys) -> list[str]:
    return list(dict.fromkeys(keys))


class ReportComposer:
    """``structured_evidence=True`` (V1.5) also carries each section's facts / series / tables and emits a
    ``ComposedReportInput`` v2. The composer only decides WHICH evidence an item may use; it never picks
    charts, cards or layouts (that is the writer's editorial choice, within what is authorised here)."""

    def __init__(self, *, structured_evidence: bool = False):
        self.structured_evidence = structured_evidence

    def compose(self, draft: ReportDraft, structure: FrozenStructure | None = None) -> ComposedReportInput:
        if structure is not None and not isinstance(structure, FrozenStructure):
            raise CompositionError("structure must be a FrozenStructure")
        spec = structure.spec if structure is not None else default_structure_spec(default_questions_from_draft(draft))
        by_key = {section.key: section for section in draft.sections}
        items = list(spec.items)

        # Primary evidence assigned by the structure (sections and the KPI pool).
        primary: dict[int, list[str]] = {
            index: _unique(item.source_question_keys) for index, item in enumerate(items)
            if item.type in ("section", "kpi_grid")}

        # Coordinator evidence -> supplementary evidence of the sections it relates to (never of KPIs).
        supplementary: dict[int, list[str]] = {index: [] for index, item in enumerate(items) if item.type == "section"}
        warnings: list[CompositionWarning] = []
        for extra in (s for s in draft.sections if s.origin == "coordinator"):
            related = set(extra.related_section_keys)
            targets = [index for index in supplementary if related & set(primary[index])]
            for index in targets:
                if extra.key not in primary[index] and extra.key not in supplementary[index]:
                    supplementary[index].append(extra.key)
            if not targets:
                warnings.append(CompositionWarning(
                    code=CW_COORDINATOR_ORPHANED, severity="info", keys=[extra.key],
                    message=("Un análisis adicional del coordinador no se asocia a ninguna sección de la estructura "
                             "y no se incluyó en el informe.")))

        # The pool executive summary / attention points / methodology notes may draw from.
        referenced = _unique(key for index in sorted(primary) for key in primary[index]) + [
            key for index in sorted(supplementary) for key in supplementary[index]]
        # No item carries evidence: the whole ORIGINAL draft (orphan coordinator analyses stay out, as warned).
        pool = _unique(referenced) or [s.key for s in draft.sections if s.origin == "definition"]

        records: dict[str, EvidenceRecord] = {}

        def register(keys) -> list[str]:
            for key in keys:
                if key not in records:
                    section = by_key.get(key)
                    records[key] = _record(section, structured=self.structured_evidence) if section is not None                         else EvidenceRecord(
                        key=key, status="missing")
            return list(keys)

        composed_items = []
        for index, item in enumerate(items):
            if item.type == "executive_summary":
                composed_items.append(ExecutiveSummaryInput(evidence_keys=register(pool)))
            elif item.type == "kpi_grid":
                composed_items.append(KpiGridInput(evidence_keys=register(primary[index]), max_kpis=MAX_KPIS))
            elif item.type == "section":
                composed_items.append(SectionInput(
                    key=item.key, title=item.title, evidence_keys=register(primary[index]),
                    supplementary_keys=register(supplementary[index])))
            elif item.type == "attention_points":
                composed_items.append(AttentionPointsInput(evidence_keys=register(pool)))
            elif item.type == "notes":
                composed_items.append(NotesInput(text=item.text))
            elif item.type == "methodology_notes":
                register(pool)
                # v2 (V1.5.1): the whole pool may back a methodology note, because caveats about bases, periods or
                # comparability often live in an ``answer`` and not in ``semantic_notes``. Provenance stays
                # deterministic (notes can only cite this pool); WHAT is worth saying stays the writer's call.
                # v1 keeps the original, narrower rule (1.2 writer inputs do not change).
                composed_items.append(MethodologyNotesInput(evidence_keys=list(pool) if self.structured_evidence else [
                    key for key in pool if records[key].status != "ok" or records[key].semantic_notes]))
            else:  # a spec version this composer does not know: refuse, never improvise
                raise CompositionError(f"Unsupported structure item type: {item.type!r}")

        warnings += self._evidence_warnings(composed_items, records)
        try:
            return ComposedReportInput(
                schema_version=COMPOSITION_SCHEMA_VERSION_2 if self.structured_evidence else COMPOSITION_SCHEMA_VERSION,
                report_name=draft.name, structure_schema_version=spec.schema_version,
                evidence=list(records.values()), items=composed_items, composition_warnings=warnings)
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            raise CompositionError(f"Composed input is not valid: {exc}") from exc

    @staticmethod
    def _evidence_warnings(items, records: dict[str, EvidenceRecord]) -> list[CompositionWarning]:
        out: list[CompositionWarning] = []
        for index, item in enumerate(items):
            keys = [*getattr(item, "evidence_keys", ()), *getattr(item, "supplementary_keys", ())]
            if not keys and item.type in ("notes", "methodology_notes"):
                continue
            if item.type == "methodology_notes":
                continue
            missing = [k for k in keys if records[k].status == "missing"]
            failed = [k for k in keys if records[k].status == "failed"]
            if missing:
                out.append(CompositionWarning(
                    code=CW_SOURCE_MISSING, item_index=index, keys=missing,
                    message="La estructura referencia análisis que no existen en este informe."))
            if failed:
                out.append(CompositionWarning(
                    code=CW_SOURCE_FAILED, item_index=index, keys=failed, severity="info",
                    message="Algunos análisis que alimentan este componente no se pudieron completar."))
            if item.type != "notes" and not any(records[k].status == "ok" for k in keys):
                out.append(CompositionWarning(
                    code=CW_ITEM_WITHOUT_EVIDENCE, item_index=index, keys=list(keys),
                    message="Este componente no tiene ningún análisis exitoso que lo respalde."))
        return out
