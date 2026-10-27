"""Semantic visual components of ``FinalReport`` 1.3 (leaf models, no layout, no style).

Everything here says WHAT is shown and WHERE ITS DATA CAME FROM; nothing says how it looks. There are no
colors, sizes, CSS classes, HTML or SVG: the renderer (``html-v3``) and the theme own all of that.

Data is MATERIALIZED inside the FinalReport (chart items, card values) so a renderer never needs the database or
the evidence store. Each materialized figure keeps an ``EvidenceRef`` (``section_key`` of the analysis +
``key`` of the fact / series inside that section's ``ReportEvidence``): ``visual_validation`` re-checks every
figure against the evidence the composer authorised for that item, so a number the writer invented is rejected
instead of drawn. The refs stay in the persisted JSON as internal provenance and are never printed.

Closed vocabulary (adding a component = a new model here + a renderer branch; no analytics change):
    bar_chart     horizontal bars; ``variant``: ``bars`` | ``ranking`` | ``distribution``
    metric_cards  supporting cards (also the shape of a ``metric_strip`` metric)
    pull_quote    an editorial sentence, optionally with one highlighted figure
    annotation    a short caveat / method note placed next to the data
plus the 1.1 blocks reused unchanged: paragraph, bullet_list, table, callout.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .report_content import (
    BulletListBlock, CalloutBlock, Key, LongText, ParagraphBlock, ShortText, SourceKeys, TableBlock, _Strict,
)

MAX_CHART_ITEMS = 15
MAX_CARDS_PER_BLOCK = 6
MAX_METRICS = 8
MAX_HERO_METADATA = 6
MAX_BLOCKS_PER_SECTION_13 = 20
MAX_SECONDARY_BLOCKS = 8
MAX_CHARTS_PER_SECTION = 3
MAX_VISUALS_PER_REPORT = 24

Value = Annotated[str, Field(min_length=1, max_length=80)]


class EvidenceRef(_Strict):
    """Points at one fact / series of the structured evidence of an analysis."""
    section_key: Key
    key: Key


class MetricCard(_Strict):
    label: ShortText
    # A figure as it reads on the page. With ``fact_ref`` it must be that fact's ``formatted_value``;
    # without it, it must appear literally in the authorised narrative evidence.
    value: Value
    secondary_value: Annotated[str, Field(max_length=120)] | None = None
    supporting_text: Annotated[str, Field(max_length=300)] | None = None
    # Semantics only. trend = direction of change; impact = whether that is good / bad news.
    trend: Literal["up", "down", "stable", "neutral"] | None = None
    impact: Literal["positive", "negative", "neutral"] | None = None
    fact_ref: EvidenceRef | None = None
    secondary_fact_ref: EvidenceRef | None = None
    source_section_keys: SourceKeys = Field(default_factory=list)

    @model_validator(mode="after")
    def _sourced(self) -> "MetricCard":
        if not self.source_section_keys:
            raise ValueError("every metric card must declare its source_section_keys")
        return self


class StripMetric(MetricCard):
    key: Key


class BarItem(_Strict):
    label: Annotated[str, Field(min_length=1, max_length=200)]
    value: float | int
    formatted_value: Annotated[str, Field(min_length=1, max_length=60)]
    # Display of ``secondary_series_ref`` for the same label (e.g. a variation next to a share).
    secondary_formatted_value: Annotated[str, Field(max_length=60)] | None = None




class PullQuoteBlock(_Strict):
    type: Literal["pull_quote"] = "pull_quote"
    text: Annotated[str, Field(min_length=1, max_length=500)]
    stat: MetricCard | None = None
    source_section_keys: SourceKeys = Field(default_factory=list)


class AnnotationBlock(_Strict):
    type: Literal["annotation"] = "annotation"
    text: Annotated[str, Field(min_length=1, max_length=600)]
    source_section_keys: SourceKeys = Field(default_factory=list)


class MetricCardsBlock(_Strict):
    type: Literal["metric_cards"] = "metric_cards"
    cards: Annotated[list[MetricCard], Field(min_length=1, max_length=MAX_CARDS_PER_BLOCK)]


class BarChartBlock(_Strict):
    type: Literal["bar_chart"] = "bar_chart"
    # bars: value / max. ranking: numbered, optional secondary column. distribution: value / total of the shown items.
    variant: Literal["bars", "ranking", "distribution"] = "bars"
    title: ShortText
    series_ref: EvidenceRef
    secondary_series_ref: EvidenceRef | None = None
    secondary_label: Annotated[str, Field(max_length=60)] | None = None
    # Leave empty: the system materializes the first ``MAX_CHART_ITEMS`` items of the referenced series. A writer
    # may list a subset; every listed item is checked against the series.
    items: Annotated[list[BarItem], Field(max_length=MAX_CHART_ITEMS)] = Field(default_factory=list)
    source_section_keys: SourceKeys = Field(default_factory=list)

    @model_validator(mode="after")
    def _materialized_and_sourced(self) -> "BarChartBlock":
        if not self.items:
            raise ValueError("bar_chart items must be materialized from the referenced series")
        if not self.source_section_keys:
            raise ValueError("bar_chart must declare its source_section_keys")
        return self


ReportBlock13 = Annotated[
    Union[ParagraphBlock, BulletListBlock, TableBlock, CalloutBlock, PullQuoteBlock, AnnotationBlock,
          MetricCardsBlock, BarChartBlock],
    Field(discriminator="type"),
]

# Block types that are visual data components (counted for limits and observability).
VISUAL_BLOCK_TYPES = ("bar_chart", "metric_cards", "pull_quote")
