"""Administrative validation helpers for analytics skills."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from app import db
from app.models import AnalyticsSkill, SchemaEmbedding


@dataclass
class SkillSchemaValidationResult:
    skill_id: int
    valid: bool
    warnings: List[str] = field(default_factory=list)


def _normalize_name(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    if text.startswith("[") and text.endswith("]") and len(text) > 2:
        text = text[1:-1].strip()
    return text.casefold()


def _schema_names(report_id: int, item_type: str) -> set[str]:
    rows = (
        SchemaEmbedding.query
        .filter(
            SchemaEmbedding.report_id_fk == report_id,
            SchemaEmbedding.item_type == item_type,
        )
        .all()
    )
    return {_normalize_name(row.item_name) for row in rows}


def _unique_strings(values: List[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _required_companion_skill_keys(routing: Dict[str, Any], warnings: List[str]) -> List[str]:
    raw_keys = routing.get("required_companion_skill_keys")
    if raw_keys is None:
        return []
    if not isinstance(raw_keys, list):
        warnings.append("required_companion_skill_keys debe ser una lista de strings.")
        return []
    result: List[str] = []
    for raw_key in raw_keys:
        if not isinstance(raw_key, str) or not raw_key.strip():
            warnings.append("required_companion_skill_keys solo acepta strings no vacios.")
            continue
        result.append(raw_key.strip())
    return _unique_strings(result)


def _scope_filter(report_id: int, skill: AnalyticsSkill):
    filters = [
        db.and_(
            AnalyticsSkill.report_id_fk.is_(None),
            AnalyticsSkill.empresa_id_fk.is_(None),
            AnalyticsSkill.dataset_id.is_(None),
        ),
        AnalyticsSkill.report_id_fk == report_id,
    ]
    if skill.empresa_id_fk is not None:
        filters.append(AnalyticsSkill.empresa_id_fk == skill.empresa_id_fk)
    if skill.dataset_id:
        filters.append(AnalyticsSkill.dataset_id == str(skill.dataset_id))
    return db.or_(*filters)


def validate_skill_against_schema(skill: AnalyticsSkill, report_id: int) -> SkillSchemaValidationResult:
    """Validate skill metadata against a report schema snapshot without mutating state."""
    warnings: List[str] = []
    metadata = skill.metadata_json if isinstance(skill.metadata_json, dict) else None
    if metadata is None:
        warnings.append("metadata_json no es un objeto JSON valido.")
        metadata = {}
    if not str(skill.routing_text or "").strip():
        warnings.append("routing_text esta vacio.")
    if not str(skill.content or "").strip():
        warnings.append("content esta vacio.")
    if not str(skill.description or "").strip():
        warnings.append("description esta vacio.")
    routing = skill.routing_json if isinstance(skill.routing_json, dict) else {}
    if skill.routing_json is not None and not isinstance(skill.routing_json, dict):
        warnings.append("routing_json no es un objeto JSON valido.")
    if skill.validation_json is not None and not isinstance(skill.validation_json, dict):
        warnings.append("validation_json no es un objeto JSON valido.")

    companion_keys = _required_companion_skill_keys(routing, warnings)
    for companion_key in companion_keys:
        exists = (
            AnalyticsSkill.query
            .filter(
                AnalyticsSkill.id != skill.id,
                AnalyticsSkill.is_active.is_(True),
                db.func.lower(AnalyticsSkill.skill_key) == companion_key.casefold(),
                _scope_filter(report_id, skill),
            )
            .first()
            is not None
        )
        if not exists:
            warnings.append(f"Companion skill requerida no encontrada en scope compatible: {companion_key}")

    measure_names = _schema_names(report_id, "measure")
    table_names = _schema_names(report_id, "table")

    for measure in metadata.get("canonical_measures") or []:
        if _normalize_name(measure) not in measure_names:
            warnings.append(f"Medida canonica no encontrada en schema: {measure}")

    required_items = metadata.get("required_schema_items") or []
    if not isinstance(required_items, list):
        warnings.append("required_schema_items debe ser una lista.")
        required_items = []
    for item in required_items:
        if not isinstance(item, dict):
            warnings.append("required_schema_items contiene un item no objeto.")
            continue
        item_type = str(item.get("item_type") or "").strip().lower()
        item_name = str(item.get("item_name") or "").strip()
        if item_type == "measure" and _normalize_name(item_name) not in measure_names:
            warnings.append(f"Medida requerida no encontrada en schema: {item_name}")
        elif item_type == "table" and _normalize_name(item_name) not in table_names:
            warnings.append(f"Tabla requerida no encontrada en schema: {item_name}")
        elif item_type not in {"measure", "table"}:
            warnings.append(f"Tipo de item requerido invalido: {item_type}")

    duplicate_query = AnalyticsSkill.query.filter(
        AnalyticsSkill.id != skill.id,
        AnalyticsSkill.skill_key == skill.skill_key,
        AnalyticsSkill.is_active.is_(True),
        AnalyticsSkill.report_id_fk == skill.report_id_fk,
        AnalyticsSkill.empresa_id_fk == skill.empresa_id_fk,
        AnalyticsSkill.dataset_id == skill.dataset_id,
    )
    if skill.report_id_fk is None:
        duplicate_query = duplicate_query.filter(AnalyticsSkill.report_id_fk.is_(None))
    if skill.empresa_id_fk is None:
        duplicate_query = duplicate_query.filter(AnalyticsSkill.empresa_id_fk.is_(None))
    if skill.dataset_id is None:
        duplicate_query = duplicate_query.filter(AnalyticsSkill.dataset_id.is_(None))
    if duplicate_query.first() is not None:
        warnings.append("Existe otra skill activa con el mismo skill_key y scope.")

    return SkillSchemaValidationResult(
        skill_id=int(skill.id or 0),
        valid=not warnings,
        warnings=warnings,
    )
