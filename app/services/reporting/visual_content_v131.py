"""Semantic visual components of ``FinalReport`` 1.3.1 (typed evidence references).

Same vocabulary as 1.3 (``visual_content``), with the one change that motivated 1.3.1: references are TYPED.
1.3 had a single ``EvidenceRef{section_key,key}`` that could mean a fact or a series, and a card could not point
at one item of a series. Here:

    card / hero figure   value_ref ∈ FactRef | SeriesItemRef      (+ optional secondary_value_ref, same kinds)
    bar_chart            series_ref: SeriesRef                    (+ optional secondary_series_ref: SeriesRef)

The figures themselves stay MATERIALIZED in the document (``value``, chart ``items``) so a renderer never resolves
evidence; refs are internal provenance. ``value`` may be omitted by the writer when a ``value_ref`` exists: the
system materializes it from the evidence (``visual_validation_v131``) before schema validation, so the model never
has to copy a number the backend already knows. Without a ref the figure must be literally grounded in the
authorised narrative evidence (same rule as 1.3).

1.3 models are untouched (historical artifacts keep validating); only what changed is redefined here.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .evidence_refs import SeriesRef, ValueRef
from .report_content import (
    BulletListBlock, CalloutBlock, Key, ParagraphBlock, ShortText, SourceKeys, TableBlock, _Strict,
)
from .visual_content import MAX_CARDS_PER_BLOCK, MAX_CHART_ITEMS, AnnotationBlock, BarItem

Value = Annotated[str, Field(min_length=1, max_length=80)]


class MetricCard131(_Strict):
    label: ShortText
    # Omit when ``value_ref`` is given: the system fills it with the evidence' own display string.
    value: Value | None = None
    value_ref: ValueRef | None = None
    secondary_value: Annotated[str, Field(max_length=120)] | None = None
    secondary_value_ref: ValueRef | None = None
    supporting_text: Annotated[str, Field(max_length=300)] | None = None
    # Semantics only. trend = direction of change; impact = whether that is good / bad news.
    trend: Literal["up", "down", "stable", "neutral"] | None = None
    impact: Literal["positive", "negative", "neutral"] | None = None
    source_section_keys: SourceKeys = Field(default_factory=list)

    @model_validator(mode="after")
    def _complete(self) -> "MetricCard131":
        if not self.source_section_keys:
            raise ValueError("every metric card must declare its source_section_keys")
        if self.value is None:
            raise ValueError("a metric card needs a value (from value_ref, or a figure grounded in the evidence)")
        return self


class StripMetric131(MetricCard131):
    key: Key


class PullQuoteBlock131(_Strict):
    type: Literal["pull_quote"] = "pull_quote"
    text: Annotated[str, Field(min_length=1, max_length=500)]
    stat: MetricCard131 | None = None
    source_section_keys: SourceKeys = Field(default_factory=list)


class MetricCardsBlock131(_Strict):
    type: Literal["metric_cards"] = "metric_cards"
    cards: Annotated[list[MetricCard131], Field(min_length=1, max_length=MAX_CARDS_PER_BLOCK)]


class BarChartBlock131(_Strict):
    type: Literal["bar_chart"] = "bar_chart"
    variant: Literal["bars", "ranking", "distribution"] = "bars"
    title: ShortText
    series_ref: SeriesRef
    secondary_series_ref: SeriesRef | None = None
    secondary_label: Annotated[str, Field(max_length=60)] | None = None
    # Leave empty: the system materializes the first ``MAX_CHART_ITEMS`` items of ``series_ref``.
    items: Annotated[list[BarItem], Field(max_length=MAX_CHART_ITEMS)] = Field(default_factory=list)
    source_section_keys: SourceKeys = Field(default_factory=list)

    @model_validator(mode="after")
    def _materialized_and_sourced(self) -> "BarChartBlock131":
        if not self.items:
            raise ValueError("bar_chart items must be materialized from the referenced series")
        if not self.source_section_keys:
            raise ValueError("bar_chart must declare its source_section_keys")
        return self


ReportBlock131 = Annotated[
    Union[ParagraphBlock, BulletListBlock, TableBlock, CalloutBlock, PullQuoteBlock131, AnnotationBlock,
          MetricCardsBlock131, BarChartBlock131],
    Field(discriminator="type"),
]
