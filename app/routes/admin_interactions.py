"""Admin screen: KLARA interactions across every channel (chat, WhatsApp, MCP, ...).

Reads exclusively through ``app.services.interactions`` (InteractionQueryService)
— never through the public/legacy ``/api/chatbot/sessions*`` endpoints (those
are unauthenticated and out of scope for an admin audit surface) and never by
querying ChatSession/ChatMessage/McpInteraction directly from this module.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from app.models import Empresa, Report
from app.services.interactions import InteractionFilters, interaction_query_service
from app.utils.decorators import admin_required

bp = Blueprint('admin_interactions', __name__, url_prefix='/admin/interactions')

_CHANNEL_OPTIONS = [
    ("", "Todos"),
    ("klara_chat", "Klara Chat"),
    ("whatsapp", "WhatsApp"),
    ("mcp", "MCP"),
    ("unknown", "Sin identificar"),
]


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _filters_from_request(args) -> InteractionFilters:
    date_from = _parse_date(args.get("date_from"))
    date_to = _parse_date(args.get("date_to"))
    if date_to is not None:
        # Filters are inclusive on the UI side; the query layer treats
        # date_to as an exclusive upper bound, so push it one day forward.
        date_to = date_to + timedelta(days=1)
    return InteractionFilters(
        date_from=date_from,
        date_to=date_to,
        empresa_id=args.get("empresa_id", type=int),
        report_id=args.get("report_id", type=int),
        channel=(args.get("channel") or "").strip() or None,
        search=args.get("q"),
        page=args.get("page", 1, type=int),
        page_size=args.get("page_size", 25, type=int),
    )


def _row_json(row):
    return {
        "id": row.id,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "empresa_id": row.empresa_id,
        "empresa_name": row.empresa_name,
        "report_id": row.report_id,
        "report_name": row.report_name,
        "channel": row.channel,
        "question": row.question,
        "answer": row.answer,
        "total_cost_usd": float(row.total_cost_usd) if row.total_cost_usd is not None else None,
        "model_key": row.model_key,
        "latency_ms": row.latency_ms,
        "had_error": row.had_error,
    }


@bp.route('', methods=['GET'])
@login_required
@admin_required
def index():
    empresas = Empresa.query.order_by(Empresa.nombre).all()
    reports = Report.query.order_by(Report.name).all()
    return render_template(
        'admin/interactions/index.html',
        empresas=empresas,
        reports=reports,
        channel_options=_CHANNEL_OPTIONS,
        today=date.today().isoformat(),
    )


@bp.route('/api/list', methods=['GET'])
@login_required
@admin_required
def api_list():
    filters = _filters_from_request(request.args)
    page = interaction_query_service.query(filters)
    return jsonify({
        "rows": [_row_json(row) for row in page.rows],
        "total": page.total,
        "page": page.page,
        "page_size": page.page_size,
        "total_pages": page.total_pages,
    })


@bp.route('/api/detail/<string:interaction_id>', methods=['GET'])
@login_required
@admin_required
def api_detail(interaction_id):
    detail = interaction_query_service.get_detail(interaction_id)
    if detail is None:
        return jsonify({"error": "not_found"}), 404

    def _serialize(value):
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    return jsonify({key: _serialize(value) for key, value in detail.items()})
