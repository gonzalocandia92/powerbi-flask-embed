"""Deterministic post-writer validation of a ``FinalReportV12`` against its ``ComposedReportInput``.

The prompt asks the model to respect the structure; this module ENFORCES it. Pure functions, no LLM, no DB.
Messages name paths and expected values taken from the composed input (never echo free text the model wrote),
and are what the single repair retry feeds back to the model.

Checks, per item index ``i`` (the composed input is the authority):
* same number of items, same ``type`` and order (so nothing is added, removed or moved);
* sections: same ``key`` and ``title``; ``status`` is ``unavailable`` exactly when none of the section's
  evidence succeeded;
* literal ``notes`` carry exactly the requested text;
* provenance: ``source_section_keys`` ⊆ the evidence THAT item was authorised to use (successful evidence for
  analytical claims; unavailable evidence for ``unavailable`` sections; any authorised evidence for methodology
  notes), KPI count ≤ the grid's ``max_kpis``.
"""
from __future__ import annotations

from .composed_report import ComposedReportInput
from .final_report_v12 import FinalReportV12

MAX_ERRORS = 20


def _outside(keys, allowed: set[str]) -> list[str]:
    return sorted(set(keys) - allowed)


def validate_report_against_composition(report: FinalReportV12, composed: ComposedReportInput) -> list[str]:
    errors: list[str] = []
    expected, actual = composed.items, report.items

    if len(actual) != len(expected):
        errors.append(f"items: se esperaban exactamente {len(expected)} items en este orden: "
                      + ", ".join(_describe(item) for item in expected) + f"; hay {len(actual)}.")
    for index, (want, got) in enumerate(zip(expected, actual)):
        where = f"items[{index}]"
        if got.type != want.type:
            errors.append(f"{where}: se esperaba type {want.type!r} ({_describe(want)}) y hay {got.type!r}.")
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
        elif want.type == "executive_summary":
            used = list(got.headline_source_section_keys) + [k for h in got.highlights for k in h.source_section_keys]
            _provenance(errors, where, used, ok_keys)
        elif want.type == "kpi_grid":
            if len(got.kpis) > want.max_kpis:
                errors.append(f"{where}.kpis: máximo {want.max_kpis}.")
            _provenance(errors, where, [k for kpi in got.kpis for k in kpi.source_section_keys], ok_keys)
        elif want.type == "attention_points":
            _provenance(errors, where, [k for p in got.points for k in p.source_section_keys], ok_keys)
        elif want.type == "methodology_notes":
            allowed = set(composed.item_keys(index))
            bad = _outside([k for n in got.notes for k in n.source_section_keys], allowed)
            if bad:
                errors.append(f"{where}.notes.source_section_keys: no autorizadas {bad}; válidas: {sorted(allowed)}.")
    return errors[:MAX_ERRORS]


def _provenance(errors: list[str], where: str, used, ok_keys: set[str]) -> None:
    bad = _outside(used, ok_keys)
    if bad:
        errors.append(f"{where}: source_section_keys no autorizadas {bad}; "
                      f"sólo podés citar análisis exitosos de este componente: {sorted(ok_keys)}.")


def _describe(item) -> str:
    if item.type == "section":
        return f"section {item.key!r} ({item.title!r})"
    return item.type
