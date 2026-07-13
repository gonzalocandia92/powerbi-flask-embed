"""Administration routes for AI limits, prompts, skills and model pricing."""
import csv
import json
import re
from collections import defaultdict
from datetime import datetime, time
from io import StringIO

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy.orm import joinedload
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import db
from app.forms import AgentPromptConfigForm, AIModelPricingForm, AnalyticsSkillForm, BillingLimitForm
from app.models import AgentPromptConfig, AIModelPricing, AnalyticsSkill, BillingLimit, Empresa, Report
from app.services.skill_vector_service import trigger_all_skill_reindex_update, trigger_skill_embedding_update
from app.utils.decorators import retry_on_db_error


bp = Blueprint('ai_config', __name__, url_prefix='/admin/ai-config')
SKILL_EXPORT_SCOPES = {"all", "global", "empresa", "report", "dataset"}
SKILL_EXPORT_COLUMNS = [
    "id",
    "skill_key",
    "domain_key",
    "title",
    "description",
    "priority",
    "enforcement_mode",
    "confidence_label",
    "scope",
    "empresa_id",
    "empresa_nombre",
    "report_id",
    "report_name",
    "dataset_id",
    "routing_text",
    "content",
    "metadata_canonical_measures",
    "metadata_required_schema_items",
    "metadata_preferred_tables",
    "metadata_allowed_dimensions",
    "metadata_constraints",
    "metadata_extra_json",
    "routing_trigger_terms",
    "routing_example_questions",
    "routing_intents",
    "routing_negative_triggers",
    "routing_required_companion_skill_keys",
    "routing_extra_json",
    "validation_common_failure_modes",
    "validation_validation_notes",
    "validation_extra_json",
    "is_active",
    "version",
    "created_at",
    "updated_at",
]
SKILL_IMPORT_SCOPES = {"global", "empresa", "report", "dataset"}
SKILL_IMPORT_PRIORITIES = {"low", "normal", "high"}
SKILL_IMPORT_ENFORCEMENT_MODES = {"soft", "hard_candidate", "hard"}
SKILL_IMPORT_CONFIDENCE_LABELS = {"", "draft", "reviewed", "confirmed"}
SKILL_IMPORT_MODES = {"full", "patch"}
SKILL_IMPORT_SIGNING_SALT = "analytics-skill-import-v1"
SKILL_IMPORT_TOKEN_MAX_AGE_SECONDS = 60 * 60
SKILL_IMPORT_TECHNICAL_COLUMNS = {
    "version",
    "created_at",
    "updated_at",
    "embedding",
    "embedding_model",
    "embedded_at",
    "routing_document_hash",
}
SKILL_PATCH_SIMPLE_FIELDS = {
    "skill_key",
    "domain_key",
    "title",
    "description",
    "priority",
    "enforcement_mode",
    "confidence_label",
    "routing_text",
    "content",
    "is_active",
}
SKILL_PATCH_REQUIRED_FIELDS = {"skill_key", "domain_key", "title", "routing_text", "content"}
SKILL_PATCH_CLEARABLE_COLUMNS = {
    "description",
    "confidence_label",
    "metadata_canonical_measures",
    "metadata_required_schema_items",
    "metadata_preferred_tables",
    "metadata_allowed_dimensions",
    "metadata_constraints",
    "metadata_extra_json",
    "routing_trigger_terms",
    "routing_example_questions",
    "routing_intents",
    "routing_negative_triggers",
    "routing_required_companion_skill_keys",
    "routing_extra_json",
    "validation_common_failure_modes",
    "validation_validation_notes",
    "validation_extra_json",
}


def _date_start(value):
    return datetime.combine(value, time.min) if value else None


def _date_end(value):
    return datetime.combine(value, time.max) if value else None


def _as_float(value):
    return float(value) if value is not None else 0.0


def _active_limits_by_scope():
    limits = (
        BillingLimit.query
        .filter(BillingLimit.period_type == 'monthly_anniversary')
        .order_by(BillingLimit.id.desc())
        .all()
    )
    result = {}
    for limit_item in limits:
        key = (limit_item.scope_type, limit_item.scope_id)
        if key not in result:
            result[key] = limit_item
    return result


def _active_prompts_by_scope():
    prompts = AgentPromptConfig.query.order_by(AgentPromptConfig.id.desc()).all()
    result = {}
    for prompt in prompts:
        key = (prompt.scope_type, prompt.scope_id)
        if key not in result:
            result[key] = prompt
    return result


def _build_skill_groups(skills):
    grouped_reports = defaultdict(list)
    grouped_companies = defaultdict(list)
    grouped_datasets = defaultdict(list)
    global_skills = []

    for skill in skills:
        if skill.scope == 'report':
            grouped_reports[skill.report_id_fk].append(skill)
        elif skill.scope == 'empresa':
            grouped_companies[skill.empresa_id_fk].append(skill)
        elif skill.scope == 'dataset':
            grouped_datasets[skill.dataset_id or ''].append(skill)
        else:
            global_skills.append(skill)

    def skill_key(skill):
        return (
            0 if skill.is_active else 1,
            (skill.domain_key or '').lower(),
            (skill.skill_key or '').lower(),
            -(skill.version or 0),
        )

    def report_label(report_id, items):
        report = items[0].report if items and items[0].report else None
        return report.name if report else f"Reporte #{report_id or 'sin asignar'}"

    def company_label(empresa_id, items):
        empresa = items[0].empresa if items and items[0].empresa else None
        return empresa.nombre if empresa else f"Empresa #{empresa_id or 'sin asignar'}"

    report_groups = [
        {
            'id': report_id,
            'label': report_label(report_id, items),
            'skills': sorted(items, key=skill_key),
        }
        for report_id, items in grouped_reports.items()
    ]
    company_groups = [
        {
            'id': empresa_id,
            'label': company_label(empresa_id, items),
            'skills': sorted(items, key=skill_key),
        }
        for empresa_id, items in grouped_companies.items()
    ]
    dataset_groups = [
        {
            'id': dataset_id,
            'label': dataset_id or 'Dataset sin asignar',
            'skills': sorted(items, key=skill_key),
        }
        for dataset_id, items in grouped_datasets.items()
    ]

    return {
        'reports': sorted(report_groups, key=lambda item: item['label'].lower()),
        'companies': sorted(company_groups, key=lambda item: item['label'].lower()),
        'global': sorted(global_skills, key=skill_key),
        'datasets': sorted(dataset_groups, key=lambda item: item['label'].lower()),
    }


def _build_skill_index_summary(skills):
    active_skills = [skill for skill in skills if skill.is_active]
    indexed = [
        skill
        for skill in active_skills
        if skill.embedding is not None and skill.routing_document_hash
    ]
    return {
        'active_total': len(active_skills),
        'active_indexed': len(indexed),
        'active_pending': len(active_skills) - len(indexed),
    }


def _skill_export_query(scope, scope_id):
    query = AnalyticsSkill.query.options(
        joinedload(AnalyticsSkill.report),
        joinedload(AnalyticsSkill.empresa),
    )
    if scope == "global":
        query = query.filter(
            AnalyticsSkill.report_id_fk.is_(None),
            AnalyticsSkill.empresa_id_fk.is_(None),
            AnalyticsSkill.dataset_id.is_(None),
        )
    elif scope == "empresa":
        if scope_id:
            query = query.filter(AnalyticsSkill.empresa_id_fk == int(scope_id))
        else:
            query = query.filter(AnalyticsSkill.empresa_id_fk.isnot(None))
    elif scope == "report":
        if scope_id:
            query = query.filter(AnalyticsSkill.report_id_fk == int(scope_id))
        else:
            query = query.filter(AnalyticsSkill.report_id_fk.isnot(None))
    elif scope == "dataset":
        if scope_id:
            query = query.filter(AnalyticsSkill.dataset_id == str(scope_id))
        else:
            query = query.filter(AnalyticsSkill.dataset_id.isnot(None))
    return query.order_by(
        AnalyticsSkill.report_id_fk,
        AnalyticsSkill.empresa_id_fk,
        AnalyticsSkill.dataset_id,
        AnalyticsSkill.domain_key,
        AnalyticsSkill.skill_key,
        AnalyticsSkill.version.desc(),
        AnalyticsSkill.id,
    )


def _csv_json_cell(value):
    if value in (None, "", [], {}):
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _csv_list_cell(payload, key):
    value = payload.get(key)
    if not isinstance(value, list):
        return ""
    return "\n".join(str(item).strip() for item in value if str(item or "").strip())


def _json_extra(payload, known_keys):
    extra = {
        key: value
        for key, value in payload.items()
        if key not in known_keys
    }
    return _csv_json_cell(extra)


