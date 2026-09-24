"""Administrative, one-shot screen for analytical KLARA reports."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import login_required

from app import db
from app.models import AIModelConfig, Report
from app.services import ai_billing, model_catalog
from app.services.analytics import (
    AnalyticsBillingLimitExceededError, AnalyticsConfigurationError,
    AnalyticsModelError, AnalyticsReportNotFoundError, build_analytics_engine,
)
from app.services.llm.profiles import PROFILES
from app.services.reporting import ReportDefinition, ReportGenerator, ReportQuestion, render_markdown
from app.services.reporting.usage import record_section_usage
from app.utils.decorators import admin_required


bp = Blueprint("ai_reporting", __name__, url_prefix="/admin/ai-reporting")
MAX_QUESTIONS = 20


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
        questions.append(ReportQuestion(
            key=f"section_{index:03}", title=title.strip(),
            question=question.strip(), order=index,
        ))
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
        record_section_usage(definition.report_id, section)

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
        "name": draft.name,
        "markdown": render_markdown(draft),
        "sections": [{
            "key": section.key, "title": section.title,
            "answer": "" if section.had_error else section.answer,
            "had_error": section.had_error,
        } for section in draft.sections],
    })
