"""Typed, versioned contract of the client-facing report (``FinalReport``).

The writer (an LLM) produces semantics only: no HTML, no Markdown structure, no
CSS, no colors. Presentation belongs to a renderer. Every model is strict
(``extra="forbid"``) so unknown or presentation-oriented fields are rejected.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.1"

MAX_KPIS = 8
MAX_HIGHLIGHTS = 6
MAX_SECTIONS = 20
MAX_BLOCKS_PER_SECTION = 20
MAX_TABLE_COLUMNS = 12
MAX_TABLE_ROWS = 200
MAX_ITEMS = 30

Key = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
ShortText = Annotated[str, Field(min_length=1, max_length=300)]
LongText = Annotated[str, Field(min_length=1, max_length=4000)]
SourceKeys = Annotated[list[Key], Field(max_length=50)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReportPeriod(_Strict):
    """Period covered by the report; every field is optional and never invented."""
    label: Annotated[str, Field(max_length=120)] | None = None
    start: Annotated[str, Field(max_length=40)] | None = None
    end: Annotated[str, Field(max_length=40)] | None = None
    comparison_label: Annotated[str, Field(max_length=120)] | None = None


class ReportHighlight(_Strict):
    """One executive-summary bullet; provenance-bearing like KPIs and sections."""
    text: ShortText
    source_section_keys: SourceKeys = Field(default_factory=list)


class ExecutiveSummary(_Strict):
    headline: ShortText
    # Optional: the headline is a synthesis and may legitimately span every
    # successful section, so an empty list here is not itself a validation error.
    headline_source_section_keys: SourceKeys = Field(default_factory=list)
    highlights: Annotated[list[ReportHighlight], Field(max_length=MAX_HIGHLIGHTS)] = Field(default_factory=list)


class ReportKPI(_Strict):
    key: Key
    label: ShortText
    value: Annotated[str, Field(min_length=1, max_length=80)]
    secondary_value: Annotated[str, Field(max_length=120)] | None = None
    # Semantics only; the renderer decides how these look.
    # trend: direction of the change. impact: whether that direction is good news,
    # bad news, or neither (e.g. trend=up + impact=negative for rising expenses).
    trend: Literal["up", "down", "stable", "neutral"] | None = None
    impact: Literal["positive", "negative", "neutral"] | None = None
    source_section_keys: SourceKeys = Field(default_factory=list)


class ParagraphBlock(_Strict):
    type: Literal["paragraph"] = "paragraph"
    text: LongText


class BulletListBlock(_Strict):
    type: Literal["bullet_list"] = "bullet_list"
    items: Annotated[list[Annotated[str, Field(min_length=1, max_length=1000)]],
                     Field(min_length=1, max_length=MAX_ITEMS)]


class TableColumn(_Strict):
    key: Key
    label: Annotated[str, Field(min_length=1, max_length=80)]


class TableBlock(_Strict):
    type: Literal["table"] = "table"
    caption: Annotated[str, Field(max_length=200)] | None = None
    columns: Annotated[list[TableColumn], Field(min_length=1, max_length=MAX_TABLE_COLUMNS)]
    rows: Annotated[list[dict[str, Annotated[str, Field(max_length=300)]]], Field(max_length=MAX_TABLE_ROWS)]

    @model_validator(mode="after")
    def _rows_match_columns(self) -> "TableBlock":
        keys = [column.key for column in self.columns]
        if len(set(keys)) != len(keys):
            raise ValueError("table columns must have unique keys")
        allowed = set(keys)
        for index, row in enumerate(self.rows):
            unknown = sorted(set(row) - allowed)
            if unknown:
                raise ValueError(f"table row {index} uses undeclared columns: {', '.join(unknown)}")
        return self


class CalloutBlock(_Strict):
    type: Literal["callout"] = "callout"
    severity: Literal["info", "warning", "critical"]
    title: ShortText
    text: LongText


ReportBlock = Annotated[
    Union[ParagraphBlock, BulletListBlock, TableBlock, CalloutBlock],
    Field(discriminator="type"),
]


class FinalReportSection(_Strict):
    key: Key
    title: ShortText
    summary: LongText
    blocks: Annotated[list[ReportBlock], Field(max_length=MAX_BLOCKS_PER_SECTION)] = Field(default_factory=list)
    # ReportDraft section keys that support this section (provenance).
    source_section_keys: SourceKeys = Field(default_factory=list)


class AttentionPoint(_Strict):
    severity: Literal["low", "medium", "high"]
    title: ShortText
    text: LongText
    source_section_keys: SourceKeys = Field(default_factory=list)


class ReportNote(_Strict):
    """Client-readable note, e.g. methodological context derived from semantic notes."""
    kind: Literal["methodology", "data_quality", "general"] = "general"
    text: LongText
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
