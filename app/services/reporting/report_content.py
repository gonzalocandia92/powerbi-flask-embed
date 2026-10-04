"""Small content models shared by the FinalReport contracts (leaf building blocks).

These are the pieces that every report version spells the same way: provenance-bearing
KPIs, highlights, attention points, notes and the closed set of content blocks. Versions
compose them (``FinalReport`` 1.1, ``FinalReportV12``); they know nothing about how a
report is ordered. Strict (``extra="forbid"``) like the contracts that use them.

Changing one of these models changes every version that composes it: add a new model
instead when a version needs a different shape.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
