"""Typed, versioned contract of the client-facing report (``FinalReport``).

The writer (an LLM) produces semantics only: no HTML, no Markdown structure, no
CSS, no colors. Presentation belongs to a renderer. Every model is strict
(``extra="forbid"``) so unknown or presentation-oriented fields are rejected.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .report_content import (  # noqa: F401 - re-exported: ``final_report`` stays the import path of 1.1
    MAX_BLOCKS_PER_SECTION, MAX_HIGHLIGHTS, MAX_ITEMS, MAX_KPIS, MAX_SECTIONS, MAX_TABLE_COLUMNS, MAX_TABLE_ROWS,
    AttentionPoint, BulletListBlock, CalloutBlock, Key, LongText, ParagraphBlock, ReportBlock, ReportHighlight,
    ReportKPI, ReportNote, ReportPeriod, ShortText, SourceKeys, TableBlock, TableColumn, _Strict,
)
from .versions import FINAL_REPORT_SCHEMA_1_1

SCHEMA_VERSION = FINAL_REPORT_SCHEMA_1_1



class ExecutiveSummary(_Strict):
    headline: ShortText
    # Optional: the headline is a synthesis and may legitimately span every
    # successful section, so an empty list here is not itself a validation error.
    headline_source_section_keys: SourceKeys = Field(default_factory=list)
    highlights: Annotated[list[ReportHighlight], Field(max_length=MAX_HIGHLIGHTS)] = Field(default_factory=list)


class FinalReportSection(_Strict):
    key: Key
    title: ShortText
    summary: LongText
    blocks: Annotated[list[ReportBlock], Field(max_length=MAX_BLOCKS_PER_SECTION)] = Field(default_factory=list)
    # ReportDraft section keys that support this section (provenance).
    source_section_keys: SourceKeys = Field(default_factory=list)


class FinalReport(_Strict):
    schema_version: Literal["1.1"] = SCHEMA_VERSION
    title: ShortText
    subtitle: Annotated[str, Field(max_length=300)] | None = None
    period: ReportPeriod = Field(default_factory=ReportPeriod)
    executive_summary: ExecutiveSummary
    kpis: Annotated[list[ReportKPI], Field(max_length=MAX_KPIS)] = Field(default_factory=list)
    sections: Annotated[list[FinalReportSection], Field(min_length=1, max_length=MAX_SECTIONS)]
    attention_points: Annotated[list[AttentionPoint], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)
    notes: Annotated[list[ReportNote], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_keys(self) -> "FinalReport":
        for label, items in (("kpis", self.kpis), ("sections", self.sections)):
            keys = [item.key for item in items]
            if len(set(keys)) != len(keys):
                raise ValueError(f"{label} must have unique keys")
        return self

    def analytical_source_keys(self) -> set[str]:
        """Sources for claims that assert an analytical result.

        These must resolve to *successful* ReportDraft sections: a failed
        section is not evidence for a KPI, a highlight, a section or an
        attention point.
        """
        keys: set[str] = set(self.executive_summary.headline_source_section_keys)
        for highlight in self.executive_summary.highlights:
            keys.update(highlight.source_section_keys)
        for group in (self.kpis, self.sections, self.attention_points):
            for item in group:
                keys.update(item.source_section_keys)
        return keys

    def note_source_keys(self) -> set[str]:
        """Sources for ``notes``: may point at a failed section (to say, neutrally,
        that an analysis was unavailable) as well as at successful ones."""
        keys: set[str] = set()
        for note in self.notes:
            keys.update(note.source_section_keys)
        return keys


def final_report_json_schema() -> dict:
    return FinalReport.model_json_schema()
