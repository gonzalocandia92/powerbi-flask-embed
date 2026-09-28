"""Administrative API and screens for reproducible KLARA evaluations."""
import csv
import json
from io import StringIO

from flask import Blueprint, Response, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from app import db
from app.models import AIModelConfig, ModelEvaluationCase, ModelEvaluationRun, Report
from app.services.evaluation import (
    EvaluationQuestion, EvaluationSpec, enqueue_evaluation,
)
from app.services.model_catalog import model_readiness
from app.utils.decorators import admin_required


bp = Blueprint('evaluations', __name__, url_prefix='/admin/ai-evaluations')
REVIEW_STATUSES = {'unreviewed', 'correct', 'partial', 'incorrect'}
MAX_CSV_BYTES = 2 * 1024 * 1024


def _case_json(case):
    return {
        "id": case.id, "sequence_index": case.sequence_index,
        "question": case.question, "expected_answer": case.expected_answer,
        "answer": case.answer, "model_key": case.model_key,
        "cache_mode": case.cache_mode, "provider": case.provider,
        "physical_model": case.physical_model, "actual_model": case.actual_model,
        "gateway": case.gateway, "service_tier": case.service_tier,
        "status": case.status, "latency_ms": case.latency_ms,
        "input_tokens": case.input_tokens, "output_tokens": case.output_tokens,
        "cache_read_tokens": case.cache_read_tokens,
        "cache_write_tokens": case.cache_write_tokens,
        "reasoning_tokens": case.reasoning_tokens,
        "main_model_cost": case.main_model_cost,
        "decision_layer_cost": case.decision_layer_cost,
        "pipeline_total_cost": case.pipeline_total_cost,
        "selected_skills": case.selected_skills_json,
        "candidate_skills": case.candidate_skills_json,
        "complexity_assessment": case.complexity_assessment_json,
        "execution_policy": case.execution_policy_json,
        "tools_called": case.tools_called_json, "dax_query": case.dax_query,
        "tool_rounds": case.tool_rounds, "failure_reason": case.failure_reason,
        "error_message": case.error_message, "trace_id": case.trace_id,
        "metrics": case.metrics_json,
        "review_status": case.review_status, "review_notes": case.review_notes,
        "reviewed_by_user_id": case.reviewed_by_user_id,
        "reviewed_at": case.reviewed_at.isoformat() if case.reviewed_at else None,
    }


def _run_json(run, *, include_cases=False):
    result = {
        "id": run.id, "name": run.name, "report_id": run.report_id_fk,
        "mode": run.mode, "cache_mode": run.cache_mode, "status": run.status,
        "configuration": run.configuration_json, "summary": run.summary_json,
        "error_message": run.error_message,
        "total_cases": run.total_cases, "completed_cases": run.completed_cases,
        "progress_percent": round((run.completed_cases or 0) * 100 / run.total_cases) if run.total_cases else 0,
        "requested_by_user_id": run.requested_by_user_id,
        "cancel_requested_at": run.cancel_requested_at.isoformat() if run.cancel_requested_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }
    if include_cases:
        result["cases"] = [_case_json(case) for case in run.cases.all()]
    return result


@bp.route('', methods=['GET'])
@login_required
@admin_required
def list_runs():
    limit = min(max(request.args.get('limit', 50, type=int), 1), 200)
    runs = ModelEvaluationRun.query.order_by(ModelEvaluationRun.created_at.desc()).limit(limit).all()
    return jsonify([_run_json(run) for run in runs])


@bp.route('/<int:run_id>', methods=['GET'])
@login_required
@admin_required
def get_run(run_id):
    run = db.get_or_404(ModelEvaluationRun, run_id)
    return jsonify(_run_json(run, include_cases=True))


