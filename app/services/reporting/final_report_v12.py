"""``FinalReport`` 1.2: the client-facing report whose top-level ORDER is data.

1.1 has separate ``executive_summary`` / ``kpis`` / ``sections`` / ``attention_points`` / ``notes`` fields,
which force an implicit order. 1.2 has a single ordered ``items`` list: the order of ``items`` is the order of
the document, and each item is a discriminated union. The structural identity of those items (which exist, in
which order, with which section keys/titles) is NOT chosen by the writer: it is dictated by the
``ComposedReportInput`` and verified by ``composition_validation``.

Strict (``extra="forbid"``): no presentation fields. Leaf content (KPIs, highlights, attention points, notes,
paragraph/bullet_list/table/callout blocks) is composed from ``report_content``, the same small models 1.1
uses, so neither version inherits from the other.

Provenance: every analytical claim declares ``source_section_keys`` (draft evidence keys), which must be a
subset of the evidence the composer authorised for THAT item (see ``composition_validation``).
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .report_content import (
    MAX_BLOCKS_PER_SECTION, MAX_HIGHLIGHTS, MAX_ITEMS, MAX_KPIS, AttentionPoint, Key, LongText, ReportBlock,
    ReportHighlight, ReportKPI, ReportNote, ReportPeriod, ShortText, SourceKeys, _Strict,
)
from .versions import FINAL_REPORT_SCHEMA_1_2

SCHEMA_VERSION = FINAL_REPORT_SCHEMA_1_2
MAX_REPORT_ITEMS = 30


class ExecutiveSummaryItem12(_Strict):
    type: Literal["executive_summary"] = "executive_summary"
    headline: ShortText
    headline_source_section_keys: SourceKeys = Field(default_factory=list)
    highlights: Annotated[list[ReportHighlight], Field(max_length=MAX_HIGHLIGHTS)] = Field(default_factory=list)


class KpiGridItem12(_Strict):
    type: Literal["kpi_grid"] = "kpi_grid"
    kpis: Annotated[list[ReportKPI], Field(max_length=MAX_KPIS)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kpis_are_sourced_and_unique(self) -> "KpiGridItem12":
        if any(not kpi.source_section_keys for kpi in self.kpis):
            raise ValueError("every KPI must declare its source_section_keys")
        keys = [kpi.key for kpi in self.kpis]
        if len(set(keys)) != len(keys):
            raise ValueError("kpi_grid kpis must have unique keys")
        return self


class SectionItem12(_Strict):
    type: Literal["section"] = "section"
    key: Key
    title: ShortText
    # ok: written from evidence. unavailable: none of the section's evidence succeeded; ``summary`` says so
    # neutrally, there are no blocks and ``source_section_keys`` may only name the unavailable evidence.
    status: Literal["ok", "unavailable"] = "ok"
    summary: LongText
    blocks: Annotated[list[ReportBlock], Field(max_length=MAX_BLOCKS_PER_SECTION)] = Field(default_factory=list)
    source_section_keys: SourceKeys = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_is_coherent(self) -> "SectionItem12":
        if self.status == "ok" and not self.source_section_keys:
            raise ValueError("a section with status 'ok' must declare its source_section_keys")
        if self.status == "unavailable" and self.blocks:
            raise ValueError("a section with status 'unavailable' cannot have blocks")
        return self


class AttentionPointsItem12(_Strict):
    type: Literal["attention_points"] = "attention_points"
    # May be empty: "no point deserves attention" is a legitimate, honest outcome.
    points: Annotated[list[AttentionPoint], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _points_are_sourced(self) -> "AttentionPointsItem12":
        if any(not point.source_section_keys for point in self.points):
            raise ValueError("every attention point must declare its source_section_keys")
        return self


class NotesItem12(_Strict):
    """The user's literal note (``notes`` in the structure). Its text is fixed by the composer, not the model."""
    type: Literal["notes"] = "notes"
    text: LongText


class MethodologyNotesItem12(_Strict):
    type: Literal["methodology_notes"] = "methodology_notes"
    notes: Annotated[list[ReportNote], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)


ReportItem12 = Annotated[
    Union[ExecutiveSummaryItem12, KpiGridItem12, SectionItem12, AttentionPointsItem12, NotesItem12,
          MethodologyNotesItem12],
    Field(discriminator="type"),
]


class FinalReportV12(_Strict):
    schema_version: Literal["1.2"] = SCHEMA_VERSION
    title: ShortText
    subtitle: Annotated[str, Field(max_length=300)] | None = None
    period: ReportPeriod = Field(default_factory=ReportPeriod)
    # The order of this list IS the order of the document.
    items: Annotated[list[ReportItem12], Field(min_length=1, max_length=MAX_REPORT_ITEMS)]

    @model_validator(mode="after")
    def _coherent(self) -> "FinalReportV12":
        for kind in ("executive_summary", "kpi_grid", "attention_points", "methodology_notes"):
            if sum(1 for item in self.items if item.type == kind) > 1:
                raise ValueError(f"at most one {kind} item is allowed")
        keys = [item.key for item in self.items if item.type == "section"]
        if len(set(keys)) != len(keys):
            raise ValueError("sections must have unique keys")
        return self

    # ── introspection (renderers/consumers read these; none depends on how a report was produced) ──
    def sections(self) -> list[SectionItem12]:
        return [item for item in self.items if item.type == "section"]

    def analytical_source_keys(self) -> set[str]:
        """Evidence keys backing analytical claims (summary, KPIs, ``ok`` sections, attention points)."""
        keys: set[str] = set()
        for item in self.items:
            if item.type == "executive_summary":
                keys.update(item.headline_source_section_keys)
                for highlight in item.highlights:
                    keys.update(highlight.source_section_keys)
            elif item.type == "kpi_grid":
                for kpi in item.kpis:
                    keys.update(kpi.source_section_keys)
            elif item.type == "section" and item.status == "ok":
                keys.update(item.source_section_keys)
            elif item.type == "attention_points":
                for point in item.points:
                    keys.update(point.source_section_keys)
        return keys


def final_report_v12_json_schema() -> dict:
    return FinalReportV12.model_json_schema()
