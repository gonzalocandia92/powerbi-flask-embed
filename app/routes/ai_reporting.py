"""Administrative, one-shot screen for analytical KLARA reports."""
from __future__ import annotations

import asyncio
import csv
import logging
from io import StringIO
from dataclasses import replace

import requests
from flask import Blueprint, Response, current_app, jsonify, render_template, request
from flask_login import login_required

from app import db
from app.models import AIModelConfig, Report
from app.services import ai_billing, model_catalog
from app.services.analytics import (
    AnalyticsBillingLimitExceededError, AnalyticsConfigurationError,
    AnalyticsModelError, AnalyticsReportNotFoundError, build_analytics_engine,
)
from app.services.llm.profiles import PROFILES
from app.services.skill_catalog import SkillScopeContext, list_effective_skills, unknown_skill_keys
from app.services.reporting import ReportDefinition, ReportGenerator, ReportQuestion, render_markdown
from app.services.reporting.usage import record_section_usage
from app.utils.decorators import admin_required
from app.utils.powerbi import get_current_dataset_id


bp = Blueprint("ai_reporting", __name__, url_prefix="/admin/ai-reporting")
MAX_QUESTIONS = 20
MAX_QUESTION_CHARS = 1000
MAX_TITLE_CHARS = 100
MAX_CSV_BYTES = 2 * 1024 * 1024
TITLE_HEADERS = ("title", "titulo", "título")
QUESTION_HEADERS = ("question", "pregunta")
PUBLIC_FAILURE_REASONS = frozenset({
    "dax_generation_failed", "dax_query_empty", "dax_execution_exception",
    "tool_round_limit", "unsupported_tool", "semantic_model_unavailable",
    "agent_execution_exception", "pinned_skill_unavailable", "pinned_skill_resolution_failed",
})
MAX_PINNED_SKILLS = 10


def _public_failure_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    if reason in PUBLIC_FAILURE_REASONS:
        return reason
    if reason.endswith("_prompt_too_long"):
        return "provider_prompt_too_long"
    return "execution_failed"


def _effective_skills_for_report(report: Report):
    """Effective skills (report > dataset > empresa > global) with the same scope
    the analytical engine uses. Returns (skills, dataset_resolved)."""
    dataset_resolved = True
    try:
        dataset_id = get_current_dataset_id(report)
    except (requests.RequestException, RuntimeError, KeyError):
        dataset_id, dataset_resolved = None, False
    try:
        empresa_id = ai_billing.resolve_report_billing_context(report).empresa_id
    except ai_billing.BillingConfigurationError:
        empresa_id = None
    skills = list_effective_skills(SkillScopeContext(
        dataset_id=dataset_id, empresa_id=empresa_id, report_id=report.id))
    return skills, dataset_resolved


def _model_tiers(record: AIModelConfig, config: dict) -> list[str]:
    model = model_catalog.to_model_config(record, config=config)
    if not model.api_key:
        return []
    profile = PROFILES.get(model.family_key)
    tiers = []
    for name, effective_tier in (("configured", model.service_tier), ("standard", None), ("flex", "flex")):
        if name != "configured" and profile is None:
            continue
        if name == "flex" and not model.capabilities.supports_flex:
            continue
        try:
            ai_billing.validate_pricing_coverage(replace(model, service_tier=effective_tier))
        except (ai_billing.BillingConfigurationError, ValueError):
            continue
        tiers.append(name)
    return tiers


def _available_models(config: dict) -> tuple[list[dict], str | None, str | None]:
    records = AIModelConfig.query.order_by(AIModelConfig.display_name.asc()).all()
    models = []
    luna_records = [item for item in records if item.physical_model == "gpt-6-luna"]
    for record in records:
        if not record.enabled:
            continue
        tiers = _model_tiers(record, config)
        if tiers:
            models.append({
                "model_key": record.model_key,
                "display_name": record.display_name,
                "provider": record.provider,
                "physical_model": record.physical_model,
                "tiers": tiers,
            })
    luna = next((item for item in models if item["physical_model"] == "gpt-6-luna"
                 and "flex" in item["tiers"]), None)
    if luna:
        return models, luna["model_key"], None
    if not luna_records:
        reason = "GPT-6 Luna no está configurado en el catálogo."
    elif not any(item.enabled for item in luna_records):
        reason = "GPT-6 Luna está deshabilitado."
    elif not any(model_catalog.to_model_config(item, config=config).api_key
                 for item in luna_records if item.enabled):
        reason = "Falta la credencial de GPT-6 Luna."
    elif not any(item.supports_flex for item in luna_records if item.enabled):
        reason = "GPT-6 Luna no tiene Flex habilitado."
    else:
        reason = "GPT-6 Luna Flex no tiene un perfil o pricing completo disponible."
    return models, None, reason


