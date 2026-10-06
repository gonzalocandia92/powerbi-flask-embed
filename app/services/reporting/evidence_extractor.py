"""Deterministic ``DAX result rows -> ReportEvidence``. No LLM, no Power BI, no DB, no layout.

Input is what ``AnalyticsExecutor`` already captured for the answer (``AnalyticsResult.dax_results``): a list
of ``{"rows": [...], "total_rows": int, "truncated": bool}`` where each row is a Power BI row
(``{"Sucursal[Nombre]": "San Martin", "[Ventas]": 20425450}``). The extractor reduces them to facts / series /
tables under the limits of ``evidence``; it never recomputes anything (no shares, no totals, no deltas): if a
share or a delta is in the evidence it is because the query returned it as a column.

Shape rules, per result:
* one row                      -> one fact per non-null column (numbers and short text);
* several rows                 -> one table, and, when a text column exists, one series per numeric column
                                  labelled by the FIRST text column.

Which of these becomes a KPI, a bar chart or prose is decided later by the writer, inside the limits the
composer gives it.
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Iterable

from .evidence import (
    MAX_FACTS_PER_SECTION, MAX_ITEMS_PER_SERIES, MAX_ROWS_PER_TABLE, MAX_SERIES_PER_SECTION, MAX_TABLE_COLUMNS,
    MAX_TABLES_PER_SECTION, EvidenceColumn, EvidenceFact, EvidenceProvenance, EvidenceSeries, EvidenceSeriesItem,
    EvidenceTable, ReportEvidence, format_number,
)

# Column names that suggest a proportion / variation. They only choose the DISPLAY unit (ratio -> "41,73 %")
# and only when every value is a plausible fraction; they never change the raw value.
_RATIO_HINT = re.compile(r"(%|pct|porc|share|particip|variaci|delta|crec|growth|margen|mix)", re.I)
_COMPARISON_HINT = re.compile(r"(variaci|delta|vs\b|cambio|diferenc|crec|growth|\bdif\b)", re.I)
_BRACKET = re.compile(r"\[([^\]]+)\]\s*$")


def _clean_label(column: str) -> str:
    match = _BRACKET.search(column)
    label = (match.group(1) if match else column).strip()
    return label or "Valor"


def _slug(text: str, fallback: str, limit: int = 36) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_text).strip("_")[:limit].strip("_")
    return slug or fallback


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _unique(base: str, used: set[str]) -> str:
    key, index = base, 2
    while key in used:
        key, index = f"{base}_{index}", index + 1
    used.add(key)
    return key


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _cell(value: Any) -> float | int | str | None:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None  # NaN / Infinity are not data (and are not valid JSON)
    if _is_number(value):
        return value
    if isinstance(value, bool):
        return "Sí" if value else "No"
    return _clip(str(value).strip(), 300) or None


def _ratio_unit(label: str, values: Iterable[float | int]) -> str | None:
    numbers = list(values)
    if not numbers or not _RATIO_HINT.search(label):
        return None
    if all(abs(v) <= 10 for v in numbers) and any(not float(v).is_integer() for v in numbers):
        return "ratio"
    return None


class _Builder:
    def __init__(self) -> None:
        self.facts: list[EvidenceFact] = []
        self.series: list[EvidenceSeries] = []
        self.tables: list[EvidenceTable] = []
        self.truncations: list[str] = []
        self._fact_keys: set[str] = set()
        self._series_keys: set[str] = set()
        self._table_keys: set[str] = set()
        self._omitted = {"facts": 0, "series": 0, "tables": 0}

    # ── single row -> facts ──────────────────────────────────────────────────────────────
    def add_single_row(self, row: dict[str, Any]) -> None:
        for column, raw in row.items():
            cell = _cell(raw)
            if cell is None:
                continue
            label = _clean_label(str(column))
            if _is_number(cell):
                unit = _ratio_unit(label, [cell])
                formatted = format_number(cell, unit)
                kind = "comparison" if _COMPARISON_HINT.search(label) else "value"
            else:
                unit, formatted, kind = None, _clip(str(cell), 60), "value"
            if len(self.facts) >= MAX_FACTS_PER_SECTION:
                self._omitted["facts"] += 1
                continue
            self.facts.append(EvidenceFact(
                key=_unique(_slug(label, "valor"), self._fact_keys), label=_clip(label, 200), value=cell,
                formatted_value=formatted, unit=unit, kind=kind))

    # ── several rows -> table + series ───────────────────────────────────────────────────
    def add_rows(self, rows: list[dict[str, Any]], total_rows: int) -> None:
        columns = list(dict.fromkeys(str(column) for row in rows for column in row))
        if not columns:
            return
        kept = columns[:MAX_TABLE_COLUMNS]
        if len(columns) > len(kept):
            self.truncations.append(f"table_columns: {len(columns) - len(kept)} omitted")
        numeric = {c for c in columns if any(_is_number(r.get(c)) for r in rows)
                   and all(r.get(c) is None or _is_number(r.get(c)) for r in rows)}
        labels = {c: _clean_label(c) for c in columns}
        self._add_table(rows, kept, numeric, labels, total_rows)
        text_columns = [c for c in columns if c not in numeric]
        if text_columns:
            for column in (c for c in columns if c in numeric):
                self._add_series(rows, text_columns[0], column, labels, total_rows)

    def _add_table(self, rows, kept, numeric, labels, total_rows) -> None:
        if len(self.tables) >= MAX_TABLES_PER_SECTION:
            self._omitted["tables"] += 1
            return
        used: set[str] = set()
        keys = {c: _unique(_slug(labels[c], "col", 30), used) for c in kept}
        shown = rows[:MAX_ROWS_PER_TABLE]
        truncated = total_rows > len(shown)
        if truncated:
            self.truncations.append(f"table_rows: {len(shown)} of {total_rows}")
        table_key = _unique(f"t{len(self.tables) + 1}", self._table_keys)
        self.tables.append(EvidenceTable(
            key=table_key, title=_clip(", ".join(labels[c] for c in kept[:3]), 200),
            columns=[EvidenceColumn(key=keys[c], label=_clip(labels[c], 200),
                                    kind="number" if c in numeric else "text") for c in kept],
            rows=[{keys[c]: _cell(r.get(c)) for c in kept} for r in shown],
            total_rows=total_rows, truncated=truncated))

    def _add_series(self, rows, label_column, value_column, labels, total_rows) -> None:
        if len(self.series) >= MAX_SERIES_PER_SECTION:
            self._omitted["series"] += 1
            return
        values = [r.get(value_column) for r in rows if _is_number(r.get(value_column))]
        unit = _ratio_unit(labels[value_column], values)
        items = [
            EvidenceSeriesItem(label=_clip(str(r.get(label_column) or "(sin dato)").strip() or "(sin dato)", 200),
                               value=r[value_column], formatted_value=format_number(r[value_column], unit))
            for r in rows if _is_number(r.get(value_column))]
        if not items:
            return
        shown = items[:MAX_ITEMS_PER_SERIES]
        # Lost either to our cap or upstream (the analytics tool returned fewer rows than the query had).
        truncated = len(items) > len(shown) or total_rows > len(rows)
        if truncated:
            self.truncations.append(f"series_items: {len(shown)} of {total_rows}")
        key = _unique(f"{_slug(labels[value_column], 'valor', 24)}_by_{_slug(labels[label_column], 'item', 24)}",
                      self._series_keys)
        self.series.append(EvidenceSeries(
            key=key, title=_clip(f"{labels[value_column]} por {labels[label_column]}", 200),
            label_field=_clip(labels[label_column], 200), value_field=_clip(labels[value_column], 200), unit=unit,
            items=shown, total_items=max(total_rows, len(items)), truncated=truncated))

    def build(self, results_used: int, rows_seen: int) -> ReportEvidence | None:
        for name, count in self._omitted.items():
            if count:
                self.truncations.append(f"{name}: {count} omitted by limit")
        if not (self.facts or self.series or self.tables):
            return None
        return ReportEvidence(
            facts=self.facts, series=self.series, tables=self.tables,
            provenance=EvidenceProvenance(results_used=results_used, rows_seen=rows_seen,
                                          truncations=self.truncations[:20]))


def extract_evidence(dax_results: Iterable[dict[str, Any]] | None) -> ReportEvidence | None:
    """Reduce captured DAX results to ``ReportEvidence``; ``None`` when there is nothing structured to keep."""
    builder = _Builder()
    used = seen = 0
    for result in dax_results or ():
        rows = [row for row in (result.get("rows") if isinstance(result, dict) else None) or ()
                if isinstance(row, dict) and row]
        if not rows:
            continue
        total = result.get("total_rows") if isinstance(result.get("total_rows"), int) else len(rows)
        used += 1
        seen += max(total, len(rows))
        if result.get("truncated"):
            builder.truncations.append(f"dax_rows: {len(rows)} of {total} returned by the analytics tool")
        if len(rows) == 1:
            builder.add_single_row(rows[0])
        else:
            builder.add_rows(rows, max(total, len(rows)))
    return builder.build(used, seen)


def is_comparison_label(label: str) -> bool:
    """Whether a column / series label reads as a variation (its sign is meaningful). Same rule as ``kind``."""
    return bool(_COMPARISON_HINT.search(label or ""))