@bp.route('', methods=['POST'])
@login_required
@admin_required
def create_run():
    data = request.get_json(silent=True) or {}
    report = db.session.get(Report, data.get('report_id'))
    if report is None:
        return jsonify({"error": "report_id is required and must exist"}), 400
    try:
        questions = [
            EvaluationQuestion(
                question=(item if isinstance(item, str) else item.get('question', '')).strip(),
                expected_answer=None if isinstance(item, str) else item.get('expected_answer'),
            )
            for item in (data.get('questions') or [])
        ]
        spec = EvaluationSpec(
            name=(data.get('name') or f"Evaluation report {report.id}").strip(),
            report_id=report.id,
            model_keys=[str(value).strip() for value in (data.get('model_keys') or []) if str(value).strip()],
            questions=questions,
            mode=data.get('mode') or 'independent',
            cache_mode=data.get('cache_mode') or 'cold',
            configuration=dict(data.get('configuration') or {}),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        for model_key in spec.model_keys:
            model = AIModelConfig.query.filter_by(model_key=model_key).first()
            if model is None:
                raise ValueError(f'Modelo desconocido: {model_key}')
            readiness = model_readiness(model, config=dict(current_app.config))
            if not readiness['ready_for_evaluation']:
                blockers = ', '.join(readiness['blockers']) or 'configuracion incompleta'
                raise ValueError(f'El modelo {model_key} no esta listo para evaluar: {blockers}')
        run = enqueue_evaluation(spec, requested_by_user_id=current_user.id)
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    payload = _run_json(run, include_cases=True)
    payload["status_url"] = f"/admin/ai-evaluations/{run.id}"
    return jsonify(payload), 202


@bp.route('/import-questions', methods=['POST'])
@login_required
@admin_required
def import_questions():
    upload = request.files.get('csv_file')
    if not upload or not upload.filename:
        return jsonify({'errors': ['Selecciona un archivo CSV.'], 'warnings': [], 'rows': []}), 400
    raw = upload.stream.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        return jsonify({'errors': ['El CSV supera el limite de 2 MB.'], 'warnings': [], 'rows': []}), 400
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return jsonify({'errors': ['El CSV debe estar codificado en UTF-8.'], 'warnings': [], 'rows': []}), 400
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;')
        reader = csv.DictReader(StringIO(text), dialect=dialect)
    except csv.Error as exc:
        return jsonify({'errors': [f'CSV invalido: {exc}'], 'warnings': [], 'rows': []}), 400
    normalized_headers = {str(name or '').strip().lower(): name for name in (reader.fieldnames or [])}
    question_header = next((normalized_headers[key] for key in ('question', 'pregunta') if key in normalized_headers), None)
    expected_header = next((normalized_headers[key] for key in ('expected_answer', 'respuesta_esperada') if key in normalized_headers), None)
    if question_header is None:
        return jsonify({'errors': ['Falta la columna question o pregunta.'], 'warnings': [], 'rows': []}), 400
    rows, errors, warnings, seen = [], [], [], set()
    for line_number, raw_row in enumerate(reader, start=2):
        question = str(raw_row.get(question_header) or '').strip()
        expected = str(raw_row.get(expected_header) or '').strip() if expected_header else ''
        if not question:
            errors.append(f'Fila {line_number}: la pregunta esta vacia.')
            continue
        if question.casefold() in seen:
            warnings.append(f'Fila {line_number}: pregunta duplicada.')
        seen.add(question.casefold())
        rows.append({'question': question, 'expected_answer': expected or None, 'line': line_number})
    if len(rows) > 100:
        errors.append('El archivo contiene mas de 100 preguntas.')
    status = 400 if errors else 200
    return jsonify({'rows': rows[:100], 'errors': errors, 'warnings': warnings,
                    'summary': {'questions': len(rows), 'with_expected_answer': sum(bool(row['expected_answer']) for row in rows)}}), status


@bp.route('/questions-template.csv', methods=['GET'])
@login_required
@admin_required
def questions_template():
    return Response(
        '\ufeffquestion,expected_answer\n"¿Cuales fueron las ventas del mes?",""\n',
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="preguntas-evaluacion.csv"'},
    )


@bp.route('/<int:run_id>/cancel', methods=['POST'])
@login_required
@admin_required
def cancel_run(run_id):
    run = db.get_or_404(ModelEvaluationRun, run_id)
    if run.status not in {'queued', 'running'}:
        return jsonify({'error': 'La evaluacion ya finalizo.'}), 409
    from datetime import datetime, timezone
    run.cancel_requested_at = datetime.now(timezone.utc)
    if run.status == 'queued':
        for case in run.cases.filter_by(status='pending').all():
            case.status = 'cancelled'
            case.completed_at = run.cancel_requested_at
        run.status = 'cancelled'
        run.completed_cases = run.total_cases
        run.completed_at = run.cancel_requested_at
    else:
        run.status = 'cancel_requested'
    db.session.commit()
    return jsonify(_run_json(run, include_cases=False))


@bp.route('/<int:run_id>/cases/<int:case_id>/review', methods=['PATCH'])
@login_required
@admin_required
def review_case(run_id, case_id):
    case = ModelEvaluationCase.query.filter_by(id=case_id, run_id=run_id).first_or_404()
    data = request.get_json(silent=True) or {}
    status = str(data.get('review_status') or '').strip()
    if status not in REVIEW_STATUSES:
        return jsonify({'error': 'review_status invalido'}), 400
    from datetime import datetime, timezone
    case.review_status = status
    case.review_notes = str(data.get('review_notes') or '').strip() or None
    case.reviewed_by_user_id = current_user.id if status != 'unreviewed' else None
    case.reviewed_at = datetime.now(timezone.utc) if status != 'unreviewed' else None
    db.session.commit()
    return jsonify(_case_json(case))


@bp.route('/<int:run_id>/models/<string:model_key>/promote', methods=['POST'])
@login_required
@admin_required
def promote_model(run_id, model_key):
    run = db.get_or_404(ModelEvaluationRun, run_id)
    if run.status not in {'completed', 'completed_with_errors'}:
        return jsonify({'error': 'La evaluacion no finalizo.'}), 409
    cases = run.cases.filter_by(model_key=model_key).all()
    if not cases:
        return jsonify({'error': 'El modelo no pertenece a esta evaluacion.'}), 404
    if any(case.status != 'success' or case.review_status != 'correct' for case in cases):
        return jsonify({'error': 'Todos los casos deben ser exitosos y estar revisados como correctos.'}), 409
    model = AIModelConfig.query.filter_by(model_key=model_key).first_or_404()
    from datetime import datetime, timezone
    model.validation_status = 'passed'
    model.validated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'model_key': model.model_key, 'validation_status': model.validation_status,
                    'validated_at': model.validated_at.isoformat()})


