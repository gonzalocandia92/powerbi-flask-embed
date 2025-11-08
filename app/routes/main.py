"""
Main application routes.
"""
from flask import Blueprint, render_template
from flask_login import login_required

from app import db
from app.models import ReportConfig, PublicLink
from app.utils.decorators import retry_on_db_error

bp = Blueprint('main', __name__)


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def index():
    """Display the main dashboard with all report configurations."""
    configs = ReportConfig.query.options(
        db.joinedload(ReportConfig.tenant),
        db.joinedload(ReportConfig.client),
        db.joinedload(ReportConfig.workspace),
        db.joinedload(ReportConfig.report),
        db.joinedload(ReportConfig.usuario_pbi)
    ).order_by(ReportConfig.created_at.desc()).all()
    
    public_links = PublicLink.query.filter_by(is_active=True).all()
    
    links_by_config = {}
    for link in public_links:
        if link.report_config_id not in links_by_config:
            links_by_config[link.report_config_id] = []
        links_by_config[link.report_config_id].append(link)
    
    return render_template('index.html', configs=configs, links_by_config=links_by_config)