def _definition_from_payload(data: dict, models: list[dict]) -> ReportDefinition:
    if not isinstance(data, dict):
        raise ValueError("Se requiere un objeto JSON.")
    report_id = data.get("report_id")
    if isinstance(report_id, bool) or not isinstance(report_id, int) or report_id <= 0:
        raise ValueError("Seleccioná un Report válido.")
    report = db.session.get(Report, report_id)
    if report is None:
        raise AnalyticsReportNotFoundError(f"Report no encontrado: {report_id}")
    name = data.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
        raise ValueError("El nombre debe tener entre 1 y 200 caracteres.")
    model_key = data.get("analysis_model_key")
    selected = next((item for item in models if item["model_key"] == model_key), None)
    if selected is None:
        raise AnalyticsModelError("Seleccioná un modelo habilitado y listo para ejecutar.")
    tier = data.get("analysis_service_tier") or "configured"
    if not isinstance(tier, str) or tier not in selected["tiers"]:
        raise AnalyticsModelError("El tier elegido no está disponible para ese modelo.")
    rows = data.get("questions")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_QUESTIONS:
        raise ValueError(f"Ingresá entre 1 y {MAX_QUESTIONS} preguntas.")
    questions = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"La pregunta {index} no es válida.")
        question = row.get("question")
        title = row.get("title")
        if title is None or title == "":
            title = f"Pregunta {index}"
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1000:
            raise ValueError(f"La pregunta {index} debe tener entre 1 y 1000 caracteres.")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
            raise ValueError(f"El título de la pregunta {index} debe tener hasta 100 caracteres.")
        pins = row.get("required_skill_keys") or []
        if (not isinstance(pins, list) or len(pins) > MAX_PINNED_SKILLS
                or any(not isinstance(key, str) or not key.strip() or len(key) > 120 for key in pins)):
            raise ValueError(f"Las skills fijadas de la pregunta {index} no son válidas (máximo {MAX_PINNED_SKILLS}).")
        questions.append(ReportQuestion(
            key=f"section_{index:03}", title=title.strip(),
            question=question.strip(), order=index, required_skill_keys=tuple(pins),
        ))
    pinned = [key for item in questions for key in item.required_skill_keys]
    if pinned:
        effective, _ = _effective_skills_for_report(report)
        missing = unknown_skill_keys(pinned, effective)
        if missing:
            raise ValueError("Skills fijadas no disponibles para este Report: " + ", ".join(missing))
    return ReportDefinition(
        report_id=report_id, name=name.strip(), questions=questions,
        analysis_model_key=model_key,
        analysis_service_tier=None if tier == "configured" else tier,
    )


@bp.route("/ui", methods=["GET"])
@login_required
@admin_required
def page():
    models, default_model_key, luna_warning = _available_models(dict(current_app.config))
    return render_template(
        "admin/ai_reporting.html",
        reports=Report.query.order_by(Report.name.asc()).all(),
        models=models, default_model_key=default_model_key,
        luna_warning=luna_warning, max_questions=MAX_QUESTIONS,
    )


def _csv_error(message: str):
    return jsonify({"errors": [message], "warnings": [], "rows": []}), 400