def _skill_export_row(skill):
    metadata = _json_object(skill.metadata_json)
    routing = _json_object(skill.routing_json)
    validation = _json_object(skill.validation_json)
    return {
        "id": skill.id,
        "skill_key": skill.skill_key,
        "domain_key": skill.domain_key,
        "title": skill.title,
        "description": skill.description or "",
        "priority": skill.priority or "",
        "enforcement_mode": skill.enforcement_mode or "",
        "confidence_label": skill.confidence_label or "",
        "scope": skill.scope,
        "empresa_id": skill.empresa_id_fk or "",
        "empresa_nombre": skill.empresa.nombre if skill.empresa else "",
        "report_id": skill.report_id_fk or "",
        "report_name": skill.report.name if skill.report else "",
        "dataset_id": skill.dataset_id or "",
        "routing_text": skill.routing_text or "",
        "content": skill.content or "",
        "metadata_canonical_measures": _csv_list_cell(metadata, "canonical_measures"),
        "metadata_required_schema_items": _csv_json_cell(metadata.get("required_schema_items")),
        "metadata_preferred_tables": _csv_list_cell(metadata, "preferred_tables"),
        "metadata_allowed_dimensions": _csv_list_cell(metadata, "allowed_dimensions"),
        "metadata_constraints": _csv_list_cell(metadata, "constraints"),
        "metadata_extra_json": _json_extra(metadata, METADATA_JSON_KEYS | DEPRECATED_METADATA_JSON_KEYS),
        "routing_trigger_terms": _csv_list_cell(routing, "trigger_terms"),
        "routing_example_questions": _csv_list_cell(routing, "example_questions"),
        "routing_intents": _csv_list_cell(routing, "intents"),
        "routing_negative_triggers": _csv_list_cell(routing, "negative_triggers"),
        "routing_required_companion_skill_keys": _csv_list_cell(routing, "required_companion_skill_keys"),
        "routing_extra_json": _json_extra(routing, ROUTING_JSON_KEYS),
        "validation_common_failure_modes": _csv_json_cell(validation.get("common_failure_modes")),
        "validation_validation_notes": _csv_list_cell(validation, "validation_notes"),
        "validation_extra_json": _json_extra(validation, VALIDATION_JSON_KEYS),
        "is_active": "true" if skill.is_active else "false",
        "version": skill.version or "",
        "created_at": skill.created_at.isoformat() if skill.created_at else "",
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else "",
    }


def _skills_to_csv(skills):
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SKILL_EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for skill in skills:
        writer.writerow(_skill_export_row(skill))
    return buffer.getvalue()


def _skill_export_filename(scope, scope_id):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = scope
    if scope_id:
        suffix = f"{scope}_{scope_id}"
    return f"analytics_skills_{suffix}_{timestamp}.csv"


def _skill_import_serializer():
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"],
        salt=SKILL_IMPORT_SIGNING_SALT,
    )


def _dump_skill_import_payload(preview, mode="full"):
    payload = {
        "mode": mode,
        "rows": [
            row["data"]
            for row in preview["rows"]
            if not row["errors"] and row["data"] is not None
        ],
    }
    return _skill_import_serializer().dumps(payload)


def _load_skill_import_payload(token):
    return _skill_import_serializer().loads(
        token,
        max_age=SKILL_IMPORT_TOKEN_MAX_AGE_SECONDS,
    )


def _skill_import_summary(rows):
    summary = {"create": 0, "update": 0, "patch": 0, "unchanged": 0, "error": 0, "total": len(rows)}
    for row in rows:
        if row["errors"]:
            summary["error"] += 1
        else:
            summary[row["action"]] += 1
    return summary


def _skill_import_row(row_number, data=None, *, action="error", errors=None):
    errors = errors or []
    return {
        "row_number": row_number,
        "action": "error" if errors else action,
        "data": data,
        "scope": (data or {}).get("scope", ""),
        "skill_key": (data or {}).get("skill_key", ""),
        "title": (data or {}).get("title", ""),
        "errors": errors,
    }


def _csv_cell(row, key):
    return str(row.get(key) or "").strip()


def _parse_import_int(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _parse_import_bool(value, *, default=True):
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "t", "yes", "y", "si", "s"}:
        return True
    if text in {"0", "false", "f", "no", "n"}:
        return False
    return None


