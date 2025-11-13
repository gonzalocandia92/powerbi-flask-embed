"""
Public report viewing routes (no authentication required).
"""
import logging
from flask import Blueprint, render_template, request, make_response

from app.models import PublicLink
from app.utils.decorators import retry_on_db_error
from app.utils.powerbi import get_embed_for_config
from app.utils.analytics import track_visit, generate_visitor_id

bp = Blueprint('public', __name__, url_prefix='/p')


@bp.route('/<custom_slug>')
@retry_on_db_error(max_retries=3, delay=1)
def view(custom_slug):
    """View a report via public link (no authentication required)."""
    link = PublicLink.query.filter_by(custom_slug=custom_slug, is_active=True).first_or_404()
    config = link.report_config
    
    # Get or create visitor ID from cookie
    import re
    existing_visitor_id = request.cookies.get('visitor_id')
    
    # Validate visitor_id format (must be valid UUID)
    visitor_id_is_valid = (
        existing_visitor_id and
        re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', existing_visitor_id)
    )
    
    # Use validated existing visitor_id or generate new one
    if visitor_id_is_valid:
        visitor_id = existing_visitor_id
    else:
        visitor_id = generate_visitor_id()
    
    # Track the visit with the visitor_id (either validated or newly generated)
    track_visit(custom_slug, request, visitor_id)
    
    try:
        embed_token, embed_url, report_id = get_embed_for_config(config)
    except Exception as e:
        logging.error(f"Error generating embed token: {e}")
        return render_template(
            'error_public.html',
            error_message=f"Error generando embed token: {e}",
            config_name=config.name
        ), 500
    
    response = make_response(render_template(
        'report_base.html',
        embed_token=embed_token,
        embed_url=embed_url,
        report_id=report_id,
        config_name=config.name,
        is_public=True
    ))
    
    # Set visitor ID cookie only if it didn't exist or was invalid
    # At this point, visitor_id is either validated or newly generated (always safe)
    if not visitor_id_is_valid:
        # Set secure cookie with all security flags
        response.set_cookie(
            'visitor_id',
            visitor_id,  # Safe: either newly generated UUID or validated existing one
            max_age=60*60*24*365*2,
            httponly=True,
            secure=request.is_secure,  # Set Secure flag for HTTPS
            samesite='Lax'
        )
    
    return response
