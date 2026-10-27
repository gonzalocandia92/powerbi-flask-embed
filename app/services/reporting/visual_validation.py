"""Deterministic checks for the quantitative / visual content of a ``FinalReportV13`` (V1.5). Pure, no LLM, no DB.

Central rule: every figure the report SHOWS in a structured component must trace back to evidence the composer
authorised for THAT item. Two mechanisms, strongest first:

1. ``EvidenceRef`` (``section_key`` + local ``key``): the figure must equal the referenced fact / series item
   exactly (label, raw value and formatted value). A label that exists with an altered number is rejected.
2. Grounding (no ref available, e.g. narrative-only evidence): every numeric token of the displayed value must
   occur in the authorised evidence (narrative answers, facts, series, tables). It catches invented numbers; it does
   not recompute anything, and it is NOT applied to free prose paragraphs (documented limit).

Plus: refs must point at authorised, successful evidence and be cited in ``source_section_keys``; metric trends must
not contradict the sign of the comparison fact they cite; a hero headline must not state the opposite direction of the
(all-same-sign) comparison facts it cites. Messages name paths and expected values, never echo free model text.

``materialize_visuals`` runs BEFORE schema validation: it fills ``bar_chart.items`` from the referenced series
(so the writer never copies 15 numbers) and the optional secondary column. It only fills what is EMPTY: a figure the
writer typed itself is left alone for the validator to judge.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .composed_report import ComposedReportInput, EvidenceRecord
from .evidence import EvidenceFact, EvidenceSeries
from .visual_content import MAX_CHART_ITEMS

_NUMBER = re.compile(r"\d[\d.,]*")
_NUMERIC_CELL = re.compile(r"^[\s$€£%+\-−–()\d.,/:]+$")
_UP = re.compile(r"\b(crecieron|crecen|crecimiento|creci[oó]|aumentaron|aument[oó]|aumenta|subieron|subi[oó]|suba|"
                 r"alza|mejoraron|mejor[oó]|incremento|incrementaron)\b", re.I)
_DOWN = re.compile(r"\b(cayeron|cay[oó]|ca[ií]da|cae|bajaron|baj[oó]|baja|disminuyeron|disminuy[oó]|descenso|"
                   r"retroceso|redujeron|redujo|deterioro|empeoraron)\b", re.I)


# ── markup / presentation leaks ──────────────────────────────────────────────────────────────

_MARKUP = re.compile(
    r"(<\s*/?\s*[a-zA-Z!]|&lt;\s*/?\s*[a-zA-Z]|javascript:|data:\w+/|\bhttps?://|\bwww\.|//[a-z0-9.-]+\.[a-z]{2,}|"
    r"\burl\s*\(|@import|\b(?:color|background(?:-color)?|font-(?:size|family)|margin|padding|display|position)\s*:\s*\S|\bstyle\s*=|\bon\w+\s*=|expression\s*\(|\{\s*[\w-]+\s*:[^}]*\})", re.I)


def markup_errors(report_json: object, path: str = "") -> list[str]:
    """Paths of strings that look like HTML / CSS / SVG / URLs / scripts. The writer produces semantics only."""
    found: list[str] = []
    if isinstance(report_json, str):
        if _MARKUP.search(report_json):
            found.append(f"{path or '(raíz)'}: no puede contener HTML, CSS, SVG, scripts ni URLs; sólo texto plano.")
    elif isinstance(report_json, dict):
        for key, value in report_json.items():
            found += markup_errors(value, f"{path}.{key}" if path else str(key))
    elif isinstance(report_json, list):
        for index, value in enumerate(report_json):
            found += markup_errors(value, f"{path}[{index}]")
    return found


# ── numbers ──────────────────────────────────────────────────────────────────────────────────

def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _candidates(token: str) -> set[str]:
    """Canonical numeric readings of a token ("1.872" is 1872 or 1.872; "15.520.540,00" only 15520540)."""
    token = token.strip(".,")
    out: set[str] = set()

    def add(text: str) -> None:
        try:
            out.add(_plain(Decimal(text)))
        except InvalidOperation:
            pass

    if "." in token and "," in token:
        decimal_sep = "." if token.rfind(".") > token.rfind(",") else ","
        thousand = "," if decimal_sep == "." else "."
        add(token.replace(thousand, "").replace(decimal_sep, "."))
    elif "." in token or "," in token:
        sep = "." if "." in token else ","
        parts = token.split(sep)
        add(token.replace(sep, "."))                       # as a decimal separator
        if all(len(part) == 3 for part in parts[1:]) and 1 <= len(parts[0]) <= 3 and parts[0] != "0":
            add("".join(parts))                             # as a thousands separator
    else:
        add(token)
    return out


def number_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for token in _NUMBER.findall(text or ""):
        out |= _candidates(token)
    return out


def _value_tokens(value: float | int | None) -> set[str]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return set()
    out = {_plain(Decimal(repr(value)))}
    if abs(value) <= 10:                       # a ratio is read as a percentage ("0.4173" -> "41,73 %")
        out.add(_plain(Decimal(repr(round(value * 100, 2)))))
    return out


def evidence_universe(records: Iterable[EvidenceRecord]) -> set[str]:
    """Every numeric token the authorised evidence supports (narrative + structured)."""
    tokens: set[str] = set()
    for record in records:
        tokens |= number_tokens(record.answer or "")
        for fact in record.facts:
            tokens |= _value_tokens(fact.value) if not isinstance(fact.value, str) else number_tokens(fact.value)
            tokens |= number_tokens(fact.formatted_value)
        for series in record.series:
            for item in series.items:
                tokens |= _value_tokens(item.value) | number_tokens(item.formatted_value)
        for table in record.tables:
            for row in table.rows:
                for cell in row.values():
                    tokens |= _value_tokens(cell) if not isinstance(cell, str) else number_tokens(cell)
    return tokens


def _ungrounded(text: str, universe: set[str]) -> list[str]:
    """Numeric tokens of ``text`` none of whose readings occurs in the universe."""
    bad = []
    for token in _NUMBER.findall(text or ""):
        if not (_candidates(token) & universe):
            bad.append(token.strip(".,"))
    return sorted(set(bad))


def _same_number(a: float | int, b: float | int) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


# ── materialization (before schema validation) ───────────────────────────────────────────────

def _resolve_series(composed: ComposedReportInput, ref: Any, allowed: set[str]) -> EvidenceSeries | None:
    if not isinstance(ref, dict) or ref.get("section_key") not in allowed:
        return None
    record = composed.evidence_by_key().get(ref.get("section_key"))
    return next((s for s in record.series if s.key == ref.get("key")), None) if record else None


def materialize_visuals(data: dict, composed: ComposedReportInput) -> list[str]:
    """Fill empty ``bar_chart.items`` (and the secondary column) from the referenced series, in place.

    Returns errors only for charts whose series cannot be resolved (nothing to fill from); everything else is left
    for ``validate_visuals``. Never overwrites something the writer wrote.
    """
    errors: list[str] = []
    items = data.get("items")
    if not isinstance(items, list):
        return errors
    for index, (want, got) in enumerate(zip(composed.items, items)):
        if want.type != "section" or not isinstance(got, dict):
            continue
        allowed = composed.item_ok_keys(index)
        for slot in ("blocks", "secondary_blocks"):
            for position, block in enumerate(got.get(slot) or []):
                if not isinstance(block, dict) or block.get("type") != "bar_chart":
                    continue
                where = f"items[{index}].{slot}[{position}]"
                series = _resolve_series(composed, block.get("series_ref"), allowed)
                if series is None:
                    errors.append(f"{where}.series_ref: no existe una serie autorizada con esa referencia; "
                                  f"disponibles: {_available_series(composed, allowed)}.")
                    continue
                secondary = _resolve_series(composed, block.get("secondary_series_ref"), allowed) \
                    if block.get("secondary_series_ref") else None
                if not block.get("items"):
                    by_label = {item.label: item.formatted_value for item in secondary.items} if secondary else {}
                    block["items"] = [
                        {"label": item.label, "value": item.value, "formatted_value": item.formatted_value,
                         **({"secondary_formatted_value": by_label[item.label]} if item.label in by_label else {})}
                        for item in series.items[:MAX_CHART_ITEMS]]
    return errors


def _available_series(composed: ComposedReportInput, allowed: set[str]) -> list[str]:
    return sorted(f"{record.key}/{series.key}" for record in composed.evidence if record.key in allowed
                  for series in record.series)[:20]


# ── validation (after schema validation) ─────────────────────────────────────────────────────

class _Ctx:
    def __init__(self, composed: ComposedReportInput, index: int):
        self.composed, self.index = composed, index
        self.allowed = composed.item_ok_keys(index)
        self.records = composed.evidence_by_key()
        self.universe = evidence_universe(self.records[k] for k in self.allowed if k in self.records)
        self.errors: list[str] = []

    def err(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def sources(self, where: str, keys: Iterable[str]) -> None:
        bad = sorted(set(keys) - self.allowed)
        if bad:
            self.err(f"{where}.source_section_keys",
                     f"no autorizadas {bad}; sólo podés citar análisis exitosos de este componente: "
                     f"{sorted(self.allowed)}.")

    def fact(self, where: str, ref, cited: Iterable[str]) -> EvidenceFact | None:
        record = self.records.get(ref.section_key)
        if ref.section_key not in self.allowed or record is None:
            self.err(where, f"la evidencia {ref.section_key!r} no está autorizada para este componente.")
            return None
        found = next((f for f in record.facts if f.key == ref.key), None)
        if found is None:
            self.err(where, f"no existe el fact {ref.key!r} en la evidencia {ref.section_key!r}; "
                            f"disponibles: {[f.key for f in record.facts][:20]}.")
        elif ref.section_key not in set(cited):
            self.err(where, f"{ref.section_key!r} debe figurar en source_section_keys.")
        return found

    def series(self, where: str, ref, cited: Iterable[str]) -> EvidenceSeries | None:
        record = self.records.get(ref.section_key)
        if ref.section_key not in self.allowed or record is None:
            self.err(where, f"la evidencia {ref.section_key!r} no está autorizada para este componente.")
            return None
        found = next((s for s in record.series if s.key == ref.key), None)
        if found is None:
            self.err(where, f"no existe la serie {ref.key!r} en la evidencia {ref.section_key!r}; "
                            f"disponibles: {[s.key for s in record.series][:20]}.")
        elif ref.section_key not in set(cited):
            self.err(where, f"{ref.section_key!r} debe figurar en source_section_keys.")
        return found


def _check_figure(ctx: _Ctx, where: str, text: str | None, ref, cited, *, label: str) -> EvidenceFact | None:
    """``text`` is a displayed figure; with ``ref`` it must be that fact's display, otherwise grounded."""
    if ref is not None:
        fact = ctx.fact(f"{where}.fact_ref", ref, cited)
        if fact is not None and text is not None and text.strip() != fact.formatted_value \
                and not (number_tokens(text) and number_tokens(text) == number_tokens(fact.formatted_value)):
            ctx.err(f"{where}.{label}", f"debe ser el valor de la evidencia ({fact.formatted_value!r}).")
        return fact
    if text:
        bad = _ungrounded(text, ctx.universe)
        if bad:
            ctx.err(f"{where}.{label}", f"cifras sin respaldo en la evidencia autorizada: {bad}.")
    return None


