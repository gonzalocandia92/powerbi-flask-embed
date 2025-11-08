"""
Public report viewing routes (no authentication required).
"""
import logging
from flask import Blueprint, render_template

from app.models import PublicLink
from app.utils.decorators import retry_on_db_error
from app.utils.powerbi import get_embed_for_config

bp = Blueprint('public', __name__, url_prefix='/p')


@bp.route('/<custom_slug>')
@retry_on_db_error(max_retries=3, delay=1)
def view(custom_slug):
    """View a report via public link (no authentication required)."""
    link = PublicLink.query.filter_by(custom_slug=custom_slug, is_active=True).first_or_404()
    config = link.report_config
    
    try:
        embed_token, embed_url, report_id = get_embed_for_config(config)
    except Exception as e:
        logging.error(f"Error generating embed token: {e}")
        return render_template(
            'error_public.html',
            error_message=f"Error generando embed token: {e}",
            config_name=config.name
        ), 500
    
    return render_template(
        'report_base.html',
        embed_token=embed_token,
        embed_url=embed_url,
        report_id=report_id,
        config_name=config.name,
        is_public=True
    )