def _parse_import_json(value, field_name, errors, *, expected_type):
    text = str(value or "").strip()
    if not text:
        return [] if expected_type is list else {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        errors.append(f"{field_name}: JSON invalido.")
        return [] if expected_type is list else {}
    if not isinstance(payload, expected_type):
        expected_label = "array" if expected_type is list else "objeto"
        errors.append(f"{field_name}: debe ser un {expected_label} JSON.")
        return [] if expected_type is list else {}
    return payload


def _parse_import_required_schema_items(value, errors):
    raw_items = _parse_import_json(
        value,
        "metadata_required_schema_items",
        errors,
        expected_type=list,
    )
    result = []
    seen = set()
    for item in raw_items:
        if not isinstance(item, dict):
            errors.append("metadata_required_schema_items: cada item debe ser un objeto.")
            continue
        item_type = str(item.get("item_type") or "").strip().lower()
        item_name = " ".join(str(item.get("item_name") or "").strip().split())
        if item_type not in {"measure", "table"}:
            errors.append("metadata_required_schema_items: item_type debe ser measure o table.")
            continue
        if not item_name:
            errors.append("metadata_required_schema_items: item_name es obligatorio.")
            continue
        key = (item_type, item_name.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append({"item_type": item_type, "item_name": item_name})
    return result


def _parse_import_common_failure_modes(value, errors):
    raw_items = _parse_import_json(
        value,
        "validation_common_failure_modes",
        errors,
        expected_type=list,
    )
    result = []
    seen = set()
    for item in raw_items:
        if not isinstance(item, dict):
            errors.append("validation_common_failure_modes: cada item debe ser un objeto.")
            continue
        issue = " ".join(str(item.get("issue") or "").strip().split())
        prevention = " ".join(str(item.get("prevention") or "").strip().split())
        if not issue and not prevention:
            continue
        key = (issue.casefold(), prevention.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append({"issue": issue, "prevention": prevention})
    return result


def _split_import_clear_fields(value):
    return [
        item.strip()
        for item in re.split(r"[,;\n]+", str(value or ""))
        if item.strip()
    ]


def _patch_header_names(fieldnames):
    return {str(field or "").strip() for field in fieldnames or [] if str(field or "").strip()}


def _patch_has_value(row, field_name):
    return field_name in row and _csv_cell(row, field_name) != ""


def _parse_patch_list(row, field_name):
    return _unique_nonempty_strings(_csv_cell(row, field_name).splitlines())


def _parse_patch_extra_json(row, field_name, errors):
    return _parse_import_json(_csv_cell(row, field_name), field_name, errors, expected_type=dict)


def _apply_patch_scope_changes(row, changes, errors):
    if not _patch_has_value(row, "scope"):
        return
    scope = _csv_cell(row, "scope").lower()
    if scope not in SKILL_IMPORT_SCOPES:
        errors.append("scope invalido.")
        return
    has_empresa = bool(_csv_cell(row, "empresa_id") or _csv_cell(row, "empresa_nombre"))
    has_report = bool(_csv_cell(row, "report_id") or _csv_cell(row, "report_name"))
    has_dataset = bool(_csv_cell(row, "dataset_id"))
    changes["scope"] = scope
    changes["empresa_id_fk"] = None
    changes["report_id_fk"] = None
    changes["dataset_id"] = None
    if scope == "global" and (has_empresa or has_report or has_dataset):
        errors.append("Scope global no acepta empresa, reporte ni dataset.")
        return
    if scope == "empresa":
        if has_report or has_dataset:
            errors.append("Scope empresa no acepta reporte ni dataset.")
        changes["empresa_id_fk"] = _resolve_import_empresa(row, errors)
    elif scope == "report":
        if has_empresa or has_dataset:
            errors.append("Scope report no acepta empresa ni dataset.")
        changes["report_id_fk"] = _resolve_import_report(row, errors)
    elif scope == "dataset":
        if has_empresa or has_report:
            errors.append("Scope dataset no acepta empresa ni reporte.")
        dataset_id = _csv_cell(row, "dataset_id")
        if not dataset_id:
            errors.append("Scope dataset exige dataset_id.")
        changes["dataset_id"] = dataset_id


def _patch_json_changes(row, errors):
    metadata_changes = {}
    routing_changes = {}
    validation_changes = {}

    for csv_name, json_key in (
        ("metadata_canonical_measures", "canonical_measures"),
        ("metadata_preferred_tables", "preferred_tables"),
        ("metadata_allowed_dimensions", "allowed_dimensions"),
        ("metadata_constraints", "constraints"),
    ):
        if _patch_has_value(row, csv_name):
            metadata_changes[json_key] = _parse_patch_list(row, csv_name)
    if _patch_has_value(row, "metadata_required_schema_items"):
        metadata_changes["required_schema_items"] = _parse_import_required_schema_items(
            _csv_cell(row, "metadata_required_schema_items"),
            errors,
        )
    if _patch_has_value(row, "metadata_extra_json"):
        extra = _parse_patch_extra_json(row, "metadata_extra_json", errors)
        for key in DEPRECATED_METADATA_JSON_KEYS:
            extra.pop(key, None)
        metadata_changes.update(extra)

    for csv_name, json_key in (
        ("routing_trigger_terms", "trigger_terms"),
        ("routing_example_questions", "example_questions"),
        ("routing_intents", "intents"),
        ("routing_negative_triggers", "negative_triggers"),
        ("routing_required_companion_skill_keys", "required_companion_skill_keys"),
    ):
        if _patch_has_value(row, csv_name):
            routing_changes[json_key] = _parse_patch_list(row, csv_name)
    if _patch_has_value(row, "routing_extra_json"):
        routing_changes.update(_parse_patch_extra_json(row, "routing_extra_json", errors))

    if _patch_has_value(row, "validation_common_failure_modes"):
        validation_changes["common_failure_modes"] = _parse_import_common_failure_modes(
            _csv_cell(row, "validation_common_failure_modes"),
            errors,
        )
    if _patch_has_value(row, "validation_validation_notes"):
        validation_changes["validation_notes"] = _parse_patch_list(row, "validation_validation_notes")
    if _patch_has_value(row, "validation_extra_json"):
        validation_changes.update(_parse_patch_extra_json(row, "validation_extra_json", errors))

    return metadata_changes, routing_changes, validation_changes


def _clear_patch_field(changes, field_name, errors):
    if field_name in SKILL_PATCH_REQUIRED_FIELDS:
        errors.append(f"clear_fields no puede borrar {field_name}.")
        return
    if field_name not in SKILL_PATCH_CLEARABLE_COLUMNS:
        errors.append(f"clear_fields tiene un campo desconocido: {field_name}.")
        return
    if field_name in {"description", "confidence_label"}:
        changes[field_name] = None
        return
    if field_name == "metadata_extra_json":
        changes.setdefault("metadata_remove_extra", True)
        return
    if field_name == "routing_extra_json":
        changes.setdefault("routing_remove_extra", True)
        return
    if field_name == "validation_extra_json":
        changes.setdefault("validation_remove_extra", True)
        return

    metadata_map = {
        "metadata_canonical_measures": "canonical_measures",
        "metadata_required_schema_items": "required_schema_items",
        "metadata_preferred_tables": "preferred_tables",
        "metadata_allowed_dimensions": "allowed_dimensions",
        "metadata_constraints": "constraints",
    }
    routing_map = {
        "routing_trigger_terms": "trigger_terms",
        "routing_example_questions": "example_questions",
        "routing_intents": "intents",
        "routing_negative_triggers": "negative_triggers",
        "routing_required_companion_skill_keys": "required_companion_skill_keys",
    }
    validation_map = {
        "validation_common_failure_modes": "common_failure_modes",
        "validation_validation_notes": "validation_notes",
    }
    if field_name in metadata_map:
        changes.setdefault("metadata_json", {})[metadata_map[field_name]] = []
    elif field_name in routing_map:
        changes.setdefault("routing_json", {})[routing_map[field_name]] = []
    elif field_name in validation_map:
        changes.setdefault("validation_json", {})[validation_map[field_name]] = []


def _patch_changed_field_names(changes):
    names = []
    for field_name in (
        "skill_key",
        "domain_key",
        "title",
        "description",
        "priority",
        "enforcement_mode",
        "confidence_label",
        "routing_text",
        "content",
        "is_active",
    ):
        if field_name in changes:
            names.append(field_name)
    if "scope" in changes:
        names.append("scope")

    metadata_reverse = {
        "canonical_measures": "metadata_canonical_measures",
        "required_schema_items": "metadata_required_schema_items",
        "preferred_tables": "metadata_preferred_tables",
        "allowed_dimensions": "metadata_allowed_dimensions",
        "constraints": "metadata_constraints",
    }
    routing_reverse = {
        "trigger_terms": "routing_trigger_terms",
        "example_questions": "routing_example_questions",
        "intents": "routing_intents",
        "negative_triggers": "routing_negative_triggers",
        "required_companion_skill_keys": "routing_required_companion_skill_keys",
    }
    validation_reverse = {
        "common_failure_modes": "validation_common_failure_modes",
        "validation_notes": "validation_validation_notes",
    }
    for key in changes.get("metadata_json", {}):
        names.append(metadata_reverse.get(key, f"metadata_extra_json.{key}"))
    for key in changes.get("routing_json", {}):
        names.append(routing_reverse.get(key, f"routing_extra_json.{key}"))
    for key in changes.get("validation_json", {}):
        names.append(validation_reverse.get(key, f"validation_extra_json.{key}"))
    if changes.get("metadata_remove_extra"):
        names.append("metadata_extra_json")
    if changes.get("routing_remove_extra"):
        names.append("routing_extra_json")
    if changes.get("validation_remove_extra"):
        names.append("validation_extra_json")
    return sorted(dict.fromkeys(names))


def _parse_patch_skill_import_row(row, row_number, fieldnames):
    errors = []
    fieldnames = _patch_header_names(fieldnames)
    import_id = _parse_import_int(_csv_cell(row, "id"))
    if "id" not in fieldnames or import_id is None:
        errors.append("Patch exige id valido.")
        return None, errors

    skill = AnalyticsSkill.query.get(import_id)
    if not skill:
        errors.append("No existe una skill con ese id.")
        return None, errors

    changes = {}
    for field_name in SKILL_PATCH_SIMPLE_FIELDS:
        if not _patch_has_value(row, field_name):
            continue
        value = _csv_cell(row, field_name)
        if field_name == "priority":
            value = value.lower()
            if value not in SKILL_IMPORT_PRIORITIES:
                errors.append("priority invalida.")
                continue
        elif field_name == "enforcement_mode":
            value = value.lower()
            if value not in SKILL_IMPORT_ENFORCEMENT_MODES:
                errors.append("enforcement_mode invalido.")
                continue
        elif field_name == "confidence_label":
            value = value.lower()
            if value not in SKILL_IMPORT_CONFIDENCE_LABELS:
                errors.append("confidence_label invalida.")
                continue
            value = value or None
        elif field_name == "is_active":
            value = _parse_import_bool(value, default=skill.is_active)
            if value is None:
                errors.append("is_active debe ser true/false.")
                continue
        changes[field_name] = value

    _apply_patch_scope_changes(row, changes, errors)
    metadata_changes, routing_changes, validation_changes = _patch_json_changes(row, errors)
    if metadata_changes:
        changes["metadata_json"] = metadata_changes
    if routing_changes:
        changes["routing_json"] = routing_changes
    if validation_changes:
        changes["validation_json"] = validation_changes

    for field_name in _split_import_clear_fields(_csv_cell(row, "clear_fields")):
        _clear_patch_field(changes, field_name, errors)

    data = {
        "source_row": row_number,
        "mode": "patch",
        "import_id": import_id,
        "target_id": skill.id,
        "skill_key": skill.skill_key,
        "scope": skill.scope,
        "title": skill.title,
        "changes": changes,
        "changed_fields": _patch_changed_field_names(changes),
    }
    if not changes and not errors:
        data["action"] = "unchanged"
        return data, errors

    preview_skill = AnalyticsSkill()
    for key, value in _skill_import_snapshot(skill).items():
        setattr(preview_skill, key, value)
    _apply_skill_patch_payload(preview_skill, data, update_version=False)
    data["action"] = "unchanged" if _skill_import_snapshot(skill) == _skill_import_snapshot(preview_skill) else "patch"
    return data, errors


def _resolve_import_empresa(row, errors):
    empresa_id = _parse_import_int(_csv_cell(row, "empresa_id"))
    if empresa_id is not None:
        empresa = Empresa.query.get(empresa_id)
        if empresa:
            return empresa.id
    empresa_nombre = _csv_cell(row, "empresa_nombre")
    if empresa_nombre:
        matches = Empresa.query.filter(Empresa.nombre == empresa_nombre).all()
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            errors.append("empresa_nombre coincide con mas de una empresa.")
            return None
    errors.append("Scope empresa exige empresa_id valido o empresa_nombre exacto.")
    return None


def _resolve_import_report(row, errors):
    report_id = _parse_import_int(_csv_cell(row, "report_id"))
    if report_id is not None:
        report = Report.query.get(report_id)
        if report:
            return report.id
    report_name = _csv_cell(row, "report_name")
    if report_name:
        matches = Report.query.filter(Report.name == report_name).all()
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            errors.append("report_name coincide con mas de un reporte.")
            return None
    errors.append("Scope report exige report_id valido o report_name exacto.")
    return None


def _scope_specific_values(data):
    return {
        "empresa": data.get("empresa_id_fk"),
        "report": data.get("report_id_fk"),
        "dataset": data.get("dataset_id"),
        "global": None,
    }


def _skill_import_natural_key(data):
    return (
        (data.get("skill_key") or "").casefold(),
        data.get("scope"),
        _scope_specific_values(data).get(data.get("scope")),
    )


def _skill_import_query_for_natural_key(data):
    query = AnalyticsSkill.query.filter(AnalyticsSkill.skill_key == data["skill_key"])
    scope = data["scope"]
    if scope == "global":
        query = query.filter(
            AnalyticsSkill.report_id_fk.is_(None),
            AnalyticsSkill.empresa_id_fk.is_(None),
            AnalyticsSkill.dataset_id.is_(None),
        )
    elif scope == "empresa":
        query = query.filter(AnalyticsSkill.empresa_id_fk == data["empresa_id_fk"])
    elif scope == "report":
        query = query.filter(AnalyticsSkill.report_id_fk == data["report_id_fk"])
    elif scope == "dataset":
        query = query.filter(AnalyticsSkill.dataset_id == data["dataset_id"])
    return query.order_by(AnalyticsSkill.version.desc(), AnalyticsSkill.id.desc())


def _find_skill_for_import(data):
    import_id = data.get("import_id")
    if import_id:
        skill = AnalyticsSkill.query.get(import_id)
        if skill:
            return skill
    return _skill_import_query_for_natural_key(data).first()


def _build_import_json_payloads(row, errors):
    metadata_extra = _parse_import_json(_csv_cell(row, "metadata_extra_json"), "metadata_extra_json", errors, expected_type=dict)
    routing_extra = _parse_import_json(_csv_cell(row, "routing_extra_json"), "routing_extra_json", errors, expected_type=dict)
    validation_extra = _parse_import_json(_csv_cell(row, "validation_extra_json"), "validation_extra_json", errors, expected_type=dict)
    for key in DEPRECATED_METADATA_JSON_KEYS:
        metadata_extra.pop(key, None)

    metadata_json = {
        **metadata_extra,
        "canonical_measures": _unique_nonempty_strings(_csv_cell(row, "metadata_canonical_measures").splitlines()),
        "required_schema_items": _parse_import_required_schema_items(_csv_cell(row, "metadata_required_schema_items"), errors),
        "preferred_tables": _unique_nonempty_strings(_csv_cell(row, "metadata_preferred_tables").splitlines()),
        "allowed_dimensions": _unique_nonempty_strings(_csv_cell(row, "metadata_allowed_dimensions").splitlines()),
        "constraints": _unique_nonempty_strings(_csv_cell(row, "metadata_constraints").splitlines()),
    }
    routing_json = {
        **routing_extra,
        "trigger_terms": _unique_nonempty_strings(_csv_cell(row, "routing_trigger_terms").splitlines()),
        "example_questions": _unique_nonempty_strings(_csv_cell(row, "routing_example_questions").splitlines()),
        "intents": _unique_nonempty_strings(_csv_cell(row, "routing_intents").splitlines()),
        "negative_triggers": _unique_nonempty_strings(_csv_cell(row, "routing_negative_triggers").splitlines()),
        "required_companion_skill_keys": _unique_nonempty_strings(_csv_cell(row, "routing_required_companion_skill_keys").splitlines()),
    }
    validation_json = {
        **validation_extra,
        "common_failure_modes": _parse_import_common_failure_modes(_csv_cell(row, "validation_common_failure_modes"), errors),
        "validation_notes": _unique_nonempty_strings(_csv_cell(row, "validation_validation_notes").splitlines()),
    }
    return metadata_json, routing_json, validation_json


def _parse_skill_import_row(row, row_number):
    errors = []
    scope = (_csv_cell(row, "scope") or "global").lower()
    if scope not in SKILL_IMPORT_SCOPES:
        errors.append("scope invalido.")

    skill_key = _csv_cell(row, "skill_key")
    domain_key = _csv_cell(row, "domain_key")
    title = _csv_cell(row, "title")
    routing_text = _csv_cell(row, "routing_text")
    content = _csv_cell(row, "content")
    for field_name, value in (
        ("skill_key", skill_key),
        ("domain_key", domain_key),
        ("title", title),
        ("routing_text", routing_text),
        ("content", content),
    ):
        if not value:
            errors.append(f"{field_name} es obligatorio.")

    priority = (_csv_cell(row, "priority") or "normal").lower()
    enforcement_mode = (_csv_cell(row, "enforcement_mode") or "soft").lower()
    confidence_label = _csv_cell(row, "confidence_label").lower()
    if priority not in SKILL_IMPORT_PRIORITIES:
        errors.append("priority invalida.")
    if enforcement_mode not in SKILL_IMPORT_ENFORCEMENT_MODES:
        errors.append("enforcement_mode invalido.")
    if confidence_label not in SKILL_IMPORT_CONFIDENCE_LABELS:
        errors.append("confidence_label invalida.")

    is_active = _parse_import_bool(_csv_cell(row, "is_active"), default=True)
    if is_active is None:
        errors.append("is_active debe ser true/false.")
        is_active = True

    empresa_id_fk = None
    report_id_fk = None
    dataset_id = None
    has_empresa = bool(_csv_cell(row, "empresa_id") or _csv_cell(row, "empresa_nombre"))
    has_report = bool(_csv_cell(row, "report_id") or _csv_cell(row, "report_name"))
    has_dataset = bool(_csv_cell(row, "dataset_id"))
    if scope == "global" and (has_empresa or has_report or has_dataset):
        errors.append("Scope global no acepta empresa, reporte ni dataset.")
    elif scope == "empresa":
        if has_report or has_dataset:
            errors.append("Scope empresa no acepta reporte ni dataset.")
        empresa_id_fk = _resolve_import_empresa(row, errors)
    elif scope == "report":
        if has_empresa or has_dataset:
            errors.append("Scope report no acepta empresa ni dataset.")
        report_id_fk = _resolve_import_report(row, errors)
    elif scope == "dataset":
        if has_empresa or has_report:
            errors.append("Scope dataset no acepta empresa ni reporte.")
        dataset_id = _csv_cell(row, "dataset_id")
        if not dataset_id:
            errors.append("Scope dataset exige dataset_id.")

    metadata_json, routing_json, validation_json = _build_import_json_payloads(row, errors)
    data = {
        "source_row": row_number,
        "import_id": _parse_import_int(_csv_cell(row, "id")),
        "target_id": None,
        "skill_key": skill_key,
        "domain_key": domain_key,
        "title": title,
        "description": _csv_cell(row, "description") or None,
        "priority": priority,
        "enforcement_mode": enforcement_mode,
        "confidence_label": confidence_label or None,
        "scope": scope,
        "empresa_id_fk": empresa_id_fk,
        "report_id_fk": report_id_fk,
        "dataset_id": dataset_id,
        "routing_text": routing_text,
        "content": content,
        "metadata_json": metadata_json,
        "routing_json": routing_json,
        "validation_json": validation_json,
        "is_active": bool(is_active),
    }
    return data, errors


def _skill_import_snapshot(skill):
    return {
        "skill_key": skill.skill_key,
        "domain_key": skill.domain_key,
        "title": skill.title,
        "description": skill.description,
        "priority": skill.priority,
        "enforcement_mode": skill.enforcement_mode,
        "confidence_label": skill.confidence_label,
        "empresa_id_fk": skill.empresa_id_fk,
        "report_id_fk": skill.report_id_fk,
        "dataset_id": skill.dataset_id,
        "routing_text": skill.routing_text,
        "content": skill.content,
        "metadata_json": _json_object(skill.metadata_json),
        "routing_json": _json_object(skill.routing_json),
        "validation_json": _json_object(skill.validation_json),
        "is_active": bool(skill.is_active),
    }


def _skill_import_payload_snapshot(data):
    return {
        "skill_key": data["skill_key"],
        "domain_key": data["domain_key"],
        "title": data["title"],
        "description": data["description"],
        "priority": data["priority"],
        "enforcement_mode": data["enforcement_mode"],
        "confidence_label": data["confidence_label"],
        "empresa_id_fk": data["empresa_id_fk"],
        "report_id_fk": data["report_id_fk"],
        "dataset_id": data["dataset_id"],
        "routing_text": data["routing_text"],
        "content": data["content"],
        "metadata_json": data["metadata_json"],
        "routing_json": data["routing_json"],
        "validation_json": data["validation_json"],
        "is_active": bool(data["is_active"]),
    }


def _skill_import_payload_equals(skill, data):
    return _skill_import_snapshot(skill) == _skill_import_payload_snapshot(data)


def _strip_json_extra(payload, known_keys):
    return {
        key: value
        for key, value in _json_object(payload).items()
        if key in known_keys
    }


def _patch_json_object(existing, changes, *, remove_extra=False, known_keys=None):
    result = _strip_json_extra(existing, known_keys or set()) if remove_extra else _json_object(existing)
    result.update(changes or {})
    return result


def _apply_skill_patch_payload(skill, data, *, update_version=True):
    before = _skill_import_snapshot(skill)
    changes = data.get("changes") or {}

    for field_name in (
        "skill_key",
        "domain_key",
        "title",
        "description",
        "priority",
        "enforcement_mode",
        "confidence_label",
        "routing_text",
        "content",
        "is_active",
    ):
        if field_name in changes:
            setattr(skill, field_name, changes[field_name])

    if "scope" in changes:
        skill.report_id_fk = None
        skill.empresa_id_fk = None
        skill.dataset_id = None
        if changes["scope"] == "empresa":
            skill.empresa_id_fk = changes.get("empresa_id_fk")
        elif changes["scope"] == "report":
            skill.report_id_fk = changes.get("report_id_fk")
        elif changes["scope"] == "dataset":
            skill.dataset_id = changes.get("dataset_id")

    if changes.get("metadata_json") or changes.get("metadata_remove_extra"):
        skill.metadata_json = _patch_json_object(
            skill.metadata_json,
            changes.get("metadata_json"),
            remove_extra=bool(changes.get("metadata_remove_extra")),
            known_keys=METADATA_JSON_KEYS,
        )
    if changes.get("routing_json") or changes.get("routing_remove_extra"):
        skill.routing_json = _patch_json_object(
            skill.routing_json,
            changes.get("routing_json"),
            remove_extra=bool(changes.get("routing_remove_extra")),
            known_keys=ROUTING_JSON_KEYS,
        )
    if changes.get("validation_json") or changes.get("validation_remove_extra"):
        skill.validation_json = _patch_json_object(
            skill.validation_json,
            changes.get("validation_json"),
            remove_extra=bool(changes.get("validation_remove_extra")),
            known_keys=VALIDATION_JSON_KEYS,
        )

    changed = before != _skill_import_snapshot(skill)
    if changed and update_version:
        skill.version = int(skill.version or 1) + 1
        skill.embedding = None
        skill.embedding_model = None
        skill.embedded_at = None
        skill.routing_document_hash = None
    return changed


def _preview_skill_import_rows(raw_rows):
    rows = []
    for index, raw_row in enumerate(raw_rows, start=2):
        data, errors = _parse_skill_import_row(raw_row, index)
        if errors:
            rows.append(_skill_import_row(index, data, errors=errors))
            continue
        existing = _find_skill_for_import(data)
        data["target_id"] = existing.id if existing else None
        action = "create"
        if existing:
            action = "unchanged" if _skill_import_payload_equals(existing, data) else "update"
        data["action"] = action
        rows.append(_skill_import_row(index, data, action=action))

    seen = {}
    for row in rows:
        if row["errors"] or row["data"] is None:
            continue
        key = _skill_import_natural_key(row["data"])
        if key in seen:
            row["errors"].append(f"Duplicada en el CSV con la fila {seen[key]}.")
            row["action"] = "error"
            row["data"]["action"] = "error"
        else:
            seen[key] = row["row_number"]

    return {"rows": rows, "summary": _skill_import_summary(rows)}


def _preview_patch_skill_import_rows(raw_rows, fieldnames):
    rows = []
    seen_ids = {}
    for index, raw_row in enumerate(raw_rows, start=2):
        data, errors = _parse_patch_skill_import_row(raw_row, index, fieldnames)
        if data and data.get("import_id") in seen_ids:
            errors.append(f"Duplicada en el CSV con la fila {seen_ids[data['import_id']]}.")
        elif data:
            seen_ids[data["import_id"]] = index

        if errors:
            rows.append(_skill_import_row(index, data, errors=errors))
            continue
        rows.append(_skill_import_row(index, data, action=data["action"]))
    return {"rows": rows, "summary": _skill_import_summary(rows)}


def _preview_skill_import_csv(csv_text, mode="full"):
    try:
        reader = csv.DictReader(StringIO(csv_text.lstrip("\ufeff")))
        if not reader.fieldnames:
            return {"rows": [], "summary": _skill_import_summary([]), "errors": ["El CSV no tiene encabezados."]}
        raw_rows = list(reader)
        if not raw_rows:
            return {"rows": [], "summary": _skill_import_summary([]), "errors": ["El CSV no tiene filas para importar."]}
        if mode == "patch":
            preview = _preview_patch_skill_import_rows(raw_rows, reader.fieldnames)
        elif mode == "full":
            preview = _preview_skill_import_rows(raw_rows)
        else:
            return {"rows": [], "summary": _skill_import_summary([]), "errors": ["Modo de importacion invalido."]}
        preview["errors"] = []
        return preview
    except csv.Error:
        return {"rows": [], "summary": _skill_import_summary([]), "errors": ["No se pudo leer el CSV."]}


def _validate_confirm_import_row(data):
    errors = []
    if data.get("mode") == "patch":
        if data.get("action") in {"patch", "unchanged"} and not AnalyticsSkill.query.get(data["target_id"]):
            errors.append("La skill a actualizar ya no existe.")
        changes = data.get("changes") or {}
        if changes.get("scope") == "empresa" and not Empresa.query.get(changes.get("empresa_id_fk")):
            errors.append("La empresa referenciada ya no existe.")
        if changes.get("scope") == "report" and not Report.query.get(changes.get("report_id_fk")):
            errors.append("El reporte referenciado ya no existe.")
        return errors
    if data["scope"] == "empresa" and not Empresa.query.get(data["empresa_id_fk"]):
        errors.append("La empresa referenciada ya no existe.")
    if data["scope"] == "report" and not Report.query.get(data["report_id_fk"]):
        errors.append("El reporte referenciado ya no existe.")
    if data["action"] in {"update", "unchanged"} and not AnalyticsSkill.query.get(data["target_id"]):
        errors.append("La skill a actualizar ya no existe.")
    if data["action"] == "create" and _find_skill_for_import(data):
        errors.append("Ya existe una skill con la misma clave y scope.")
    return errors


def _apply_skill_import_payload(skill, data, *, is_new):
    before = None if is_new else _skill_import_snapshot(skill)
    skill.skill_key = data["skill_key"]
    skill.domain_key = data["domain_key"]
    skill.title = data["title"]
    skill.description = data["description"]
    skill.priority = data["priority"]
    skill.enforcement_mode = data["enforcement_mode"]
    skill.confidence_label = data["confidence_label"]
    skill.report_id_fk = data["report_id_fk"]
    skill.empresa_id_fk = data["empresa_id_fk"]
    skill.dataset_id = data["dataset_id"]
    skill.routing_text = data["routing_text"]
    skill.content = data["content"]
    skill.metadata_json = data["metadata_json"]
    skill.routing_json = data["routing_json"]
    skill.validation_json = data["validation_json"]
    skill.is_active = bool(data["is_active"])

    if is_new:
        skill.version = 1
        return True

    changed = before != _skill_import_snapshot(skill)
    if changed:
        skill.version = int(skill.version or 1) + 1
        skill.embedding = None
        skill.embedding_model = None
        skill.embedded_at = None
        skill.routing_document_hash = None
    return changed


def _confirm_skill_import_rows(rows):
    preview_rows = []
    changed_skill_ids = []
    summary = {"create": 0, "update": 0, "patch": 0, "unchanged": 0, "error": 0, "total": len(rows)}

    for data in rows:
        errors = _validate_confirm_import_row(data)
        if errors:
            preview_rows.append(_skill_import_row(data.get("source_row"), data, errors=errors))
            summary["error"] += 1
            continue

        is_new = data["action"] == "create"
        skill = AnalyticsSkill(version=1) if is_new else AnalyticsSkill.query.get(data["target_id"])
        changed = False
        if data["action"] != "unchanged":
            if data.get("mode") == "patch":
                changed = _apply_skill_patch_payload(skill, data)
            else:
                changed = _apply_skill_import_payload(skill, data, is_new=is_new)
            if is_new:
                db.session.add(skill)
                db.session.flush()
        if changed and skill.is_active:
            changed_skill_ids.append(skill.id)

        final_action = data["action"] if changed or is_new else "unchanged"
        data["target_id"] = skill.id
        preview_rows.append(_skill_import_row(data.get("source_row"), data, action=final_action))
        summary[final_action] += 1

    if summary["error"]:
        db.session.rollback()
        return {
            "ok": False,
            "preview": {"rows": preview_rows, "summary": summary, "errors": []},
            "changed_skill_ids": [],
        }

    db.session.commit()
    return {
        "ok": True,
        "preview": {"rows": preview_rows, "summary": summary, "errors": []},
        "changed_skill_ids": changed_skill_ids,
    }


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def index():
    """AI configuration overview."""
    active_tab = request.args.get('tab', 'limits')
    if active_tab not in {'limits', 'pricing', 'prompts', 'skills'}:
        active_tab = 'limits'

    limits_by_scope = _active_limits_by_scope()
    prompts_by_scope = _active_prompts_by_scope()
    global_limit = limits_by_scope.get(('global', None))
    global_prompt = prompts_by_scope.get(('global', None))
    companies = Empresa.query.order_by(Empresa.nombre).all()
    reports = Report.query.order_by(Report.name).all()
    company_limits = [
        {
            'company': company,
            'limit': limits_by_scope.get(('empresa', str(company.id))),
        }
        for company in companies
    ]
    company_prompts = [
        {
            'company': company,
            'prompt': prompts_by_scope.get(('empresa', str(company.id))),
        }
        for company in companies
    ]
    report_prompts = [
        {
            'report': report,
            'prompt': prompts_by_scope.get(('report', str(report.id))),
        }
        for report in reports
    ]
    pricings = (
        AIModelPricing.query
        .order_by(
            AIModelPricing.is_active.desc(),
            AIModelPricing.provider,
            AIModelPricing.model,
            AIModelPricing.effective_from.desc(),
        )
        .all()
    )
    skills = (
        AnalyticsSkill.query
        .options(
            joinedload(AnalyticsSkill.report),
            joinedload(AnalyticsSkill.empresa),
        )
        .order_by(
            AnalyticsSkill.is_active.desc(),
            AnalyticsSkill.domain_key,
            AnalyticsSkill.skill_key,
            AnalyticsSkill.version.desc(),
        )
        .all()
    )
    return render_template(
        'admin/ai_config/index.html',
        active_tab=active_tab,
        global_limit=global_limit,
        global_prompt=global_prompt,
        company_limits=company_limits,
        company_prompts=company_prompts,
        report_prompts=report_prompts,
        pricings=pricings,
        skills=skills,
        skill_groups=_build_skill_groups(skills),
        skill_index_summary=_build_skill_index_summary(skills),
    )


def _populate_skill_choices(form):
    form.empresa_id.choices = [(0, "Seleccionar empresa")] + [
        (empresa.id, empresa.nombre)
        for empresa in Empresa.query.order_by(Empresa.nombre).all()
    ]
    form.report_id.choices = [(0, "Seleccionar reporte")] + [
        (report.id, report.name)
        for report in Report.query.order_by(Report.name).all()
    ]


METADATA_JSON_KEYS = {
    "canonical_measures",
    "required_schema_items",
    "preferred_tables",
    "allowed_dimensions",
    "constraints",
}
ROUTING_JSON_KEYS = {
    "trigger_terms",
    "example_questions",
    "intents",
    "negative_triggers",
    "required_companion_skill_keys",
}
VALIDATION_JSON_KEYS = {
    "common_failure_modes",
    "validation_notes",
}
DEPRECATED_METADATA_JSON_KEYS = {"enforcement_mode", "confidence"}


def _json_object(value):
    return dict(value) if isinstance(value, dict) else {}


def _add_form_error(field, message):
    field.errors = [*field.errors, message]


def _unique_nonempty_strings(values):
    seen = set()
    result = []
    for value in values:
        text = " ".join(str(value or "").strip().split())
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _lines_from_form(form, field_name):
    field = getattr(form, field_name)
    return _unique_nonempty_strings((field.data or "").splitlines())


def _lines_to_text(values):
    if not isinstance(values, list):
        return ""
    return "\n".join(_unique_nonempty_strings(values))


def _replace_known_json_fields(existing, known_keys, payload, *, drop_keys=None):
    drop_keys = set(drop_keys or ())
    result = {
        key: value
        for key, value in _json_object(existing).items()
        if key not in known_keys and key not in drop_keys
    }
    result.update(payload)
    return result


def _reset_field_list(field_list, rows, empty_row):
    while field_list.entries:
        field_list.pop_entry()
    for row in rows or [empty_row]:
        field_list.append_entry(row)


def _populate_required_schema_items(form, metadata):
    rows = []
    raw_items = metadata.get("required_schema_items")
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "item_type": str(item.get("item_type") or "").strip().lower(),
                    "item_name": str(item.get("item_name") or "").strip(),
                }
            )
    _reset_field_list(form.required_schema_items, rows, {"item_type": "", "item_name": ""})


def _populate_common_failure_modes(form, validation):
    rows = []
    raw_items = validation.get("common_failure_modes")
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "issue": str(item.get("issue") or "").strip(),
                    "prevention": str(item.get("prevention") or "").strip(),
                }
            )
    _reset_field_list(form.common_failure_modes, rows, {"issue": "", "prevention": ""})


