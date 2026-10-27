"""Materialization and validation of the quantitative / visual content of a ``FinalReportV131`` (V1.5.1).

Same guarantees as ``visual_validation`` (1.3) — every shown figure must trace to evidence the composer authorised for
THAT item — over TYPED references (``evidence_refs``):

* ``materialize_visuals_v131`` runs BEFORE schema validation, on the raw writer JSON. For every card / hero figure with a
  ``value_ref`` it fills the EMPTY ``value`` (and ``secondary_value`` for ``secondary_value_ref``) with the evidence' own
  display string, and for every ``bar_chart`` it fills the EMPTY ``items``. A figure the writer typed itself is never
  overwritten: the validator judges it. Unresolvable refs are returned as type-aware errors (the repair input), e.g.
  "'x' existe como SERIES, no como FACT"; nothing is accepted "because the number appears somewhere in the answer".
* ``validate_item_visuals_v131`` runs AFTER schema validation and re-checks everything (defense in depth): refs resolve,
  figures equal the evidence, refs are cited in ``source_section_keys``, trends do not contradict the sign of a
  variation, tables and supporting text are grounded, a hero headline does not state the opposite direction.

No provenance rule was relaxed relative to 1.3; refs just became expressive enough to say what the writer means.
"""
from __future__ import annotations

from typing import Any, Iterable

from .composed_report import ComposedReportInput
from .evidence_extractor import is_comparison_label
from .evidence_refs import GUIDANCE, EvidenceResolver, FactRef, SeriesItemRef, infer_ref_type
from .visual_content import MAX_CHART_ITEMS
from .visual_validation import (
    _NUMERIC_CELL, _same_number, _ungrounded, direction_conflict, evidence_universe, number_tokens,
)

_LEGACY_FIELDS = {
    "fact_ref": "value_ref", "secondary_fact_ref": "secondary_value_ref",
}


def _with_type(raw: Any) -> Any:
    """Raw ref dict with a missing ``type`` filled when the fields allow exactly one reading."""
    if isinstance(raw, dict) and raw.get("type") is None and (kind := infer_ref_type(raw)):
        raw["type"] = kind
    return raw


# ── materialization ─────────────────────────────────────────────────────────────────────────────

def _fill_card(card: Any, where: str, resolver: EvidenceResolver, errors: list[str],
               inherited: list[str] | None = None) -> None:
    if not isinstance(card, dict):
        return
    if not card.get("source_section_keys"):
        # A card's provenance is the evidence it points at (or its container's); each key is validated later.
        derived = [raw["section_key"] for name in ("value_ref", "secondary_value_ref")
                   if isinstance(raw := card.get(name), dict) and isinstance(raw.get("section_key"), str)]
        card["source_section_keys"] = list(dict.fromkeys(derived)) or list(inherited or ())
    for old, new in _LEGACY_FIELDS.items():
        if old in card:
            errors.append(f"{where}.{old}: ese campo es de FinalReport 1.3; usá {new} con un FactRef o SeriesItemRef "
                          f"tipado (type 'fact' | 'series_item'). {GUIDANCE}")
    for ref_field, value_field in (("value_ref", "value"), ("secondary_value_ref", "secondary_value")):
        raw = card.get(ref_field)
        if raw is None:
            continue
        resolved = resolver.value_from_raw(_with_type(raw), where=f"{where}.{ref_field}")
        if not resolved.ok:
            errors.append(resolved.error)
        elif card.get(value_field) in (None, ""):
            card[value_field] = resolved.formatted


def _fill_chart(chart: dict, where: str, resolver: EvidenceResolver, errors: list[str]) -> None:
    if "series_ref" not in chart or chart.get("series_ref") is None:
        errors.append(f"{where}.series_ref: un bar_chart necesita un SeriesRef (type 'series'). {GUIDANCE}")
        return
    primary = resolver.series_from_raw(_with_type(chart["series_ref"]), where=f"{where}.series_ref")
    secondary = None
    if chart.get("secondary_series_ref") is not None:
        secondary = resolver.series_from_raw(_with_type(chart["secondary_series_ref"]),
                                             where=f"{where}.secondary_series_ref")
        if not secondary.ok:
            errors.append(secondary.error)
    if not primary.ok:
        errors.append(primary.error)
        return
    if not chart.get("items"):
        by_label = {i.label: i.formatted_value for i in secondary.series.items} if secondary and secondary.ok else {}
        chart["items"] = [
            {"label": i.label, "value": i.value, "formatted_value": i.formatted_value,
             **({"secondary_formatted_value": by_label[i.label]} if i.label in by_label else {})}
            for i in primary.series.items[:MAX_CHART_ITEMS]]


