"""Administrative, one-shot screen for analytical KLARA reports."""
from __future__ import annotations

import asyncio
import csv
import logging
import re
import time
import uuid
from io import StringIO
from dataclasses import replace

from flask import Blueprint, Response, current_app, jsonify, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.models import AIModelConfig, AnalyticalReportDefinition, Report, ReportRun
from app.services import ai_billing, model_catalog
from app.services.analytics import (
    AnalyticsBillingLimitExceededError, AnalyticsConfigurationError,
    AnalyticsModelError, AnalyticsReportNotFoundError, build_analytics_engine,
    list_effective_skills_for_report,
)
from app.services.llm.profiles import PROFILES
from app.services.reporting import (
    PdfBrowserUnavailableError, PdfRenderError, ReportDefinition, ReportDraft, ReportQuestion, ReportWriterConfigurationError,
    build_pdf_renderer, render_markdown, report_pdf_filename,
)
from app.services.reporting.artifact_service import ArtifactService
from app.services.reporting.cost import ReportCostService
from app.services.reporting.payloads import (
    coordination_payload as _coordination_payload, planned_sections as _planned_sections,
    public_failure_reason as _public_failure_reason, section_payload as _section_payload,
    writer_payload as _writer_payload,
)
from app.services.reporting.analysis_execution import resolve_analysis_concurrency
from app.services.reporting.pipeline_factory import build_report_pipeline
from app.services.reporting.run_store import (
    TERMINAL_STATUSES, DefinitionNotRunnableError, ReportRunStore, allocate_question_key, is_valid_question_key,
)
from app.services.reporting.structure_compiler import StructureCompiler
from app.services.reporting.structure_factory import resolve_structure_planner
from app.services.reporting.structure_preview import structure_payload
from app.services.reporting.structure_service import StructureDefinitionNotFoundError, StructureService
from app.services.reporting.structure_store import StructureChangedDuringCompileError, StructureStore
from app.services.reporting.writer_factory import resolve_report_writer
from app.utils.decorators import admin_required


bp = Blueprint("ai_reporting", __name__, url_prefix="/admin/ai-reporting")
MAX_QUESTIONS = 20
MAX_QUESTION_CHARS = 1000
MAX_TITLE_CHARS = 100
MAX_CSV_BYTES = 2 * 1024 * 1024
TITLE_HEADERS = ("title", "titulo", "título")
QUESTION_HEADERS = ("question", "pregunta")
MAX_STRUCTURE_PROMPT_CHARS = 4000
MAX_PINNED_SKILLS = 10
MAX_PDF_PAYLOAD_BYTES = 2 * 1024 * 1024
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


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


def _definition_from_payload(data: dict, models: list[dict], *, reserved_keys=()) -> ReportDefinition:
    """Validate a JSON payload into a ``ReportDefinition``.

    A question may carry its stable ``key`` (editing an existing definition); questions
    without one get a new key that never collides with ``reserved_keys`` or each other.
    """
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
    explicit = [row["key"] for row in rows if isinstance(row, dict) and row.get("key") is not None]
    if not all(is_valid_question_key(key) for key in explicit):
        raise ValueError("Una pregunta tiene una key inválida.")
    if len(set(explicit)) != len(explicit):
        raise ValueError("Hay keys de pregunta duplicadas.")
    used_keys = set(reserved_keys) | set(explicit)
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
        key = row.get("key")
        if key is None:
            key = allocate_question_key(used_keys)
            used_keys.add(key)
        questions.append(ReportQuestion(
            key=key, title=title.strip(),
            question=question.strip(), order=index, required_skill_keys=tuple(pins),
        ))
    pinned = [key for item in questions for key in item.required_skill_keys]
    if pinned:
        missing = list_effective_skills_for_report(report).missing(pinned)
        if missing:
            raise ValueError("Skills fijadas no disponibles para este Report: " + ", ".join(missing))
    strategy = data.get("strategy") or "fixed"
    if strategy not in ("fixed", "coordinated"):
        raise ValueError("La estrategia debe ser 'fixed' o 'coordinated'.")
    structure_prompt = data.get("structure_prompt")
    if structure_prompt is not None and (
            not isinstance(structure_prompt, str) or len(structure_prompt) > MAX_STRUCTURE_PROMPT_CHARS):
        raise ValueError(f"El prompt de estructura debe ser texto de hasta {MAX_STRUCTURE_PROMPT_CHARS} caracteres.")
    return ReportDefinition(
        report_id=report_id, name=name.strip(), questions=questions,
        analysis_model_key=model_key,
        analysis_service_tier=None if tier == "configured" else tier,
        coordination_enabled=strategy == "coordinated",
        structure_prompt=(structure_prompt.strip() or None) if structure_prompt else None,
    )