def _check_card(ctx: _Ctx, where: str, card) -> None:
    ctx.sources(where, card.source_section_keys)
    fact = _check_figure(ctx, where, card.value, card.fact_ref, card.source_section_keys, label="value")
    if card.secondary_fact_ref is not None:
        second = ctx.fact(f"{where}.secondary_fact_ref", card.secondary_fact_ref, card.source_section_keys)
        if second is not None and card.secondary_value is not None \
                and second.formatted_value not in card.secondary_value:
            ctx.err(f"{where}.secondary_value", f"debe contener el valor de la evidencia ({second.formatted_value!r}).")
    elif card.secondary_value:
        bad = _ungrounded(card.secondary_value, ctx.universe)
        if bad:
            ctx.err(f"{where}.secondary_value", f"cifras sin respaldo en la evidencia autorizada: {bad}.")
    for fact_ in (fact,):
        if fact_ is not None and fact_.kind == "comparison" and isinstance(fact_.value, (int, float)) \
                and card.trend in ("up", "down") and fact_.value != 0:
            if (card.trend == "up") != (fact_.value > 0):
                ctx.err(f"{where}.trend", f"contradice el signo de la evidencia ({fact_.formatted_value}).")
    if card.supporting_text:
        bad = _ungrounded(card.supporting_text, ctx.universe)
        if bad:
            ctx.err(f"{where}.supporting_text", f"cifras sin respaldo en la evidencia autorizada: {bad}.")


