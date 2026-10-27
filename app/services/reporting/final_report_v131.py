"""``FinalReport`` 1.3.1: 1.3 with TYPED evidence references (V1.5.1).

Why a new version instead of editing 1.3: the JSON shape of a reference changed
(``{section_key, key}`` -> ``FactRef | SeriesRef | SeriesItemRef``) and a card's ``value`` became optional-in-writer-output.
Persisted 1.3 documents keep validating with ``FinalReportV13`` and rendering with ``html-v3``, untouched.

What did NOT change: the item vocabulary (hero, metric_strip, section, attention_grid, notes, methodology_notes), the
section ``layout`` words, all limits, and the MATERIALIZED figures that ``html-v3`` reads. ``html-v3`` therefore presents
both 1.3 and 1.3.1 (it never looks at refs); no new renderer was needed.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .final_report_v13 import (
    MAX_REPORT_ITEMS, AttentionGridItem, FinalReportV13, MethodologyNotesItem13, NotesItem13,
)
from .evidence_refs import ValueRef
from .report_content import (
    MAX_HIGHLIGHTS, Key, LongText, ReportHighlight, ReportPeriod, ShortText, SourceKeys, _Strict,
)
from .versions import FINAL_REPORT_SCHEMA_1_3_1
from .visual_content import (
    MAX_BLOCKS_PER_SECTION_13, MAX_CHARTS_PER_SECTION, MAX_HERO_METADATA, MAX_METRICS, MAX_SECONDARY_BLOCKS,
    MAX_VISUALS_PER_REPORT,
)
from .visual_content_v131 import ReportBlock131, StripMetric131

SCHEMA_VERSION = FINAL_REPORT_SCHEMA_1_3_1


class HeroHighlight131(_Strict):
    """The one figure the hero puts forward. ``value`` is materialized from ``value_ref`` when omitted."""
    value: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    label: ShortText
    supporting_text: Annotated[str, Field(max_length=300)] | None = None
    value_ref: ValueRef | None = None
    source_section_keys: SourceKeys = Field(default_factory=list)

    @model_validator(mode="after")
    def _complete(self) -> "HeroHighlight131":
        if not self.source_section_keys:
            raise ValueError("the hero highlight must declare its source_section_keys")
        if self.value is None:
            raise ValueError("the hero highlight needs a value (from value_ref, or a figure grounded in the evidence)")
        return self


class HeroItem131(_Strict):
    type: Literal["hero"] = "hero"
    headline: ShortText
    deck: Annotated[str, Field(min_length=1, max_length=600)] | None = None
    metadata: Annotated[list[Annotated[str, Field(min_length=1, max_length=80)]],
                        Field(max_length=MAX_HERO_METADATA)] = Field(default_factory=list)
    highlight: HeroHighlight131 | None = None
    highlights: Annotated[list[ReportHighlight], Field(max_length=MAX_HIGHLIGHTS)] = Field(default_factory=list)
    headline_source_section_keys: SourceKeys = Field(default_factory=list)


class MetricStripItem131(_Strict):
    """No minimum: ``max_kpis`` is a ceiling (0..8), never a quota."""
    type: Literal["metric_strip"] = "metric_strip"
    metrics: Annotated[list[StripMetric131], Field(max_length=MAX_METRICS)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "MetricStripItem131":
        keys = [metric.key for metric in self.metrics]
        if len(set(keys)) != len(keys):
            raise ValueError("metric_strip metrics must have unique keys")
        return self


class SectionItem131(_Strict):
    type: Literal["section"] = "section"
    key: Key
    title: ShortText
    status: Literal["ok", "unavailable"] = "ok"
    layout: Literal["standard", "feature", "split"] = "standard"
    summary: LongText
    blocks: Annotated[list[ReportBlock131], Field(max_length=MAX_BLOCKS_PER_SECTION_13)] = Field(default_factory=list)
    secondary_blocks: Annotated[list[ReportBlock131], Field(max_length=MAX_SECONDARY_BLOCKS)] = Field(
        default_factory=list)
    source_section_keys: SourceKeys = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> "SectionItem131":
        if self.status == "ok" and not self.source_section_keys:
            raise ValueError("a section with status 'ok' must declare its source_section_keys")
        if self.status == "unavailable" and (self.blocks or self.secondary_blocks):
            raise ValueError("a section with status 'unavailable' cannot have blocks")
        if self.layout == "standard" and self.secondary_blocks:
            raise ValueError("secondary_blocks require layout 'feature' or 'split'")
        if sum(1 for b in (*self.blocks, *self.secondary_blocks) if b.type == "bar_chart") > MAX_CHARTS_PER_SECTION:
            raise ValueError(f"at most {MAX_CHARTS_PER_SECTION} bar_chart blocks per section")
        return self

    def all_blocks(self) -> list:
        return [*self.blocks, *self.secondary_blocks]


ReportItem131 = Annotated[
    Union[HeroItem131, MetricStripItem131, SectionItem131, AttentionGridItem, NotesItem13, MethodologyNotesItem13],
    Field(discriminator="type"),
]


class FinalReportV131(_Strict):
    schema_version: Literal["1.3.1"] = SCHEMA_VERSION
    title: ShortText
    subtitle: Annotated[str, Field(max_length=300)] | None = None
    period: ReportPeriod = Field(default_factory=ReportPeriod)
    items: Annotated[list[ReportItem131], Field(min_length=1, max_length=MAX_REPORT_ITEMS)]

    @model_validator(mode="after")
    def _coherent(self) -> "FinalReportV131":
        for kind in ("hero", "metric_strip", "attention_grid", "methodology_notes"):
            if sum(1 for item in self.items if item.type == kind) > 1:
                raise ValueError(f"at most one {kind} item is allowed")
        keys = [item.key for item in self.items if item.type == "section"]
        if len(set(keys)) != len(keys):
            raise ValueError("sections must have unique keys")
        if self.visual_component_count() > MAX_VISUALS_PER_REPORT:
            raise ValueError(f"at most {MAX_VISUALS_PER_REPORT} visual components per report")
        return self

    # Introspection is shape-only (``item.type`` / attributes), shared verbatim with 1.3.
    sections = FinalReportV13.sections
    visual_blocks = FinalReportV13.visual_blocks
    visual_component_count = FinalReportV13.visual_component_count
    chart_count = FinalReportV13.chart_count
    analytical_source_keys = FinalReportV13.analytical_source_keys


def final_report_v131_json_schema() -> dict:
    return FinalReportV131.model_json_schema()