def _run_endpoints() -> dict:
    """URL templates for the browser's ReportingApi (``{id}`` is filled client-side)."""
    return {
        "definitions": url_for("ai_reporting.save_definition"),
        "definition": url_for("ai_reporting.save_definition", definition_id=0)[:-1] + "{id}",
        "createRun": url_for("ai_reporting.create_report_run"),
        "getRun": url_for("ai_reporting.get_report_run", run_id="RUNID")[:-len("RUNID")] + "{id}",
        "cancelRun": url_for("ai_reporting.cancel_report_run", run_id="RUNID")[:-len("RUNID/cancel")] + "{id}/cancel",
        "structure": url_for("ai_reporting.get_definition_structure", definition_id=0)[:-len("0/structure")]
        + "{id}/structure",
        "compileStructure": url_for("ai_reporting.compile_definition_structure", definition_id=0)[
            :-len("0/compile-structure")] + "{id}/compile-structure",
    }


@bp.route("/ui", methods=["GET"])
@login_required
@admin_required
def page():
    models, default_model_key, luna_warning = _available_models(dict(current_app.config))
    # ``?run_id=`` reopens a persisted run (refresh, shared link, runs created elsewhere).
    run_id = request.args.get("run_id", "")
    return render_template(
        "admin/ai_reporting.html",
        initial_run_id=_log_run_id(run_id),
        run_endpoints=_run_endpoints(),
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
    return jsonify(list_effective_skills_for_report(report).to_payload())


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

    # Preflight the writer BEFORE spending analytical tokens: an unassigned or
    # unpriced report_writer role would otherwise waste a full analysis.
    try:
        writer = resolve_report_writer(config, definition.report_id)
    except ReportWriterConfigurationError as exc:
        return jsonify({"error": str(exc), "code": exc.code}), 400
    except ai_billing.BillingLimitExceeded:
        return jsonify({"error": "Se alcanzó el límite de consumo de IA para este Report."}), 403

    pipeline = build_report_pipeline(
        config, definition, writer=writer, analytics_engine=build_analytics_engine(config))
    # Legacy synchronous path: the caller (this route) owns the run identity and injects it.
    report_run_id = uuid.uuid4().hex
    try:
        result = asyncio.run(pipeline.run(definition, report_run_id=report_run_id))
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
    draft = result.draft
    return jsonify({
        "report_run_id": draft.report_run_id,
        "name": draft.name,
        "markdown": render_markdown(draft),
        "writer": _writer_payload(result),
        # The final report and its HTML are client-facing; everything below is admin/debug.
        "final_report": result.final_report.model_dump(mode="json") if result.final_report else None,
        "html": result.html,
        "sections": [_section_payload(section) for section in draft.sections],
        # Admin/debug only (V1.1). Absent ("fixed" strategy or no coordination_runner) is
        # normal, never surfaced client-side, and never blocks the report on failure.
        "coordination": _coordination_payload(result.coordination),
        # Admin only: cost of THIS report_run_id, aggregated from the AI usage ledger.
        "cost": _cost_payload(result),
    })


# ── Persistent definitions and background runs ───────────────────────────────
# POST /report-runs only validates, snapshots and queues (HTTP 202). The pipeline runs
# in the report worker (``services.reporting.run_worker``), never in this request.

def _definition_payload(model: AnalyticalReportDefinition, *, include_structure: bool = True) -> dict:
    payload = _definition_core_payload(model)
    if include_structure:
        # What KLARA currently understood of ``structure_prompt`` (and whether it is stale). Read-only.
        payload["structure"] = structure_payload(StructureStore().state(model))
    return payload


def _definition_core_payload(model: AnalyticalReportDefinition) -> dict:
    return {
        "id": model.id, "name": model.name, "report_id": model.report_id_fk,
        "strategy": model.strategy, "analysis_model_key": model.analysis_model_key,
        "analysis_service_tier": model.analysis_service_tier or "configured",
        "structure_prompt": model.structure_prompt, "is_active": model.is_active,
        "created_at": model.created_at.isoformat() if model.created_at else None,
        "updated_at": model.updated_at.isoformat() if model.updated_at else None,
        "questions": [{
            "key": q.key, "title": q.title, "question": q.question, "order": q.position,
            "required_skill_keys": list(q.required_skill_keys_json or []), "is_active": q.is_active,
        } for q in model.questions],
    }


def _payload_from_definition(model: AnalyticalReportDefinition) -> dict:
    """Stored definition -> the payload shape ``_definition_from_payload`` validates."""
    return {
        "report_id": model.report_id_fk, "name": model.name, "strategy": model.strategy,
        "analysis_model_key": model.analysis_model_key,
        "analysis_service_tier": model.analysis_service_tier or "configured",
        "structure_prompt": model.structure_prompt,
        "questions": [{
            "key": q.key, "title": q.title, "question": q.question,
            "required_skill_keys": list(q.required_skill_keys_json or []),
        } for q in sorted((q for q in model.questions if q.is_active), key=lambda q: q.position)],
    }


def _validation_error(exc: Exception):
    if isinstance(exc, AnalyticsReportNotFoundError):
        return jsonify({"error": "El Report seleccionado no existe."}), 404
    return jsonify({"error": str(exc)}), 400


@bp.route("/definitions", methods=["GET"])
@login_required
@admin_required
def list_definitions():
    query = AnalyticalReportDefinition.query.order_by(AnalyticalReportDefinition.updated_at.desc())
    if request.args.get("report_id", type=int):
        query = query.filter_by(report_id_fk=request.args.get("report_id", type=int))
    return jsonify({"definitions": [_definition_payload(item, include_structure=False)
                                     for item in query.limit(200).all()]})


@bp.route("/definitions/<int:definition_id>", methods=["GET"])
@login_required
@admin_required
def get_definition(definition_id: int):
    model = db.session.get(AnalyticalReportDefinition, definition_id)
    if model is None:
        return jsonify({"error": "La definición no existe."}), 404
    return jsonify(_definition_payload(model))


@bp.route("/definitions", methods=["POST"])
@bp.route("/definitions/<int:definition_id>", methods=["PUT"])
@login_required
@admin_required
def save_definition(definition_id: int | None = None):
    """Create or edit a definition. Questions keep their ``key`` across edits and reorders."""
    store = ReportRunStore()
    data = request.get_json(silent=True)
    if definition_id is not None and db.session.get(AnalyticalReportDefinition, definition_id) is None:
        return jsonify({"error": "La definición no existe."}), 404
    try:
        models, _, _ = _available_models(dict(current_app.config))
        definition = _definition_from_payload(
            data, models, reserved_keys=store.all_question_keys(definition_id) if definition_id else ())
    except (AnalyticsReportNotFoundError, AnalyticsModelError, ValueError) as exc:
        return _validation_error(exc)
    model = store.save_definition(
        definition, definition_id=definition_id,
        created_by_user_id=getattr(current_user, "id", None) if definition_id is None else None)
    if definition_id is not None and isinstance(data.get("is_active"), bool):
        model.is_active = data["is_active"]
        db.session.commit()
    return jsonify(_definition_payload(model)), (200 if definition_id else 201)


def _structure_service() -> StructureService:
    config = dict(current_app.config)
    return StructureService(lambda report_id: StructureCompiler(lambda: resolve_structure_planner(config, report_id)))


@bp.route("/definitions/<int:definition_id>/structure", methods=["GET"])
@login_required
@admin_required
def get_definition_structure(definition_id: int):
    """How KLARA currently understands the definition's ``structure_prompt``. Never calls an LLM."""
    try:
        state = _structure_service().get_state(definition_id)
    except StructureDefinitionNotFoundError:
        return jsonify({"error": "La definición no existe."}), 404
    response = jsonify(structure_payload(state))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/definitions/<int:definition_id>/compile-structure", methods=["POST"])
@login_required
@admin_required
def compile_definition_structure(definition_id: int):
    """Explicit (and possibly paid) interpretation of the saved ``structure_prompt``.

    Idempotent: an up-to-date interpretation is returned as-is without calling the model, unless
    ``{"force": true}``. A planner failure is NOT an HTTP error: the response carries
    ``status="fallback"`` with the default structure, the error and a ``planner_failed`` warning.
    """
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("force", False), bool):
        return jsonify({"error": "El cuerpo debe ser un objeto JSON; force debe ser booleano."}), 400
    try:
        outcome = _structure_service().compile_definition(definition_id, force=data.get("force", False))
    except StructureDefinitionNotFoundError:
        return jsonify({"error": "La definición no existe."}), 404
    except StructureChangedDuringCompileError:
        return jsonify({"error": "El prompt o las preguntas cambiaron mientras se interpretaba. Volvé a compilar.",
                        "code": "structure_input_changed_during_compile"}), 409
    payload = structure_payload(outcome.state, compiled=outcome.compiled)
    payload["reused"] = outcome.reused
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/report-runs", methods=["POST"])
@login_required
@admin_required
def create_report_run():
    """Queue a run. Body: ``{"definition_id": N}`` or an inline definition (saved, then queued)."""
    config = dict(current_app.config)
    store = ReportRunStore()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Se requiere un objeto JSON."}), 400
    try:
        models, _, _ = _available_models(config)
        if data.get("definition_id") is not None:
            model = db.session.get(AnalyticalReportDefinition, data["definition_id"]) \
                if isinstance(data["definition_id"], int) and not isinstance(data["definition_id"], bool) else None
            if model is None or not model.is_active:
                return jsonify({"error": "La definición no existe o está inactiva."}), 404
            definition = _definition_from_payload(
                _payload_from_definition(model), models, reserved_keys=store.all_question_keys(model.id))
        else:
            definition = _definition_from_payload(data, models)
            model = None
    except (AnalyticsReportNotFoundError, AnalyticsModelError, ValueError) as exc:
        return _validation_error(exc)

    # Same early preflight as /generate: do not queue a run that cannot be written.
    try:
        resolve_report_writer(config, definition.report_id)
    except ReportWriterConfigurationError as exc:
        return jsonify({"error": str(exc), "code": exc.code}), 400
    except ai_billing.BillingLimitExceeded:
        return jsonify({"error": "Se alcanzó el límite de consumo de IA para este Report."}), 403

    if model is None:
        model = store.save_definition(definition, created_by_user_id=getattr(current_user, "id", None))
    try:
        run = store.enqueue_run(model.id, requested_by_user_id=getattr(current_user, "id", None))
    except (DefinitionNotRunnableError, ValueError) as exc:
        return _validation_error(exc)
    status_url = url_for("ai_reporting.get_report_run", run_id=run.id)
    response = jsonify({"run_id": run.id, "status": run.status, "status_url": status_url,
                        "definition_id": model.id})
    response.status_code = 202
    response.headers["Location"] = status_url
    return response


