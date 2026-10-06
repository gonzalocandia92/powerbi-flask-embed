"""Deterministic post-writer validation of a ``FinalReportV131`` against its ``ComposedReportInput`` (v2).

Same contract as ``composition_validation_v13`` (the composed input decides WHAT items exist, in WHAT order and with
WHICH evidence), with the 1.3.1 typed-reference checks of ``visual_validation_v131``. ``check_markup=False`` is used only
for the deterministic fallback, whose text is the analytics answers verbatim (the renderer escapes everything).
"""
from __future__ import annotations

from .composed_report import ComposedReportInput
from .composition_validation import MAX_ERRORS, _outside, _provenance
from .composition_validation_v13 import _describe_13, expected_type
from .final_report_v131 import FinalReportV131
from .visual_validation import markup_errors
from .visual_validation_v131 import validate_item_visuals_v131


def validate_report_against_composition_v131(report: FinalReportV131, composed: ComposedReportInput, *,
                                             check_markup: bool = True) -> list[str]:
    errors: list[str] = markup_errors(report.model_dump(mode="json")) if check_markup else []
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
        errors.extend(validate_item_visuals_v131(got, index, composed))
    return errors[:MAX_ERRORS]
