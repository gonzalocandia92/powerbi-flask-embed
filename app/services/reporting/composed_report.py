"""``ComposedReportInput``: the ONLY thing the structured writer receives.

It is the result of ``ReportComposer`` (deterministic, no LLM, no DB) over ``ReportDraft`` +
``FrozenStructure``. It answers, per top-level item of the structure and in the structure's order,
"which evidence is this item allowed to use". Three levels, kept apart on purpose:

* ``items``: WHAT components exist and in WHAT ORDER (authoritative: copied from the StructureSpec);
* ``evidence`` + each item's ``evidence_keys`` / ``supplementary_keys``: WHICH evidence feeds each item;
* the writer's job (not here): the prose.

Evidence is stored once (``evidence``) and referenced by key, so a section answer is not repeated for the
executive summary, the KPI grid and the section itself. The model never sees DAX, tool traces, skills,
routing, usage, DB ids, the free-text ``structure_prompt`` or a raw snapshot.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from .evidence import EvidenceFact, EvidenceSeries, EvidenceTable
from .report_content import MAX_KPIS

# "1": narrative evidence only (consumed by the 1.2 writer). "2" (V1.5): the same plus each record's structured
# evidence (facts / series / tables). Emitted by ``ReportComposer(structured_evidence=True)``; a v1 payload is
# never reinterpreted as v2 (the new fields simply do not exist in it).
COMPOSITION_SCHEMA_VERSION = "1"
COMPOSITION_SCHEMA_VERSION_2 = "2"

Key = Annotated[str, Field(min_length=1, max_length=80)]
KeyList = Annotated[list[Key], Field(max_length=100)]

# ── composition warnings (separate from the StructurePlanner's warnings) ──────────────────────
CW_SOURCE_MISSING = "source_question_missing"
CW_SOURCE_FAILED = "source_analysis_failed"
CW_COORDINATOR_ORPHANED = "coordinator_evidence_orphaned"
CW_ITEM_WITHOUT_EVIDENCE = "item_without_successful_evidence"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceRecord(_Strict):
    """One piece of analytical evidence (a ``ReportSection`` of the draft), reduced to what a writer may see."""
    key: Key
    # ok: analysis succeeded | failed: it ran and failed | missing: the structure references it but the draft has none.
    status: Literal["ok", "failed", "missing"]
    origin: Literal["definition", "coordinator"] = "definition"
    title: str | None = None
    question: str | None = None
    answer: str | None = None                      # only when status == "ok"
    purpose: str | None = None                     # coordinator evidence: why it was requested
    related_section_keys: KeyList = Field(default_factory=list)
    semantic_notes: Annotated[list[str], Field(max_length=20)] = Field(default_factory=list)
    # v2 only. Empty lists = "no structured evidence" (narrative-only record); never means "no data existed".
    facts: list[EvidenceFact] = Field(default_factory=list)
    series: list[EvidenceSeries] = Field(default_factory=list)
    tables: list[EvidenceTable] = Field(default_factory=list)

    def has_structured_evidence(self) -> bool:
        return bool(self.facts or self.series or self.tables)


class ExecutiveSummaryInput(_Strict):
    type: Literal["executive_summary"] = "executive_summary"
    evidence_keys: KeyList = Field(default_factory=list)


class KpiGridInput(_Strict):
    type: Literal["kpi_grid"] = "kpi_grid"
    evidence_keys: KeyList = Field(default_factory=list)   # the ONLY eligible sources for KPI cards
    max_kpis: int = MAX_KPIS


class SectionInput(_Strict):
    type: Literal["section"] = "section"
    key: Key
    title: Annotated[str, Field(min_length=1, max_length=200)]
    evidence_keys: KeyList = Field(default_factory=list)          # the questions the structure assigned
    supplementary_keys: KeyList = Field(default_factory=list)     # coordinator analyses that deepen them


class AttentionPointsInput(_Strict):
    type: Literal["attention_points"] = "attention_points"
    evidence_keys: KeyList = Field(default_factory=list)


class NotesInput(_Strict):
    """Literal text requested by the user: transported verbatim, never reinterpreted."""
    type: Literal["notes"] = "notes"
    text: Annotated[str, Field(min_length=1, max_length=1000)]


class MethodologyNotesInput(_Strict):
    type: Literal["methodology_notes"] = "methodology_notes"
    evidence_keys: KeyList = Field(default_factory=list)


ComposedItem = Annotated[
    Union[ExecutiveSummaryInput, KpiGridInput, SectionInput, AttentionPointsInput, NotesInput, MethodologyNotesInput],
    Field(discriminator="type"),
]


class CompositionWarning(_Strict):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=500)
    severity: Literal["info", "warning"] = "warning"
    item_index: int | None = None
    keys: KeyList = Field(default_factory=list)


class ComposedReportInput(_Strict):
    schema_version: Literal["1", "2"] = COMPOSITION_SCHEMA_VERSION
    report_name: Annotated[str, Field(min_length=1, max_length=200)]
    structure_schema_version: str
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    items: Annotated[list[ComposedItem], Field(min_length=1, max_length=60)]
    composition_warnings: list[CompositionWarning] = Field(default_factory=list)

    # ── read helpers (used by the writer's validation; pure) ──────────────────────────────
    def evidence_by_key(self) -> dict[str, EvidenceRecord]:
        return {record.key: record for record in self.evidence}

    def keys_with_status(self, status: str) -> set[str]:
        return {record.key for record in self.evidence if record.status == status}

    def item_keys(self, index: int) -> list[str]:
        """Every evidence key item ``index`` is authorised to reference (primary + supplementary), in order."""
        item = self.items[index]
        return [*getattr(item, "evidence_keys", ()), *getattr(item, "supplementary_keys", ())]

    def item_ok_keys(self, index: int) -> set[str]:
        ok = self.keys_with_status("ok")
        return {key for key in self.item_keys(index) if key in ok}

    def item_unavailable_keys(self, index: int) -> set[str]:
        ok = self.keys_with_status("ok")
        return {key for key in self.item_keys(index) if key not in ok}

    def has_successful_evidence(self) -> bool:
        return any(record.status == "ok" for record in self.evidence)