@bp.route("/import-questions", methods=["POST"])
@login_required
@admin_required
def import_questions():
    """Parse a title,question CSV into rows for the form; nothing is executed or stored."""
    upload = request.files.get("csv_file")
    if not upload or not upload.filename:
        return _csv_error("Seleccioná un archivo CSV.")
    raw = upload.stream.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        return _csv_error("El CSV supera el límite de 2 MB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _csv_error("El CSV debe estar codificado en UTF-8.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;")
    except csv.Error:
        dialect = csv.excel  # e.g. a single-column file
    reader = csv.DictReader(StringIO(text), dialect=dialect)
    try:
        headers = {str(name or "").strip().lower(): name for name in (reader.fieldnames or [])}
    except csv.Error as exc:
        return _csv_error(f"CSV inválido: {exc}")
    question_header = next((headers[key] for key in QUESTION_HEADERS if key in headers), None)
    title_header = next((headers[key] for key in TITLE_HEADERS if key in headers), None)
    if question_header is None:
        return _csv_error("Falta la columna question o pregunta.")
    rows, errors, warnings, seen = [], [], [], set()
    try:
        for line_number, raw_row in enumerate(reader, start=2):
            question = str(raw_row.get(question_header) or "").strip()
            title = str(raw_row.get(title_header) or "").strip() if title_header else ""
            if not question and not title:
                continue  # fully blank line
            if not question:
                errors.append(f"Fila {line_number}: la pregunta está vacía.")
                continue
            if len(question) > MAX_QUESTION_CHARS:
                errors.append(f"Fila {line_number}: la pregunta supera {MAX_QUESTION_CHARS} caracteres.")
                continue
            if len(title) > MAX_TITLE_CHARS:
                errors.append(f"Fila {line_number}: el título supera {MAX_TITLE_CHARS} caracteres.")
                continue
            if question.casefold() in seen:
                warnings.append(f"Fila {line_number}: pregunta duplicada.")
            seen.add(question.casefold())
            rows.append({"title": title, "question": question, "line": line_number})
    except csv.Error as exc:
        return _csv_error(f"CSV inválido: {exc}")
    if not rows and not errors:
        errors.append("El archivo no contiene preguntas.")
    if len(rows) > MAX_QUESTIONS:
        errors.append(f"El archivo contiene más de {MAX_QUESTIONS} preguntas.")
    return jsonify({"rows": rows[:MAX_QUESTIONS], "errors": errors, "warnings": warnings,
                    "summary": {"questions": len(rows)}}), (400 if errors else 200)


@bp.route("/questions-template.csv", methods=["GET"])
@login_required
@admin_required
def questions_template():
    return Response(
        '\ufefftitle,question\n"Ventas","¿Cuánto se vendió durante la última semana cerrada?"\n',
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="preguntas-informe.csv"'},
    )


@bp.route("/skills", methods=["GET"])
@login_required
@admin_required
def skills():
    """Effective skills of a Report, used to pin skills to questions."""
    report = db.session.get(Report, request.args.get("report_id", type=int) or 0)
    if report is None:
        return jsonify({"error": "El Report seleccionado no existe."}), 404
    effective, dataset_resolved = _effective_skills_for_report(report)
    return jsonify({
        "dataset_resolved": dataset_resolved,
        "skills": [{
            "skill_key": skill.skill_key, "title": skill.title,
            "domain_key": skill.domain_key, "scope": skill.scope,
        } for skill in effective],
    })


@bp.route("/generate", methods=["POST"])
@login_required
@admin_required
def generate():
    config = dict(current_app.config)
    try:
        models, _, _ = _available_models(config)
        definition = _definition_from_payload(request.get_json(silent=True), models)
    except AnalyticsReportNotFoundError:
        return jsonify({"error": "El Report seleccionado no existe."}), 404
    except (AnalyticsModelError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    async def record_usage(section):
        await asyncio.to_thread(record_section_usage, definition.report_id, section)

    try:
        draft = asyncio.run(ReportGenerator(
            build_analytics_engine(config), record_usage=record_usage,
        ).generate(definition))
    except AnalyticsBillingLimitExceededError:
        return jsonify({"error": "Se alcanzó el límite de consumo de IA para este Report."}), 403
    except AnalyticsReportNotFoundError:
        return jsonify({"error": "El Report seleccionado ya no existe."}), 404
    except AnalyticsModelError:
        return jsonify({"error": "El modelo o tier elegido no está disponible para esta ejecución."}), 400
    except AnalyticsConfigurationError:
        return jsonify({"error": "La configuración o el pricing del análisis no permiten generar el informe."}), 400
    except Exception:
        logging.exception("[AIReporting] Report generation failed")
        return jsonify({"error": "No se pudo generar el informe. Revisá la configuración o intentá nuevamente."}), 500
    return jsonify({
        "report_run_id": draft.report_run_id,
        "name": draft.name,
        "markdown": render_markdown(draft),
        "sections": [{
            "key": section.key, "title": section.title,
            "answer": "" if section.had_error else section.answer,
            "had_error": section.had_error,
            "failure_reason": (_public_failure_reason(section.failure_reason) or "execution_failed")
                              if section.had_error else None,
            "recovered_error_count": len(section.recovered_errors),
            # Administrative/debug metadata only; the public Markdown never includes it.
            "semantic_notes": [] if section.had_error else list(section.semantic_notes),
            "skill_routing": dict(section.skill_routing),
        } for section in draft.sections],
    })
