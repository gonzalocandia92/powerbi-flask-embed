"""Structured Evidence: the small, versioned record of what an analytical question actually returned.

A ``ReportRunSection`` used to keep only a narrative ``answer``. That is enough to write prose but not to
draw a bar chart or fill a KPI card without a second model re-extracting numbers from text. ``ReportEvidence``
keeps the *relevant* result rows of the DAX that backed the answer, already reduced to:

* ``facts``   single values (a KPI, a comparison), raw + display formatted;
* ``series``  label/value lists (rankings, distributions, breakdowns), the thing bar charts consume;
* ``tables``  bounded row sets (for table blocks / audit);
* ``provenance`` where it came from and whether anything was truncated.

It is NOT a BI model: no schema, no DAX, no relationships, no dataset. It says nothing about layout or
visual components (that is the writer's / renderer's business). It is deterministic (built by
``evidence_extractor`` with no LLM), bounded (limits below) and versioned (``EVIDENCE_SCHEMA_VERSION``): a
persisted payload is read back through ``load_evidence`` with the version it was written with, never
reinterpreted under a newer contract.

Limits (all enforced by the extractor; truncation is always recorded, never silent):
    MAX_FACTS_PER_SECTION   = 40    MAX_SERIES_PER_SECTION = 6    MAX_TABLES_PER_SECTION = 3
    MAX_ITEMS_PER_SERIES    = 30    MAX_ROWS_PER_TABLE     = 30   MAX_TABLE_COLUMNS      = 12
(30 rows is also the most the analytics tool ever hands the model, see ``powerbi_tools``.)
"""
from __future__ import annotations

import logging
import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

LOG = logging.getLogger(__name__)

EVIDENCE_SCHEMA_VERSION = "1"

MAX_FACTS_PER_SECTION = 40
MAX_SERIES_PER_SECTION = 6
MAX_TABLES_PER_SECTION = 3
MAX_ITEMS_PER_SERIES = 30
MAX_ROWS_PER_TABLE = 30
MAX_TABLE_COLUMNS = 12

LocalKey = Annotated[str, Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
Label = Annotated[str, Field(min_length=1, max_length=200)]
Formatted = Annotated[str, Field(min_length=1, max_length=60)]
Scalar = float | int | str | None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceFact(_Strict):
    """One value. ``value`` is raw (number or text); ``formatted_value`` is how it reads to a person."""
    key: LocalKey
    label: Label
    value: float | int | str
    formatted_value: Formatted
    # "ratio": 0.4173 means 41,73 %. None: a plain number or text whose unit the result does not state.
    unit: Literal["ratio"] | None = None
    # "comparison": a variation / delta; its sign is meaningful for contradiction checks.
    kind: Literal["value", "comparison"] = "value"
    comparison_label: Annotated[str, Field(max_length=120)] | None = None


class EvidenceSeriesItem(_Strict):
    label: Label
    value: float | int
    formatted_value: Formatted


class EvidenceSeries(_Strict):
    key: LocalKey
    title: Label
    label_field: Label
    value_field: Label
    unit: Literal["ratio"] | None = None
    items: Annotated[list[EvidenceSeriesItem], Field(max_length=MAX_ITEMS_PER_SERIES)]
    total_items: int = Field(ge=0)         # rows the result had (>= len(items) when truncated)
    truncated: bool = False


class EvidenceColumn(_Strict):
    key: LocalKey
    label: Label
    kind: Literal["text", "number"]


class EvidenceTable(_Strict):
    key: LocalKey
    title: Label
    columns: Annotated[list[EvidenceColumn], Field(min_length=1, max_length=MAX_TABLE_COLUMNS)]
    rows: Annotated[list[dict[str, Scalar]], Field(max_length=MAX_ROWS_PER_TABLE)]
    total_rows: int = Field(ge=0)
    truncated: bool = False


class EvidenceProvenance(_Strict):
    source: Literal["dax_result"] = "dax_result"
    results_used: int = 0
    rows_seen: int = 0
    # Anything the limits (or the analytics tool's own cap) cut off; empty when nothing was lost.
    truncations: list[str] = Field(default_factory=list)


class ReportEvidence(_Strict):
    evidence_schema_version: Literal["1"] = EVIDENCE_SCHEMA_VERSION
    facts: Annotated[list[EvidenceFact], Field(max_length=MAX_FACTS_PER_SECTION)] = Field(default_factory=list)
    series: Annotated[list[EvidenceSeries], Field(max_length=MAX_SERIES_PER_SECTION)] = Field(default_factory=list)
    tables: Annotated[list[EvidenceTable], Field(max_length=MAX_TABLES_PER_SECTION)] = Field(default_factory=list)
    provenance: EvidenceProvenance = Field(default_factory=EvidenceProvenance)

    def is_empty(self) -> bool:
        return not (self.facts or self.series or self.tables)

    def fact(self, key: str) -> EvidenceFact | None:
        return next((item for item in self.facts if item.key == key), None)

    def series_by_key(self, key: str) -> EvidenceSeries | None:
        return next((item for item in self.series if item.key == key), None)

    def table(self, key: str) -> EvidenceTable | None:
        return next((item for item in self.tables if item.key == key), None)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def load_evidence(raw: Any, schema_version: str | None) -> ReportEvidence | None:
    """Persisted evidence -> model, or ``None`` when absent / of a version this code does not know.

    An unknown version is never coerced into the current contract (it would be silently misread); the
    section simply has no structured evidence and the report falls back to its narrative.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    if schema_version != EVIDENCE_SCHEMA_VERSION:
        LOG.warning("Unsupported evidence schema version %r; ignoring structured evidence", schema_version)
        return None
    try:
        return ReportEvidence.model_validate(raw)
    except ValueError:
        LOG.exception("Persisted evidence is invalid; ignoring it")
        return None


# ── display formatting (single home; es-AR style, deterministic, no locale/global state) ──────────────

def format_number(value: float | int, unit: str | None = None) -> str:
    """``15520540`` -> ``15.520.540``; ``0.4173`` with unit ratio -> ``41,73 %``; non-integers get 2 decimals."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("format_number needs a finite number")
    if unit == "ratio":
        return f"{_group(round(value * 100, 2), force_decimals=True)} %"
    return _group(value)


def _group(value: float | int, *, force_decimals: bool = False) -> str:
    if not force_decimals and float(value).is_integer():
        text = f"{int(value):,}"
    else:
        text = f"{value:,.2f}"
    return text.replace(",", "\0").replace(".", ",").replace("\0", ".")
