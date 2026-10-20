"""Frontend-ready payload of "how KLARA understood the structure". Pure: no DB, no model knowledge."""
from __future__ import annotations

from typing import Any

from .structure_spec import ReportStructureSpec
from .structure_store import StructureState
from .snapshot import structure_from_snapshot

ITEM_LABELS = {
    "executive_summary": "Resumen ejecutivo",
    "kpi_grid": "Indicadores (KPIs)",
    "attention_points": "Puntos de atención",
    "notes": "Notas",
    "methodology_notes": "Notas y metodología",
}


def spec_preview(spec: ReportStructureSpec, titles: dict[str, str]) -> list[dict[str, Any]]:
    items = []
    for index, item in enumerate(spec.items):
        keys = list(getattr(item, "source_question_keys", ()))
        items.append({
            "position": index + 1, "type": item.type,
            "label": getattr(item, "title", None) or ITEM_LABELS[item.type],
            "section_key": getattr(item, "key", None),
            "text": getattr(item, "text", None),
            "questions": [{"key": key, "title": titles.get(key, key)} for key in keys],
        })
    return items


def run_structure_payload(snapshot: dict | None) -> dict[str, Any]:
    """Read only the run's frozen inputs; never reconstruct a historical spec."""
    snapshot = snapshot or {}
    block = snapshot.get("structure")
    prompt = block.get("prompt") if isinstance(block, dict) else snapshot.get("structure_prompt")
    payload = {"prompt": prompt, "source": None, "status": None, "warnings": [], "preview": None}
    try:
        frozen = structure_from_snapshot(snapshot)
    except (ValueError, KeyError, TypeError):
        # An unavailable schema must not prevent reading the report itself.
        return payload
    if frozen is not None:
        titles = {q["key"]: q["title"] for q in snapshot.get("questions") or []}
        payload.update(source=frozen.source, status=frozen.status,
                       warnings=[w.model_dump(mode="json") for w in frozen.warnings],
                       preview=spec_preview(frozen.spec, titles))
    return payload


def structure_payload(state: StructureState, *, compiled: bool | None = None) -> dict[str, Any]:
    """``spec`` is what a run created now would use; ``saved_spec`` what was last interpreted (maybe stale)."""
    titles = {q.key: q.title for q in state.questions}
    return {
        "status": state.status, "source": state.source, "stale": state.stale,
        "needs_compile": state.needs_compile, "fallback_used": state.source == "fallback",
        "has_prompt": state.prompt is not None,
        "schema_version": state.spec.schema_version,
        "input_hash": state.input_hash,
        "compiled_at": state.compiled_at.isoformat() if state.compiled_at else None,
        "compiled": compiled,
        "spec": state.spec.to_json(),
        "preview": spec_preview(state.spec, titles),
        "saved_spec": state.saved_spec.to_json() if state.saved_spec is not None and state.stale else None,
        "warnings": [warning.model_dump(mode="json") for warning in state.warnings],
        "error": state.error,
        "planner": {key: state.planner[key] for key in ("model_key", "prompt_version") if key in state.planner},
    }