def _analysis_concurrency(run: ReportRun) -> int:
    recorded = ((run.result_json or {}).get("analysis") or {}).get("configured_concurrency")
    if isinstance(recorded, int) and recorded >= 1:
        return recorded
    return resolve_analysis_concurrency(current_app.config, strict=False)


def _run_payload(run: ReportRun) -> dict:
    store = ReportRunStore()
    sections = [ReportRunStore.section_to_domain(row, run.id) for row in run.sections]
    payload = {
        "run_id": run.id, "definition_id": run.definition_id, "report_id": run.report_id_fk,
        "name": (run.definition_snapshot_json or {}).get("name"),
        "strategy": (run.definition_snapshot_json or {}).get("strategy"),
        # What the run will execute (from its snapshot); the UI uses it to show pending questions.
        "planned_sections": _planned_sections(run.definition_snapshot_json),
        "status": run.status, "current_stage": run.current_stage,
        # current = analyses FINISHED (several may be running at once).
        "progress": {"current": run.progress_current, "total": run.progress_total},
        # How many questions the worker runs at once (server policy): lets the UI mark that many as running.
        "analysis_concurrency": _analysis_concurrency(run),
        "error": {"code": run.error_code, "message": run.error_message} if run.error_code or run.error_message else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else None,
        "sections": [_section_payload(section) for section in sections],
        "result": None, "cost": None,
    }
    if run.status in TERMINAL_STATUSES:
        result = dict(run.result_json or {})
        # Artifacts are the source of truth (stored FinalReport + stored HTML, resolved with the
        # versions they were produced with); runs from before artifacts fall back to result_json.
        # Reading never renders a historical run again and never writes.
        artifacts = ArtifactService()
        final_report = artifacts.get_final_report(run.id)
        html = artifacts.get_html(run.id)
        result["final_report"] = final_report.to_json() if final_report else None
        result["html"] = html.html if html else None
        result["artifacts"] = {
            "final_report": None if final_report is None else {
                "schema_version": final_report.schema_version, "origin": final_report.origin},
            "html": None if html is None else {
                "schema_version": html.schema_version, "renderer_version": html.renderer_version,
                "origin": html.origin},
        }
        result["markdown"] = render_markdown(ReportDraft(
            report_run_id=run.id, report_id=run.report_id_fk, name=payload["name"] or "Informe",
            analysis_model_key=None, analysis_service_tier=None, sections=sections))
        payload["result"] = result
        payload["cost"] = _cost_summary(run.id, run.report_id_fk, sections, result.get("usage_record_failures", 0))
    return payload


