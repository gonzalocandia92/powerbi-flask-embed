"""
Public report viewing routes (no authentication required).
"""
import logging
import time
import requests as _requests_lib
from flask import Blueprint, render_template, request, make_response, jsonify

from app.models import PublicLink, Report, Workspace, Tenant
from app.utils.decorators import retry_on_db_error
from app.utils.powerbi import get_embed_for_report, refresh_dataset
from app.utils.analytics import track_visit, generate_visitor_id
from app import db

bp = Blueprint('public', __name__, url_prefix='/p')

# In-memory rate limiting: stores last refresh timestamp per slug
_refresh_timestamps = {}

# Minimum seconds between refreshes per slug
_REFRESH_COOLDOWN = 1800  # 30 minutes


def _cleanup_refresh_timestamps():
    """Remove entries older than the cooldown period to prevent unbounded growth."""
    cutoff = time.time() - _REFRESH_COOLDOWN
    stale = [slug for slug, ts in _refresh_timestamps.items() if ts < cutoff]
    for slug in stale:
        del _refresh_timestamps[slug]


@bp.route('/<custom_slug>')
@retry_on_db_error(max_retries=3, delay=1)
def view(custom_slug):
    """View a report via public link (no authentication required)."""
    link = PublicLink.query.filter_by(custom_slug=custom_slug, is_active=True).first_or_404()
    report = link.report
    
    import re
    existing_visitor_id = request.cookies.get('visitor_id')
    visitor_id_is_valid = (
        existing_visitor_id and
        re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', existing_visitor_id)
    )
    
    if visitor_id_is_valid:
        visitor_id = existing_visitor_id
    else:
        visitor_id = generate_visitor_id()
    
    track_visit(custom_slug, request, visitor_id)
    
    try:
        embed_token, embed_url, report_id = get_embed_for_report(report)
    except Exception as e:
        logging.error(f"Error generating embed token: {e}")
        return render_template(
            'error_public.html',
            error_message=f"Error generando embed token: {e}",
            config_name=report.name
        ), 500
    
    response = make_response(render_template(
        'report_base.html',
        embed_token=embed_token,
        embed_url=embed_url,
        report_id=report_id,
        config_name=report.name,
        is_public=True,
        allow_refresh=link.allow_refresh
    ))
    
    if not visitor_id_is_valid:
        response.set_cookie(
            'visitor_id', visitor_id,
            max_age=60*60*24*365*2,
            httponly=True,
            secure=request.is_secure,
            samesite='Lax'
        )
    
    return response


@bp.route('/<custom_slug>/refresh', methods=['POST'])
@retry_on_db_error(max_retries=3, delay=1)
def refresh(custom_slug):
    """Trigger dataset refresh from a public link (if allowed)."""
    link = PublicLink.query.filter_by(custom_slug=custom_slug, is_active=True).first_or_404()

    if not link.allow_refresh:
        return jsonify({"error": "Refresh not allowed for this link"}), 403

    # Rate limiting: enforce minimum cooldown between refreshes
    now = time.time()
    _cleanup_refresh_timestamps()
    last_refresh = _refresh_timestamps.get(custom_slug)
    if last_refresh is not None:
        elapsed = now - last_refresh
        if elapsed < _REFRESH_COOLDOWN:
            retry_after = int(_REFRESH_COOLDOWN - elapsed)
            return jsonify({
                "error": "Debe esperar al menos 30 minutos entre actualizaciones",
                "retry_after": retry_after
            }), 429

    # Load full relationship chain needed by refresh_dataset
    report = db.session.get(
        Report,
        link.report_id_fk,
        options=[
            db.joinedload(Report.workspace).joinedload(Workspace.tenant).joinedload(Tenant.client),
            db.joinedload(Report.usuario_pbi)
        ]
    )

    try:
        result = refresh_dataset(report)
        _refresh_timestamps[custom_slug] = now
        return jsonify({
            "status": "success",
            "message": "Actualización del modelo semántico iniciada",
            "dataset_id": result["dataset_id"]
        }), 202
    except Exception as e:
        if isinstance(e, _requests_lib.HTTPError):
            status_code = e.response.status_code if e.response is not None else 0
            if status_code == 429:
                logging.error(f"Power BI refresh quota exceeded for slug '{custom_slug}': {e}")
                return jsonify({"error": "Se alcanzó el límite diario de actualizaciones de Power BI"}), 429
            logging.error(f"Power BI API error during refresh for slug '{custom_slug}': {e}")
            return jsonify({"error": "Error al actualizar el modelo semántico"}), 500
        logging.error(f"Refresh error for slug '{custom_slug}': {e}")
        return jsonify({"error": "Error al actualizar el modelo semántico"}), 500