def _declared_sources(section: dict) -> list[str]:
    """Union (in order) of the evidence keys the blocks of a section already declare or reference."""
    found: list[str] = []

    def add(key: Any) -> None:
        if isinstance(key, str) and key not in found:
            found.append(key)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in node.get("source_section_keys") or ():
                add(key)
            for name in ("value_ref", "secondary_value_ref", "series_ref", "secondary_series_ref"):
                if isinstance(node.get(name), dict):
                    add(node[name].get("section_key"))
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk([section.get("blocks") or [], section.get("secondary_blocks") or []])
    return found


def materialize_visuals_v131(data: dict, composed: ComposedReportInput) -> list[str]:
    """Fill what the system knows (card figures, chart items) in place; return type-aware errors for bad refs."""
    errors: list[str] = []
    items = data.get("items")
    if not isinstance(items, list):
        return errors
    for index, (want, got) in enumerate(zip(composed.items, items)):
        if not isinstance(got, dict):
            continue
        resolver = EvidenceResolver(composed.evidence_by_key(), composed.item_ok_keys(index))
        where = f"items[{index}]"
        if want.type == "executive_summary":
            _fill_card(got.get("highlight"), f"{where}.highlight", resolver, errors)
        elif want.type == "kpi_grid":
            for position, metric in enumerate(got.get("metrics") or []):
                _fill_card(metric, f"{where}.metrics[{position}]", resolver, errors)
        elif want.type == "section":
            # A section's own provenance is the union of what its blocks declare (each key is still validated
            # against the item's authorised evidence): writers often cite sources on blocks and forget the section.
            if got.get("status", "ok") == "ok" and not got.get("source_section_keys"):
                got["source_section_keys"] = [k for k in _declared_sources(got) if k in composed.item_ok_keys(index)]
            for slot in ("blocks", "secondary_blocks"):
                for position, block in enumerate(got.get(slot) or []):
                    if not isinstance(block, dict):
                        continue
                    at = f"{where}.{slot}[{position}]"
                    if block.get("type") == "metric_cards":
                        for number, card in enumerate(block.get("cards") or []):
                            _fill_card(card, f"{at}.cards[{number}]", resolver, errors)
                    elif block.get("type") == "pull_quote":
                        _fill_card(block.get("stat"), f"{at}.stat", resolver, errors,
                                   inherited=block.get("source_section_keys"))
                    elif block.get("type") == "bar_chart":
                        _fill_chart(block, at, resolver, errors)
    return errors


# ── validation ──────────────────────────────────────────────────────────────────────────────────

class _Ctx:
    def __init__(self, composed: ComposedReportInput, index: int):
        self.composed, self.index = composed, index
        self.allowed = composed.item_ok_keys(index)
        self.records = composed.evidence_by_key()
        self.resolver = EvidenceResolver(self.records, self.allowed)
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

    def cited(self, where: str, ref, cited: Iterable[str]) -> None:
        if ref.section_key not in set(cited):
            self.err(where, f"{ref.section_key!r} debe figurar en source_section_keys.")


def _matches_display(text: str, formatted: str) -> bool:
    return text.strip() == formatted or (bool(number_tokens(text)) and number_tokens(text) == number_tokens(formatted))


def _check_figure(ctx: _Ctx, where: str, ref, text: str | None, cited, *, field: str, contains: bool = False):
    """Figure ``text`` vs its typed ref (exact display) or, without a ref, grounded in the authorised evidence."""
    if ref is not None:
        resolved = ctx.resolver.value(ref)
        if not resolved.ok:
            ctx.err(f"{where}.{field}_ref", resolved.error)
            return None
        ctx.cited(f"{where}.{field}_ref", ref, cited)
        shown = resolved.formatted
        if text is not None and not (shown in text if contains else _matches_display(text, shown)):
            ctx.err(f"{where}.{field}", f"debe {'contener' if contains else 'ser'} el valor de la evidencia ({shown!r}).")
        return resolved
    if text:
        bad = _ungrounded(text, ctx.universe)
        if bad:
            ctx.err(f"{where}.{field}", f"cifras sin respaldo en la evidencia autorizada: {bad}.")
    return None