@bp.route("/report-runs/<run_id>", methods=["GET"])
@login_required
@admin_required
def get_report_run(run_id: str):
    run = ReportRunStore().get(run_id) if _log_run_id(run_id) else None
    if run is None:
        return jsonify({"error": "La ejecución no existe."}), 404
    response = jsonify(_run_payload(run))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/report-runs/<run_id>/cancel", methods=["POST"])
@login_required
@admin_required
def cancel_report_run(run_id: str):
    status = ReportRunStore().request_cancel(run_id) if _log_run_id(run_id) else None
    if status is None:
        return jsonify({"error": "La ejecución no existe o ya finalizó."}), 404
    return jsonify({"run_id": run_id, "status": status}), 202


def _cost_summary(report_run_id: str, report_id: int, sections, ledger_record_failures: int) -> dict | None:
    """Cost tab payload. The route only asks ``ReportCostService``; it never queries or prices.

    A failure here must never cost the admin their report, so it degrades to ``None``.
    """
    try:
        summary = ReportCostService().summarize(
            report_run_id, report_id=report_id,
            section_titles={section.key: section.title for section in sections})
        payload = summary.to_payload()
    except Exception:
        logging.exception("[AIReporting] Cost summary failed report_run_id=%s", _log_run_id(report_run_id))
        return None
    # Ledger writes that failed are invisible to the ledger query: say so instead of
    # presenting an incomplete total as complete.
    payload["ledger_record_failures"] = ledger_record_failures
    return payload