def _csv_safe(value):
    if value is None:
        return ''
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return "'" + text if text.startswith(('=', '+', '-', '@')) else text


@bp.route('/<int:run_id>/export.csv', methods=['GET'])
@login_required
@admin_required
def export_run(run_id):
    run = db.get_or_404(ModelEvaluationRun, run_id)
    fields = [
        'case_id', 'model_key', 'cache_mode', 'question', 'expected_answer', 'answer',
        'status', 'review_status', 'review_notes', 'latency_ms', 'input_tokens',
        'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens',
        'main_model_cost', 'decision_layer_cost', 'pipeline_total_cost', 'tools_called',
        'selected_skills', 'dax_query', 'failure_reason', 'error_message', 'trace_id',
    ]
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for case in run.cases.order_by(ModelEvaluationCase.sequence_index.asc()).all():
        writer.writerow({key: _csv_safe(value) for key, value in {
            'case_id': case.id, 'model_key': case.model_key, 'cache_mode': case.cache_mode,
            'question': case.question, 'expected_answer': case.expected_answer, 'answer': case.answer,
            'status': case.status, 'review_status': case.review_status, 'review_notes': case.review_notes,
            'latency_ms': case.latency_ms, 'input_tokens': case.input_tokens,
            'output_tokens': case.output_tokens, 'cache_read_tokens': case.cache_read_tokens,
            'cache_write_tokens': case.cache_write_tokens, 'reasoning_tokens': case.reasoning_tokens,
            'main_model_cost': case.main_model_cost, 'decision_layer_cost': case.decision_layer_cost,
            'pipeline_total_cost': case.pipeline_total_cost, 'tools_called': case.tools_called_json,
            'selected_skills': case.selected_skills_json, 'dax_query': case.dax_query,
            'failure_reason': case.failure_reason, 'error_message': case.error_message,
            'trace_id': case.trace_id,
        }.items()})
    return Response('\ufeff' + buffer.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="evaluacion-{run.id}.csv"'})


@bp.route('/ui', methods=['GET'])
@login_required
@admin_required
def runs_page():
    runs = ModelEvaluationRun.query.order_by(ModelEvaluationRun.created_at.desc()).limit(100).all()
    return render_template('admin/ai_evaluations/index.html', runs=runs)


@bp.route('/ui/new', methods=['GET'])
@login_required
@admin_required
def new_page():
    models = AIModelConfig.query.filter_by(enabled=True).order_by(AIModelConfig.display_name.asc()).all()
    return render_template(
        'admin/ai_evaluations/new.html',
        reports=Report.query.order_by(Report.name.asc()).all(),
        models=[model for model in models if model_readiness(
            model, config=dict(current_app.config)
        )['ready_for_evaluation']],
    )


@bp.route('/ui/<int:run_id>', methods=['GET'])
@login_required
@admin_required
def detail_page(run_id):
    run = db.get_or_404(ModelEvaluationRun, run_id)
    cases = run.cases.all()
    return render_template(
        'admin/ai_evaluations/detail.html', run=run,
        total_cost=sum(case.pipeline_total_cost or 0 for case in cases),
        model_keys=list(dict.fromkeys(case.model_key for case in cases)),
    )