def _check_chart(ctx: _Ctx, where: str, chart) -> None:
    ctx.sources(where, chart.source_section_keys)
    series = ctx.series(f"{where}.series_ref", chart.series_ref, chart.source_section_keys)
    secondary = None
    if chart.secondary_series_ref is not None:
        secondary = ctx.series(f"{where}.secondary_series_ref", chart.secondary_series_ref, chart.source_section_keys)
    elif any(item.secondary_formatted_value for item in chart.items):
        ctx.err(f"{where}.items", "secondary_formatted_value requiere secondary_series_ref.")
    if series is None:
        return
    for position, item in enumerate(chart.items):
        match = [s for s in series.items if s.label == item.label]
        if not match:
            ctx.err(f"{where}.items[{position}].label", f"no existe en la serie {series.key!r}.")
            continue
        if not any(_same_number(s.value, item.value) for s in match):
            ctx.err(f"{where}.items[{position}].value", "no coincide con la evidencia de esa etiqueta.")
        elif not any(s.formatted_value == item.formatted_value and _same_number(s.value, item.value) for s in match):
            ctx.err(f"{where}.items[{position}].formatted_value", "no coincide con la evidencia de esa etiqueta.")
        if secondary is not None and item.secondary_formatted_value is not None:
            if not any(s.label == item.label and s.formatted_value == item.secondary_formatted_value
                       for s in secondary.items):
                ctx.err(f"{where}.items[{position}].secondary_formatted_value",
                        "no coincide con la serie secundaria de esa etiqueta.")


def _check_table(ctx: _Ctx, where: str, table) -> None:
    for r, row in enumerate(table.rows):
        for column, cell in row.items():
            if _NUMERIC_CELL.match(cell.strip() or "x") and any(c.isdigit() for c in cell):
                bad = _ungrounded(cell, ctx.universe)
                if bad:
                    ctx.err(f"{where}.rows[{r}].{column}", f"cifras sin respaldo en la evidencia autorizada: {bad}.")


def _check_block(ctx: _Ctx, where: str, block) -> None:
    if block.type == "bar_chart":
        _check_chart(ctx, where, block)
    elif block.type == "metric_cards":
        for position, card in enumerate(block.cards):
            _check_card(ctx, f"{where}.cards[{position}]", card)
    elif block.type == "pull_quote":
        ctx.sources(where, block.source_section_keys)
        if block.stat is not None:
            _check_card(ctx, f"{where}.stat", block.stat)
    elif block.type == "annotation":
        ctx.sources(where, block.source_section_keys)
    elif block.type == "table":
        _check_table(ctx, where, block)


def direction_conflict(text: str, facts: Iterable[EvidenceFact]) -> str | None:
    """"crecieron" over comparison facts that are all negative (or the reverse); conservative on purpose."""
    signs = {(f.value > 0) - (f.value < 0) for f in facts
             if f.kind == "comparison" and isinstance(f.value, (int, float)) and f.value != 0}
    if len(signs) != 1:
        return None
    up, down = bool(_UP.search(text)), bool(_DOWN.search(text))
    if up and not down and signs == {-1}:
        return "afirma un aumento pero la evidencia citada muestra una baja"
    if down and not up and signs == {1}:
        return "afirma una baja pero la evidencia citada muestra un aumento"
    return None


def validate_item_visuals(item, index: int, composed: ComposedReportInput) -> list[str]:
    """Quantitative / visual checks of one FinalReport 1.3 item against the evidence authorised for it."""
    ctx = _Ctx(composed, index)
    where = f"items[{index}]"
    if item.type == "section" and item.status == "ok":
        for slot, blocks in (("blocks", item.blocks), ("secondary_blocks", item.secondary_blocks)):
            for position, block in enumerate(blocks):
                _check_block(ctx, f"{where}.{slot}[{position}]", block)
    elif item.type == "metric_strip":
        for position, metric in enumerate(item.metrics):
            _check_card(ctx, f"{where}.metrics[{position}]", metric)
    elif item.type == "hero":
        cited = set(item.headline_source_section_keys) | {k for h in item.highlights for k in h.source_section_keys}
        ctx.sources(where, cited)
        if item.highlight is not None:
            highlight = item.highlight
            ctx.sources(f"{where}.highlight", highlight.source_section_keys)
            _check_figure(ctx, f"{where}.highlight", highlight.value, highlight.fact_ref,
                          highlight.source_section_keys, label="value")
            cited |= set(highlight.source_section_keys)
        facts = [f for key in cited if key in ctx.records for f in ctx.records[key].facts]
        for field, text in (("headline", item.headline), ("deck", item.deck or "")):
            conflict = direction_conflict(text, facts)
            if conflict:
                ctx.err(f"{where}.{field}", f"{conflict}.")
    return ctx.errors
