"""Deterministic post-writer validation of a ``FinalReportV13`` against its ``ComposedReportInput`` (v2).

Same contract as ``composition_validation`` (1.2): the composed input is the authority on WHAT items exist, in WHAT
order and with WHICH evidence; the prompt only asks. Differences for 1.3:

* the StructureSpec vocabulary maps onto the 1.3 item names (``executive_summary -> hero``, ``kpi_grid -> metric_strip``,
  ``attention_points -> attention_grid``); the item COUNT and ORDER are still fixed by the composer;
* provenance also covers the visual components inside a section (charts, cards, pull quotes, annotations);
* ``visual_validation`` checks every figure of the structured components against the authorised evidence.

Pure functions, no LLM, no DB. Messages feed the single repair retry.
"""
from __future__ import annotations

from .composed_report import ComposedReportInput
from .composition_validation import MAX_ERRORS, _describe, _outside, _provenance
from .final_report_v13 import FinalReportV13
from .visual_validation import markup_errors, validate_item_visuals

# structure item type -> FinalReport 1.3 item type (identity for the rest)
STRUCTURE_TO_ITEM_13 = {"executive_summary": "hero", "kpi_grid": "metric_strip", "attention_points": "attention_grid"}


def expected_type(structure_type: str) -> str:
    return STRUCTURE_TO_ITEM_13.get(structure_type, structure_type)


def validate_report_against_composition_v13(report: FinalReportV13, composed: ComposedReportInput) -> list[str]:
    errors: list[str] = markup_errors(report.model_dump(mode="json"))
    expected, actual = composed.items, report.items

    if len(actual) != len(expected):
        errors.append(f"items: se esperaban exactamente {len(expected)} items en este orden: "
                      + ", ".join(_describe_13(item) for item in expected) + f"; hay {len(actual)}.")
    for index, (want, got) in enumerate(zip(expected, actual)):
        where = f"items[{index}]"
        if got.type != expected_type(want.type):
            errors.append(f"{where}: se esperaba type {expected_type(want.type)!r} ({_describe_13(want)}) "
                          f"y hay {got.type!r}.")
            continue
        ok_keys = composed.item_ok_keys(index)
        if want.type == "section":
            if got.key != want.key:
                errors.append(f"{where}.key: debe ser {want.key!r}.")
            if got.title != want.title:
                errors.append(f"{where}.title: debe ser {want.title!r}.")
            expected_status = "ok" if ok_keys else "unavailable"
            if got.status != expected_status:
                errors.append(f"{where}.status: debe ser {expected_status!r}.")
            allowed = ok_keys if got.status == "ok" else composed.item_unavailable_keys(index)
            bad = _outside(got.source_section_keys, allowed)
            if bad:
                errors.append(f"{where}.source_section_keys: no autorizadas {bad}; válidas: {sorted(allowed)}.")
        elif want.type == "notes":
            if got.text != want.text:
                errors.append(f"{where}.text: debe ser exactamente el texto solicitado.")
        elif want.type == "kpi_grid":
            if len(got.metrics) > want.max_kpis:
                errors.append(f"{where}.metrics: máximo {want.max_kpis}.")
        elif want.type == "attention_points":
            _provenance(errors, where, [k for p in got.points for k in p.source_section_keys], ok_keys)
        elif want.type == "methodology_notes":
            allowed = set(composed.item_keys(index))
            bad = _outside([k for n in got.notes for k in n.source_section_keys], allowed)
            if bad:
                errors.append(f"{where}.notes.source_section_keys: no autorizadas {bad}; válidas: {sorted(allowed)}.")
        errors.extend(validate_item_visuals(got, index, composed))
    return errors[:MAX_ERRORS]


def _describe_13(item) -> str:
    return _describe(item) if item.type == "section" else expected_type(item.type)