def _signed_comparison(resolved):
    """Numeric value of a resolved figure when its sign is a variation, else ``None``."""
    if resolved is None:
        return None
    if resolved.fact is not None:
        return resolved.fact.value if resolved.fact.kind == "comparison" and isinstance(resolved.fact.value, (int, float)) else None
    if resolved.item is not None and resolved.series is not None and is_comparison_label(resolved.series.value_field):
        return resolved.item.value
    return None


def _check_card(ctx: _Ctx, where: str, card) -> None:
    ctx.sources(where, card.source_section_keys)
    resolved = _check_figure(ctx, where, card.value_ref, card.value, card.source_section_keys, field="value")
    _check_figure(ctx, where, card.secondary_value_ref, card.secondary_value, card.source_section_keys,
                  field="secondary_value", contains=True)
    signed = _signed_comparison(resolved)
    if signed and card.trend in ("up", "down") and (card.trend == "up") != (signed > 0):
        ctx.err(f"{where}.trend", f"contradice el signo de la evidencia ({resolved.formatted}).")
    if card.supporting_text:
        bad = _ungrounded(card.supporting_text, ctx.universe)
        if bad:
            ctx.err(f"{where}.supporting_text", f"cifras sin respaldo en la evidencia autorizada: {bad}.")


def _check_chart(ctx: _Ctx, where: str, chart) -> None:
    ctx.sources(where, chart.source_section_keys)
    primary = ctx.resolver.series(chart.series_ref)
    if not primary.ok:
        ctx.err(f"{where}.series_ref", primary.error)
        return
    ctx.cited(f"{where}.series_ref", chart.series_ref, chart.source_section_keys)
    secondary = None
    if chart.secondary_series_ref is not None:
        secondary = ctx.resolver.series(chart.secondary_series_ref)
        if not secondary.ok:
            ctx.err(f"{where}.secondary_series_ref", secondary.error)
            secondary = None
        else:
            ctx.cited(f"{where}.secondary_series_ref", chart.secondary_series_ref, chart.source_section_keys)
    elif any(item.secondary_formatted_value for item in chart.items):
        ctx.err(f"{where}.items", "secondary_formatted_value requiere secondary_series_ref.")
    series = primary.series
    for position, item in enumerate(chart.items):
        at = f"{where}.items[{position}]"
        match = [s for s in series.items if s.label == item.label]
        if not match:
            ctx.err(f"{at}.label", f"no existe en la serie {series.key!r}.")
            continue
        if not any(_same_number(s.value, item.value) for s in match):
            ctx.err(f"{at}.value", "no coincide con la evidencia de esa etiqueta.")
        elif not any(s.formatted_value == item.formatted_value and _same_number(s.value, item.value) for s in match):
            ctx.err(f"{at}.formatted_value", "no coincide con la evidencia de esa etiqueta.")
        if secondary is not None and item.secondary_formatted_value is not None:
            if not any(s.label == item.label and s.formatted_value == item.secondary_formatted_value
                       for s in secondary.series.items):
                ctx.err(f"{at}.secondary_formatted_value", "no coincide con la serie secundaria de esa etiqueta.")


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


def validate_item_visuals_v131(item, index: int, composed: ComposedReportInput) -> list[str]:
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
            _check_figure(ctx, f"{where}.highlight", highlight.value_ref, highlight.value,
                          highlight.source_section_keys, field="value")
            cited |= set(highlight.source_section_keys)
        facts = [f for key in cited if key in ctx.records for f in ctx.records[key].facts]
        for field, text in (("headline", item.headline), ("deck", item.deck or "")):
            conflict = direction_conflict(text, facts)
            if conflict:
                ctx.err(f"{where}.{field}", f"{conflict}.")
    return ctx.errors
