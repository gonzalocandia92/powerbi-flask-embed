"""Deterministic pieces around the structure spec: domain validation, default spec, input hash.

Pure functions, no database and no LLM. The planner is never trusted: whatever it returns
goes through ``validate_spec`` before it can be persisted.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from pydantic import ValidationError

from .artifacts import canonical_json, sha256_hex
from .structure_contracts import (
    WARNING_QUESTIONS_NOT_PLACED, StructureQuestion, StructureValidationError, StructureWarning,
)
from .structure_spec import (
    CURRENT_STRUCTURE_SCHEMA, DEFAULT_STRUCTURE_SCHEMA, ReportStructureSpec, UnknownStructureSchemaError,
    _StructureSpecBase, structure_schemas,
)

MAX_VALIDATION_ERRORS = 20
MAX_ERROR_CHARS = 300
_SLUG_RE = re.compile(r"[^a-z0-9_.-]+")


def _clip(value: str) -> str:
    return value if len(value) <= MAX_ERROR_CHARS else value[:MAX_ERROR_CHARS] + "…"


def format_validation_errors(exc: ValidationError) -> list[str]:
    errors = []
    for item in exc.errors(include_input=False, include_url=False)[:MAX_VALIDATION_ERRORS]:
        path = ".".join(str(part) for part in item.get("loc", ())) or "(raíz)"
        errors.append(_clip(f"{path}: {item.get('msg')}"))
    return errors


def validate_spec(raw: Mapping[str, Any] | ReportStructureSpec, valid_question_keys: Iterable[str],
                  *, schema_version: str | None = None) -> ReportStructureSpec:
    """Schema + domain validation. Raises ``StructureValidationError`` (with ``errors``) on any violation.

    Domain rules (beyond what the Pydantic model enforces): every ``source_question_key`` must be a
    key of THIS definition. Resolved through the registry, so a persisted spec of an older version
    is validated by its own model.
    """
    try:
        if isinstance(raw, _StructureSpecBase):
            raw = raw.to_json()
        spec = structure_schemas.validate(raw, schema_version)
    except UnknownStructureSchemaError as exc:
        raise StructureValidationError(str(exc), code="structure_unknown_schema_version",
                                       errors=[str(exc)]) from exc
    except ValidationError as exc:
        errors = format_validation_errors(exc)
        raise StructureValidationError("La estructura no cumple el schema.", errors=errors) from exc
    valid = set(valid_question_keys)
    unknown = sorted(spec.referenced_question_keys() - valid)
    if unknown:
        raise StructureValidationError(
            "La estructura referencia preguntas inexistentes.", code="structure_unknown_question_key",
            errors=[_clip(f"source_question_keys: key inexistente {key!r}. Keys válidas: "
                          + ", ".join(sorted(valid))) for key in unknown[:MAX_VALIDATION_ERRORS]])
    return spec


def _section_key(question_key: str, used: set[str]) -> str:
    base = _SLUG_RE.sub("_", question_key.lower()).strip("_.-") or "seccion"
    key, n = base[:80], 2
    while key in used:
        suffix = f"_{n}"
        key, n = base[:80 - len(suffix)] + suffix, n + 1
    used.add(key)
    return key


def default_structure_spec(questions: Iterable[StructureQuestion]) -> ReportStructureSpec:
    """The standard composition, as an authoritative structure (``ReportStructureSpec`` 1.1).

    executive summary -> KPI grid (pool = every question; the writer picks the cards, as the legacy writer
    did) -> one section per question, in order -> attention points -> methodology notes. This is what the
    legacy report did implicitly, spelled out so that "item absent = component absent" does not regress it.
    Defined in code (never by an LLM) and a pure function of the questions, so it is reproducible.
    """
    ordered = sorted(questions, key=lambda q: (q.position, q.key))
    used: set[str] = set()
    items: list[dict[str, Any]] = [{"type": "executive_summary"}]
    if ordered:
        items.append({"type": "kpi_grid", "source_question_keys": [q.key for q in ordered]})
    for question in ordered:
        items.append({"type": "section", "key": _section_key(question.key, used),
                      "title": question.title.strip()[:200] or question.key,
                      "source_question_keys": [question.key]})
    items += [{"type": "attention_points"}, {"type": "methodology_notes"}]
    return structure_schemas.get(DEFAULT_STRUCTURE_SCHEMA).model_validate(
        {"schema_version": DEFAULT_STRUCTURE_SCHEMA, "items": items})


def placement_warnings(spec: ReportStructureSpec, questions: Iterable[StructureQuestion]) -> list[StructureWarning]:
    """Info-level note when the user's structure leaves some questions out of every item."""
    placed = spec.referenced_question_keys()
    missing = [q for q in sorted(questions, key=lambda q: (q.position, q.key)) if q.key not in placed]
    if not missing:
        return []
    names = ", ".join(q.title.strip() or q.key for q in missing[:10])
    more = f" y {len(missing) - 10} más" if len(missing) > 10 else ""
    return [StructureWarning(
        code=WARNING_QUESTIONS_NOT_PLACED, severity="info",
        message=f"Estas preguntas no quedaron ubicadas en la estructura: {names}{more}.")]


def structure_input_hash(structure_prompt: str | None, questions: Iterable[StructureQuestion],
                         schema_version: str = CURRENT_STRUCTURE_SCHEMA) -> str:
    """Identity of everything a compiled spec depends on.

    The prompt, the version it was compiled for and, per question, key/title/text/position. If any
    of those changes the stored spec is stale. The planner prompt version and model are deliberately
    NOT part of it: tuning them must not make every saved spec stale (and re-bill on the next run).
    """
    payload = {
        "schema_version": schema_version,
        "structure_prompt": (structure_prompt or "").strip(),
        "questions": [
            {"key": q.key, "title": q.title.strip(), "question": q.question.strip(), "position": q.position}
            for q in sorted(questions, key=lambda q: (q.position, q.key))
        ],
    }
    return sha256_hex(canonical_json(payload))