def _cost_payload(result) -> dict | None:
    draft = result.draft
    return _cost_summary(
        draft.report_run_id, draft.report_id, draft.sections,
        result.usage_record_failures
        + (result.coordination.extra_usage_record_failures if result.coordination else 0))


def _pdf_error(code: str, message: str, status: int):
    return jsonify({"error": message, "code": code}), status


def _log_run_id(value) -> str | None:
    """``report_run_id`` is only used for log correlation; anything unexpected is dropped."""
    return value if isinstance(value, str) and _RUN_ID_RE.match(value) else None


@bp.route("/render-pdf", methods=["POST"])
@login_required
@admin_required
def render_pdf():
    """Download an already generated ``FinalReport`` as PDF.

    Body: ``{"final_report": {...}, "report_run_id": "..."?}``. The client never sends HTML.
    A run with stored artifacts is authoritative: its stored HTML (and the schema/renderer
    versions it was produced with) is used. Otherwise the ``FinalReport`` JSON is validated
    with the model of its declared version and rendered by that schema's registered renderer
    (``ArtifactService.export_source``) before Chromium sees it. No LLM, analytics,
    coordinator or writer is involved, and nothing is stored.
    """
    if (request.content_length or 0) > MAX_PDF_PAYLOAD_BYTES:
        return _pdf_error("invalid_final_report", "El informe supera el tamaño permitido.", 413)
    payload = request.get_json(silent=True)
    raw_report = payload.get("final_report") if isinstance(payload, dict) else None
    run_id = _log_run_id(payload.get("report_run_id")) if isinstance(payload, dict) else None
    started = time.perf_counter()
    try:
        # Schema/renderer selection (and the choice between the run's stored artifacts and the
        # client's JSON) lives in ArtifactService; the route never compares versions.
        source = ArtifactService().export_source(run_id, raw_report if isinstance(raw_report, dict) else None)
    except ValueError as exc:  # unusable report: bad content (pydantic) or unknown schema version
        logging.warning("[AIReporting] PDF rejected: invalid FinalReport report_run_id=%s errors=%s",
                        run_id, exc.error_count() if hasattr(exc, "error_count") else type(exc).__name__)
        return _pdf_error("invalid_final_report", "El informe recibido no es válido.", 400)
    except Exception:
        logging.error("[AIReporting] PDF failed report_run_id=%s code=pdf_render_failed latency_ms=%d",
                      run_id, (time.perf_counter() - started) * 1000, exc_info=True)
        return _pdf_error("pdf_render_failed",
                          "No se pudo generar el PDF. El informe no se vio afectado; intentá nuevamente.", 500)
    report = source.report
    try:
        pdf = asyncio.run(build_pdf_renderer().render(source.html))
    except PdfRenderError as exc:
        logging.error("[AIReporting] PDF failed report_run_id=%s code=%s latency_ms=%d",
                      run_id, exc.code, (time.perf_counter() - started) * 1000, exc_info=True)
        if isinstance(exc, PdfBrowserUnavailableError):
            return _pdf_error(exc.code, "El generador de PDF no está disponible en este momento. "
                                        "El informe y su HTML no se vieron afectados.", 503)
        return _pdf_error(exc.code, "No se pudo generar el PDF. El informe no se vio afectado; intentá nuevamente.", 500)
    except Exception:
        logging.error("[AIReporting] PDF failed report_run_id=%s code=pdf_render_failed latency_ms=%d",
                      run_id, (time.perf_counter() - started) * 1000, exc_info=True)
        return _pdf_error("pdf_render_failed",
                          "No se pudo generar el PDF. El informe no se vio afectado; intentá nuevamente.", 500)
    logging.info("[AIReporting] PDF ok report_run_id=%s latency_ms=%d size_bytes=%d schema=%s renderer=%s origin=%s",
                 run_id, (time.perf_counter() - started) * 1000, len(pdf),
                 source.schema_version, source.renderer_version, source.origin)
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{report_pdf_filename(report)}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })
