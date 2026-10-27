"""``FinalReport`` 1.3: the semantic, visual-capable description of a report (V1.5 data story).

Same pattern as 1.2 (metadata + one ordered ``items`` list whose order is the document's order, dictated by the
``ComposedReportInput``), richer vocabulary:

    hero             headline / deck / metadata chips / one highlighted figure / executive highlights
    metric_strip     the KPI strip (evidence-backed metrics)
    section          title + summary + ``blocks`` + optional ``secondary_blocks``; ``layout`` standard | feature | split
    attention_grid   cards by severity (high | medium | low)
    notes            the user's literal note (text fixed by the composer)
    methodology_notes

Mapping from the StructureSpec (unchanged): executive_summary -> hero, kpi_grid -> metric_strip,
attention_points -> attention_grid; section / notes / methodology_notes keep their names.

``layout`` is a closed semantic word, never geometry: ``standard`` one column; ``feature`` main content +
supporting rail (``secondary_blocks``); ``split`` two equivalent columns (``blocks`` | ``secondary_blocks``).
Columns, gaps, colors and breakpoints belong to the renderer and its theme. 1.1 / 1.2 are untouched: this is
an independent contract that reuses the same leaf models.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .report_content import (
    MAX_HIGHLIGHTS, MAX_ITEMS, AttentionPoint, Key, LongText, ReportHighlight, ReportNote, ReportPeriod,
    ShortText, SourceKeys, _Strict,
)
from .versions import FINAL_REPORT_SCHEMA_1_3
from .visual_content import (
    MAX_BLOCKS_PER_SECTION_13, MAX_CHARTS_PER_SECTION, MAX_HERO_METADATA, MAX_METRICS, MAX_SECONDARY_BLOCKS,
    MAX_VISUALS_PER_REPORT, VISUAL_BLOCK_TYPES, EvidenceRef, ReportBlock13, StripMetric,
)

SCHEMA_VERSION = FINAL_REPORT_SCHEMA_1_3
MAX_REPORT_ITEMS = 30


class HeroHighlight(_Strict):
    """The one figure the hero puts forward ("4/7 días con datos"). Needs provenance like any other figure."""
    value: Annotated[str, Field(min_length=1, max_length=80)]
    label: ShortText
    supporting_text: Annotated[str, Field(max_length=300)] | None = None
    fact_ref: EvidenceRef | None = None
    source_section_keys: SourceKeys = Field(default_factory=list)

    @model_validator(mode="after")
    def _sourced(self) -> "HeroHighlight":
        if not self.source_section_keys:
            raise ValueError("the hero highlight must declare its source_section_keys")
        return self


class HeroItem(_Strict):
    type: Literal["hero"] = "hero"
    headline: ShortText
    deck: Annotated[str, Field(min_length=1, max_length=600)] | None = None
    metadata: Annotated[list[Annotated[str, Field(min_length=1, max_length=80)]],
                        Field(max_length=MAX_HERO_METADATA)] = Field(default_factory=list)
    highlight: HeroHighlight | None = None
    highlights: Annotated[list[ReportHighlight], Field(max_length=MAX_HIGHLIGHTS)] = Field(default_factory=list)
    headline_source_section_keys: SourceKeys = Field(default_factory=list)


class MetricStripItem(_Strict):
    type: Literal["metric_strip"] = "metric_strip"
    metrics: Annotated[list[StripMetric], Field(max_length=MAX_METRICS)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "MetricStripItem":
        keys = [metric.key for metric in self.metrics]
        if len(set(keys)) != len(keys):
            raise ValueError("metric_strip metrics must have unique keys")
        return self


class SectionItem13(_Strict):
    type: Literal["section"] = "section"
    key: Key
    title: ShortText
    status: Literal["ok", "unavailable"] = "ok"
    layout: Literal["standard", "feature", "split"] = "standard"
    summary: LongText
    blocks: Annotated[list[ReportBlock13], Field(max_length=MAX_BLOCKS_PER_SECTION_13)] = Field(default_factory=list)
    # feature: the supporting rail. split: the second column. standard: must be empty.
    secondary_blocks: Annotated[list[ReportBlock13], Field(max_length=MAX_SECONDARY_BLOCKS)] = Field(
        default_factory=list)
    source_section_keys: SourceKeys = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> "SectionItem13":
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


class AttentionGridItem(_Strict):
    type: Literal["attention_grid"] = "attention_grid"
    points: Annotated[list[AttentionPoint], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _points_are_sourced(self) -> "AttentionGridItem":
        if any(not point.source_section_keys for point in self.points):
            raise ValueError("every attention point must declare its source_section_keys")
        return self


class NotesItem13(_Strict):
    type: Literal["notes"] = "notes"
    text: LongText


class MethodologyNotesItem13(_Strict):
    type: Literal["methodology_notes"] = "methodology_notes"
    notes: Annotated[list[ReportNote], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)


ReportItem13 = Annotated[
    Union[HeroItem, MetricStripItem, SectionItem13, AttentionGridItem, NotesItem13, MethodologyNotesItem13],
    Field(discriminator="type"),
]


class FinalReportV13(_Strict):
    schema_version: Literal["1.3"] = SCHEMA_VERSION
    title: ShortText
    subtitle: Annotated[str, Field(max_length=300)] | None = None
    period: ReportPeriod = Field(default_factory=ReportPeriod)
    items: Annotated[list[ReportItem13], Field(min_length=1, max_length=MAX_REPORT_ITEMS)]

    @model_validator(mode="after")
    def _coherent(self) -> "FinalReportV13":
        for kind in ("hero", "metric_strip", "attention_grid", "methodology_notes"):
            if sum(1 for item in self.items if item.type == kind) > 1:
                raise ValueError(f"at most one {kind} item is allowed")
        keys = [item.key for item in self.items if item.type == "section"]
        if len(set(keys)) != len(keys):
            raise ValueError("sections must have unique keys")
        if self.visual_component_count() > MAX_VISUALS_PER_REPORT:
            raise ValueError(f"at most {MAX_VISUALS_PER_REPORT} visual components per report")
        return self

    # ── introspection (renderers / observability read these) ──────────────────────────────────
    def sections(self) -> list[SectionItem13]:
        return [item for item in self.items if item.type == "section"]

    def visual_blocks(self) -> list:
        return [block for section in self.sections() for block in section.all_blocks()
                if block.type in VISUAL_BLOCK_TYPES]

    def visual_component_count(self) -> int:
        extra = sum(1 for item in self.items if item.type == "hero" and item.highlight is not None) + sum(
            len(item.metrics) for item in self.items if item.type == "metric_strip")
        return len(self.visual_blocks()) + extra

    def chart_count(self) -> int:
        return sum(1 for block in self.visual_blocks() if block.type == "bar_chart")

    def analytical_source_keys(self) -> set[str]:
        """Evidence keys backing analytical claims (hero, metrics, ``ok`` sections and their blocks, attention)."""
        keys: set[str] = set()
        for item in self.items:
            if item.type == "hero":
                keys.update(item.headline_source_section_keys)
                for highlight in item.highlights:
                    keys.update(highlight.source_section_keys)
                if item.highlight is not None:
                    keys.update(item.highlight.source_section_keys)
            elif item.type == "metric_strip":
                for metric in item.metrics:
                    keys.update(metric.source_section_keys)
            elif item.type == "section" and item.status == "ok":
                keys.update(item.source_section_keys)
                for block in item.all_blocks():
                    keys.update(getattr(block, "source_section_keys", ()))
                    for card in getattr(block, "cards", ()):
                        keys.update(card.source_section_keys)
            elif item.type == "attention_grid":
                for point in item.points:
                    keys.update(point.source_section_keys)
        return keys


def final_report_v13_json_schema() -> dict:
    return FinalReportV13.model_json_schema()