def _required_schema_items_from_form(form):
    result = []
    seen = set()
    is_valid = True
    for entry in form.required_schema_items.entries:
        item_type = str(entry.form.item_type.data or "").strip().lower()
        item_name = " ".join(str(entry.form.item_name.data or "").strip().split())
        if not item_type and not item_name:
            continue
        if item_type not in {"measure", "table"}:
            _add_form_error(entry.form.item_type, "Selecciona medida o tabla.")
            is_valid = False
            continue
        if not item_name:
            _add_form_error(entry.form.item_name, "Ingresa el nombre.")
            is_valid = False
            continue
        key = (item_type, item_name.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append({"item_type": item_type, "item_name": item_name})
    return result if is_valid else None


def _common_failure_modes_from_form(form):
    result = []
    seen = set()
    for entry in form.common_failure_modes.entries:
        issue = " ".join(str(entry.form.issue.data or "").strip().split())
        prevention = " ".join(str(entry.form.prevention.data or "").strip().split())
        if not issue and not prevention:
            continue
        key = (issue.casefold(), prevention.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append({"issue": issue, "prevention": prevention})
    return result


def _metadata_from_form(form, existing_metadata=None):
    required_items = _required_schema_items_from_form(form)
    if required_items is None:
        return None
    payload = {
        "canonical_measures": _lines_from_form(form, "canonical_measures"),
        "required_schema_items": required_items,
        "preferred_tables": _lines_from_form(form, "preferred_tables"),
        "allowed_dimensions": _lines_from_form(form, "allowed_dimensions"),
        "constraints": _lines_from_form(form, "constraints"),
    }
    return _replace_known_json_fields(
        existing_metadata,
        METADATA_JSON_KEYS,
        payload,
        drop_keys=DEPRECATED_METADATA_JSON_KEYS,
    )


def _routing_from_form(form, existing_routing=None):
    payload = {
        "trigger_terms": _lines_from_form(form, "trigger_terms"),
        "example_questions": _lines_from_form(form, "example_questions"),
        "intents": _lines_from_form(form, "intents"),
        "negative_triggers": _lines_from_form(form, "negative_triggers"),
        "required_companion_skill_keys": _lines_from_form(form, "required_companion_skill_keys"),
    }
    return _replace_known_json_fields(existing_routing, ROUTING_JSON_KEYS, payload)


def _validation_from_form(form, existing_validation=None):
    payload = {
        "common_failure_modes": _common_failure_modes_from_form(form),
        "validation_notes": _lines_from_form(form, "validation_notes"),
    }
    return _replace_known_json_fields(existing_validation, VALIDATION_JSON_KEYS, payload)


def _populate_skill_form_from_model(form, skill):
    metadata = _json_object(skill.metadata_json)
    routing = _json_object(skill.routing_json)
    validation = _json_object(skill.validation_json)
    form.scope_type.data = skill.scope
    form.empresa_id.data = int(skill.empresa_id_fk or 0)
    form.report_id.data = int(skill.report_id_fk or 0)
    form.dataset_id.data = skill.dataset_id or ""
    form.confidence_label.data = skill.confidence_label or ""
    form.canonical_measures.data = _lines_to_text(metadata.get("canonical_measures"))
    form.preferred_tables.data = _lines_to_text(metadata.get("preferred_tables"))
    form.allowed_dimensions.data = _lines_to_text(metadata.get("allowed_dimensions"))
    form.constraints.data = _lines_to_text(metadata.get("constraints"))
    _populate_required_schema_items(form, metadata)
    form.trigger_terms.data = _lines_to_text(routing.get("trigger_terms"))
    form.example_questions.data = _lines_to_text(routing.get("example_questions"))
    form.intents.data = _lines_to_text(routing.get("intents"))
    form.negative_triggers.data = _lines_to_text(routing.get("negative_triggers"))
    form.required_companion_skill_keys.data = _lines_to_text(routing.get("required_companion_skill_keys"))
    _populate_common_failure_modes(form, validation)
    form.validation_notes.data = _lines_to_text(validation.get("validation_notes"))


def _save_skill_form(form, skill):
    scope_type = form.scope_type.data
    selected_empresa_id = int(form.empresa_id.data or 0)
    selected_report_id = int(form.report_id.data or 0)
    selected_dataset_id = (form.dataset_id.data or "").strip()
    selected_scope_fields = [
        scope_name
        for scope_name, is_selected in (
            ("empresa", bool(selected_empresa_id)),
            ("dataset", bool(selected_dataset_id)),
            ("report", bool(selected_report_id)),
        )
        if is_selected
    ]
    if scope_type == "global" and selected_scope_fields:
        _add_form_error(form.scope_type, "El scope global no acepta empresa, dataset ni reporte.")
        return False
    if scope_type != "global":
        incompatible = [scope_name for scope_name in selected_scope_fields if scope_name != scope_type]
        if incompatible:
            _add_form_error(form.scope_type, "El scope elegido no coincide con los campos especificos seleccionados.")
            return False

    metadata_json = _metadata_from_form(form, skill.metadata_json)
    if metadata_json is None:
        return False
    routing_json = _routing_from_form(form, skill.routing_json)
    validation_json = _validation_from_form(form, skill.validation_json)

    skill.skill_key = form.skill_key.data.strip()
    skill.domain_key = form.domain_key.data.strip()
    skill.title = form.title.data.strip()
    skill.description = (form.description.data or "").strip() or None
    skill.priority = form.priority.data or "normal"
    skill.enforcement_mode = form.enforcement_mode.data or "soft"
    skill.confidence_label = (form.confidence_label.data or "").strip() or None
    skill.routing_text = form.routing_text.data.strip()
    skill.content = form.content.data.strip()
    skill.is_active = bool(form.is_active.data)
    skill.metadata_json = metadata_json
    skill.routing_json = routing_json
    skill.validation_json = validation_json

    skill.report_id_fk = None
    skill.empresa_id_fk = None
    skill.dataset_id = None
    if scope_type == "empresa":
        if not selected_empresa_id:
            _add_form_error(form.empresa_id, "Selecciona una empresa.")
            return False
        skill.empresa_id_fk = selected_empresa_id
    elif scope_type == "dataset":
        if not selected_dataset_id:
            _add_form_error(form.dataset_id, "Ingresa el dataset_id.")
            return False
        skill.dataset_id = selected_dataset_id
    elif scope_type == "report":
        if not selected_report_id:
            _add_form_error(form.report_id, "Selecciona un reporte.")
            return False
        skill.report_id_fk = selected_report_id
    elif scope_type != "global":
        _add_form_error(form.scope_type, "Scope invalido.")
        return False

    if skill.id is not None:
        skill.version = int(skill.version or 1) + 1
        skill.embedding = None
        skill.embedding_model = None
        skill.embedded_at = None
        skill.routing_document_hash = None
    return True


@bp.route('/skills/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def skill_new():
    """Create a manually curated analytics skill."""
    form = AnalyticsSkillForm()
    _populate_skill_choices(form)
    if request.method == 'GET':
        form.scope_type.data = "global"
        form.priority.data = "normal"
        form.enforcement_mode.data = "soft"
        form.confidence_label.data = ""
        form.is_active.data = True
        _reset_field_list(form.required_schema_items, [], {"item_type": "", "item_name": ""})
        _reset_field_list(form.common_failure_modes, [], {"issue": "", "prevention": ""})

    if form.validate_on_submit():
        skill = AnalyticsSkill(version=1)
        if _save_skill_form(form, skill):
            db.session.add(skill)
            db.session.commit()
            trigger_skill_embedding_update(skill.id)
            flash("Skill creada. El embedding se actualizará en background.", "success")
            return redirect(url_for('ai_config.index', tab='skills'))

    return render_template('admin/ai_config/skill_form.html', form=form, title='Nueva skill')


@bp.route('/skills/<int:skill_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def skill_edit(skill_id):
    """Edit a manually curated analytics skill."""
    skill = AnalyticsSkill.query.get_or_404(skill_id)
    form = AnalyticsSkillForm(obj=skill)
    _populate_skill_choices(form)
    if request.method == 'GET':
        _populate_skill_form_from_model(form, skill)

    if form.validate_on_submit():
        if _save_skill_form(form, skill):
            db.session.commit()
            trigger_skill_embedding_update(skill.id)
            flash("Skill actualizada. El embedding se actualizará en background.", "success")
            return redirect(url_for('ai_config.index', tab='skills'))

    return render_template('admin/ai_config/skill_form.html', form=form, title=f'Editar skill: {skill.skill_key}')


@bp.route('/skills/<int:skill_id>/toggle', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def skill_toggle(skill_id):
    """Activate or deactivate an analytics skill."""
    skill = AnalyticsSkill.query.get_or_404(skill_id)
    skill.is_active = not skill.is_active
    db.session.commit()
    flash("Estado de la skill actualizado.", "success")
    return redirect(url_for('ai_config.index', tab='skills'))


@bp.route('/skills/<int:skill_id>/reindex', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def skill_reindex(skill_id):
    """Queue a background embedding refresh for one skill."""
    skill = AnalyticsSkill.query.get_or_404(skill_id)
    trigger_skill_embedding_update(skill.id)
    flash("Reindexado de skill encolado.", "success")
    return redirect(url_for('ai_config.index', tab='skills'))


@bp.route('/skills/reindex-all', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def skill_reindex_all():
    """Queue a background embedding refresh for active skills that need it."""
    if trigger_all_skill_reindex_update(force=False):
        flash("Reindexado global encolado para skills activas.", "success")
    else:
        flash("Ya hay un reindexado global en curso.", "warning")
    return redirect(url_for('ai_config.index', tab='skills'))


@bp.route('/skills/export', methods=['GET'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def skill_export():
    """Export analytics skills to CSV without embedding vectors."""
    scope = (request.args.get("scope") or "all").strip().lower()
    if scope not in SKILL_EXPORT_SCOPES:
        scope = "all"
    scope_id = (request.args.get("scope_id") or "").strip()
    if scope in {"empresa", "report"} and scope_id:
        try:
            int(scope_id)
        except ValueError:
            scope_id = ""

    skills = _skill_export_query(scope, scope_id).all()
    csv_payload = _skills_to_csv(skills)
    filename = _skill_export_filename(scope, scope_id)
    return Response(
        "\ufeff" + csv_payload,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route('/skills/import', methods=['GET'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def skill_import():
    """Render the CSV import screen for analytics skills."""
    return render_template(
        'admin/ai_config/skill_import.html',
        preview=None,
        import_token=None,
        import_mode="full",
    )


@bp.route('/skills/import/preview', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def skill_import_preview():
    """Validate an uploaded skills CSV and render a confirmable preview."""
    upload = request.files.get("csv_file")
    if not upload or not upload.filename:
        flash("Selecciona un archivo CSV para importar.", "warning")
        return redirect(url_for('ai_config.skill_import'))

    try:
        csv_text = upload.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("El CSV debe estar codificado en UTF-8.", "danger")
        return redirect(url_for('ai_config.skill_import'))

    import_mode = (request.form.get("import_mode") or "full").strip().lower()
    if import_mode not in SKILL_IMPORT_MODES:
        import_mode = "full"

    preview = _preview_skill_import_csv(csv_text, mode=import_mode)
    import_token = None
    if not preview.get("errors") and not preview["summary"]["error"]:
        import_token = _dump_skill_import_payload(preview, mode=import_mode)
    return render_template(
        'admin/ai_config/skill_import.html',
        preview=preview,
        import_token=import_token,
        import_mode=import_mode,
    )


@bp.route('/skills/import/confirm', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def skill_import_confirm():
    """Apply a previously previewed skills CSV import."""
    token = request.form.get("import_token") or ""
    try:
        payload = _load_skill_import_payload(token)
    except SignatureExpired:
        flash("La vista previa expiro. Vuelve a subir el CSV.", "warning")
        return redirect(url_for('ai_config.skill_import'))
    except BadSignature:
        flash("La confirmacion de importacion no es valida.", "danger")
        return redirect(url_for('ai_config.skill_import'))

    result = _confirm_skill_import_rows(payload.get("rows") or [])
    if not result["ok"]:
        flash("No se importo nada porque la validacion final encontro errores.", "danger")
        return render_template(
            'admin/ai_config/skill_import.html',
            preview=result["preview"],
            import_token=None,
            import_mode=payload.get("mode") or "full",
        )

    for skill_id in result["changed_skill_ids"]:
        trigger_skill_embedding_update(skill_id)
    summary = result["preview"]["summary"]
    flash(
        (
            "Importacion completada: "
            f"{summary['create']} nuevas, {summary['update']} actualizadas, "
            f"{summary['patch']} patches, "
            f"{summary['unchanged']} sin cambios."
        ),
        "success",
    )
    return redirect(url_for('ai_config.index', tab='skills'))


def _prompt_config(scope_type, scope_id):
    query = AgentPromptConfig.query.filter(AgentPromptConfig.scope_type == scope_type)
    if scope_id is None:
        query = query.filter(AgentPromptConfig.scope_id.is_(None))
    else:
        query = query.filter(AgentPromptConfig.scope_id == str(scope_id))
    return query.order_by(AgentPromptConfig.id.desc()).first()


def _populate_prompt_form_dates(form, prompt_item):
    if prompt_item:
        form.starts_at.data = prompt_item.starts_at.date() if prompt_item.starts_at else None
        form.ends_at.data = prompt_item.ends_at.date() if prompt_item.ends_at else None


def _populate_report_retrieval_form(form, report):
    form.schema_retrieval_prompt.data = report.schema_retrieval_prompt
    form.schema_table_context_limit.data = report.schema_table_context_limit
    form.schema_measure_context_limit.data = report.schema_measure_context_limit


def _save_report_retrieval_form(form, report):
    report.schema_retrieval_prompt = (form.schema_retrieval_prompt.data or '').strip() or None
    report.schema_table_context_limit = form.schema_table_context_limit.data
    report.schema_measure_context_limit = form.schema_measure_context_limit.data


def _save_prompt_form(form, prompt_item, *, scope_type, scope_id, default_title):
    if prompt_item is None:
        prompt_item = AgentPromptConfig(
            scope_type=scope_type,
            scope_id=str(scope_id) if scope_id is not None else None,
            title=default_title,
        )
        db.session.add(prompt_item)

    prompt_item.title = form.title.data.strip()
    prompt_item.instructions = form.instructions.data.strip()
    prompt_item.starts_at = _date_start(form.starts_at.data)
    prompt_item.ends_at = _date_end(form.ends_at.data)
    prompt_item.is_active = form.is_active.data
    return prompt_item


@bp.route('/prompts/global', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def global_prompt():
    """Create or edit the global agent prompt instructions."""
    prompt_item = _prompt_config('global', None)
    form = AgentPromptConfigForm(obj=prompt_item)
    if request.method == 'GET':
        if prompt_item:
            _populate_prompt_form_dates(form, prompt_item)
        else:
            form.title.data = 'Default global'
            form.is_active.data = True

    if form.validate_on_submit():
        if form.starts_at.data and form.ends_at.data and form.ends_at.data < form.starts_at.data:
            form.ends_at.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            _save_prompt_form(
                form,
                prompt_item,
                scope_type='global',
                scope_id=None,
                default_title='Default global',
            )
            db.session.commit()
            flash("Prompt global actualizado.", "success")
            return redirect(url_for('ai_config.index', tab='prompts'))

    return render_template(
        'admin/ai_config/prompt_form.html',
        form=form,
        title='Prompt global del agente',
        scope_label='Default Global',
    )


@bp.route('/prompts/company/<int:empresa_id>', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def company_prompt(empresa_id):
    """Create or edit company-specific agent prompt instructions."""
    company = Empresa.query.get_or_404(empresa_id)
    prompt_item = _prompt_config('empresa', company.id)
    form = AgentPromptConfigForm(obj=prompt_item)
    if request.method == 'GET':
        if prompt_item:
            _populate_prompt_form_dates(form, prompt_item)
        else:
            form.title.data = f'Prompt de {company.nombre}'
            form.is_active.data = True

    if form.validate_on_submit():
        if form.starts_at.data and form.ends_at.data and form.ends_at.data < form.starts_at.data:
            form.ends_at.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            _save_prompt_form(
                form,
                prompt_item,
                scope_type='empresa',
                scope_id=company.id,
                default_title=f'Prompt de {company.nombre}',
            )
            db.session.commit()
            flash(f"Prompt de {company.nombre} actualizado.", "success")
            return redirect(url_for('ai_config.index', tab='prompts'))

    return render_template(
        'admin/ai_config/prompt_form.html',
        form=form,
        title=f'Prompt de {company.nombre}',
        scope_label=company.nombre,
    )


@bp.route('/prompts/report/<int:report_id>', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def report_prompt(report_id):
    """Create or edit report-specific agent prompt instructions."""
    report = Report.query.get_or_404(report_id)
    prompt_item = _prompt_config('report', report.id)
    form = AgentPromptConfigForm(obj=prompt_item)
    if request.method == 'GET':
        if prompt_item:
            _populate_prompt_form_dates(form, prompt_item)
        else:
            form.title.data = f'Prompt de {report.name}'
            form.is_active.data = True
        _populate_report_retrieval_form(form, report)

    if form.validate_on_submit():
        if form.starts_at.data and form.ends_at.data and form.ends_at.data < form.starts_at.data:
            form.ends_at.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            _save_prompt_form(
                form,
                prompt_item,
                scope_type='report',
                scope_id=report.id,
                default_title=f'Prompt de {report.name}',
            )
            _save_report_retrieval_form(form, report)
            db.session.commit()
            flash(f"Prompt de {report.name} actualizado.", "success")
            return redirect(url_for('ai_config.index', tab='prompts'))

    return render_template(
        'admin/ai_config/prompt_form.html',
        form=form,
        title=f'Prompt de {report.name}',
        scope_label=report.name,
        retrieval_form_enabled=True,
    )


@bp.route('/limits/global', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def global_limit():
    """Create or edit the default global limit."""
    limit_item = (
        BillingLimit.query
        .filter(
            BillingLimit.scope_type == 'global',
            BillingLimit.scope_id.is_(None),
            BillingLimit.period_type == 'monthly_anniversary',
        )
        .order_by(BillingLimit.id.desc())
        .first()
    )
    form = BillingLimitForm(obj=limit_item)
    if request.method == 'GET' and limit_item:
        form.starts_at.data = limit_item.starts_at.date() if limit_item.starts_at else None
        form.ends_at.data = limit_item.ends_at.date() if limit_item.ends_at else None

    if form.validate_on_submit():
        if form.starts_at.data and form.ends_at.data and form.ends_at.data < form.starts_at.data:
            form.ends_at.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            if limit_item is None:
                limit_item = BillingLimit(
                    scope_type='global',
                    scope_id=None,
                    period_type='monthly_anniversary',
                    currency='USD',
                )
                db.session.add(limit_item)
            limit_item.limit_usd = _as_float(form.limit_usd.data)
            limit_item.cycle_anchor_day = form.cycle_anchor_day.data
            limit_item.starts_at = _date_start(form.starts_at.data)
            limit_item.ends_at = _date_end(form.ends_at.data)
            limit_item.is_active = form.is_active.data
            db.session.commit()
            flash("Default global actualizado.", "success")
            return redirect(url_for('ai_config.index', tab='limits'))

    return render_template(
        'admin/ai_config/limit_form.html',
        form=form,
        title='Editar default global',
        scope_label='Default Global',
    )


@bp.route('/limits/company/<int:empresa_id>', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def company_limit(empresa_id):
    """Create or edit the limit assigned to a company."""
    company = Empresa.query.get_or_404(empresa_id)
    limit_item = (
        BillingLimit.query
        .filter(
            BillingLimit.scope_type == 'empresa',
            BillingLimit.scope_id == str(company.id),
            BillingLimit.period_type == 'monthly_anniversary',
        )
        .order_by(BillingLimit.id.desc())
        .first()
    )
    form = BillingLimitForm(obj=limit_item)
    if request.method == 'GET':
        if limit_item:
            form.starts_at.data = limit_item.starts_at.date() if limit_item.starts_at else None
            form.ends_at.data = limit_item.ends_at.date() if limit_item.ends_at else None
        else:
            form.is_active.data = True
            form.cycle_anchor_day.data = 1

    if form.validate_on_submit():
        if form.starts_at.data and form.ends_at.data and form.ends_at.data < form.starts_at.data:
            form.ends_at.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            if limit_item is None:
                limit_item = BillingLimit(
                    scope_type='empresa',
                    scope_id=str(company.id),
                    period_type='monthly_anniversary',
                    currency='USD',
                )
                db.session.add(limit_item)
            limit_item.limit_usd = _as_float(form.limit_usd.data)
            limit_item.cycle_anchor_day = form.cycle_anchor_day.data
            limit_item.starts_at = _date_start(form.starts_at.data)
            limit_item.ends_at = _date_end(form.ends_at.data)
            limit_item.is_active = form.is_active.data
            db.session.commit()
            flash(f"Limite de {company.nombre} actualizado.", "success")
            return redirect(url_for('ai_config.index', tab='limits'))

    return render_template(
        'admin/ai_config/limit_form.html',
        form=form,
        title=f'Limite de {company.nombre}',
        scope_label=company.nombre,
    )


@bp.route('/pricing/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def pricing_new():
    """Create a model pricing record."""
    form = AIModelPricingForm()
    if request.method == 'GET':
        form.effective_from.data = datetime.utcnow().date()
        form.is_active.data = True

    if form.validate_on_submit():
        duplicate = AIModelPricing.query.filter(
            AIModelPricing.provider == form.provider.data.strip().lower(),
            AIModelPricing.model == form.model.data.strip(),
            AIModelPricing.event_type == form.event_type.data,
            AIModelPricing.is_active.is_(True),
        ).first()
        if duplicate and form.is_active.data:
            form.model.errors.append("Ya existe un pricing activo para esta combinacion.")
        elif form.effective_to.data and form.effective_to.data < form.effective_from.data:
            form.effective_to.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            pricing = AIModelPricing(
                provider=form.provider.data.strip().lower(),
                model=form.model.data.strip(),
                event_type=form.event_type.data,
                currency='USD',
                input_cost_per_million_usd=_as_float(form.input_cost_per_million_usd.data),
                output_cost_per_million_usd=_as_float(form.output_cost_per_million_usd.data),
                cache_write_cost_per_million_usd=_as_float(form.cache_write_cost_per_million_usd.data),
                cache_read_cost_per_million_usd=_as_float(form.cache_read_cost_per_million_usd.data),
                effective_from=_date_start(form.effective_from.data),
                effective_to=_date_end(form.effective_to.data),
                is_active=form.is_active.data,
            )
            db.session.add(pricing)
            db.session.commit()
            flash("Pricing creado.", "success")
            return redirect(url_for('ai_config.index', tab='pricing'))

    return render_template(
        'admin/ai_config/pricing_form.html',
        form=form,
        title='Nuevo pricing',
    )


@bp.route('/pricing/<int:pricing_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def pricing_edit(pricing_id):
    """Edit a model pricing record."""
    pricing = AIModelPricing.query.get_or_404(pricing_id)
    form = AIModelPricingForm(obj=pricing)
    if request.method == 'GET':
        form.effective_from.data = pricing.effective_from.date()
        form.effective_to.data = pricing.effective_to.date() if pricing.effective_to else None

    if form.validate_on_submit():
        duplicate = AIModelPricing.query.filter(
            AIModelPricing.id != pricing.id,
            AIModelPricing.provider == form.provider.data.strip().lower(),
            AIModelPricing.model == form.model.data.strip(),
            AIModelPricing.event_type == form.event_type.data,
            AIModelPricing.is_active.is_(True),
        ).first()
        if duplicate and form.is_active.data:
            form.model.errors.append("Ya existe otro pricing activo para esta combinacion.")
        elif form.effective_to.data and form.effective_to.data < form.effective_from.data:
            form.effective_to.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            pricing.provider = form.provider.data.strip().lower()
            pricing.model = form.model.data.strip()
            pricing.event_type = form.event_type.data
            pricing.input_cost_per_million_usd = _as_float(form.input_cost_per_million_usd.data)
            pricing.output_cost_per_million_usd = _as_float(form.output_cost_per_million_usd.data)
            pricing.cache_write_cost_per_million_usd = _as_float(form.cache_write_cost_per_million_usd.data)
            pricing.cache_read_cost_per_million_usd = _as_float(form.cache_read_cost_per_million_usd.data)
            pricing.effective_from = _date_start(form.effective_from.data)
            pricing.effective_to = _date_end(form.effective_to.data)
            pricing.is_active = form.is_active.data
            db.session.commit()
            flash("Pricing actualizado.", "success")
            return redirect(url_for('ai_config.index', tab='pricing'))

    return render_template(
        'admin/ai_config/pricing_form.html',
        form=form,
        title=f'Editar pricing: {pricing.model}',
    )


@bp.route('/pricing/<int:pricing_id>/toggle', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def pricing_toggle(pricing_id):
    """Activate or deactivate model pricing."""
    pricing = AIModelPricing.query.get_or_404(pricing_id)
    if not pricing.is_active:
        duplicate = AIModelPricing.query.filter(
            AIModelPricing.id != pricing.id,
            AIModelPricing.provider == pricing.provider,
            AIModelPricing.model == pricing.model,
            AIModelPricing.event_type == pricing.event_type,
            AIModelPricing.is_active.is_(True),
        ).first()
        if duplicate:
            flash("Ya existe otro pricing activo para esta combinacion.", "danger")
            return redirect(url_for('ai_config.index', tab='pricing'))

    pricing.is_active = not pricing.is_active
    db.session.commit()
    flash("Estado del pricing actualizado.", "success")
    return redirect(url_for('ai_config.index', tab='pricing'))
