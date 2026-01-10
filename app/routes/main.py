"""
Main application routes.
"""
from flask import Blueprint, render_template, request
from flask_login import login_required

from app import db
from app.models import ReportConfig, PublicLink
from app.utils.decorators import retry_on_db_error

bp = Blueprint('main', __name__)


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def index():
    """Display the main dashboard with all public links."""
    # Get filter parameters
    search_query = request.args.get('search', '').strip()
    
    # Query all active public links with their related configs
    query = PublicLink.query.filter_by(is_active=True).options(
        db.joinedload(PublicLink.report_config)
            .joinedload(ReportConfig.report),
        db.joinedload(PublicLink.report_config)
            .joinedload(ReportConfig.workspace),
        db.joinedload(PublicLink.report_config)
            .joinedload(ReportConfig.tenant)
    )
    
    # Apply search filter if provided
    if search_query:
        query = query.join(PublicLink.report_config).join(ReportConfig.report).filter(
            db.or_(
                PublicLink.custom_slug.ilike(f'%{search_query}%'),
                ReportConfig.name.ilike(f'%{search_query}%'),
                ReportConfig.report.has(db.Report.name.ilike(f'%{search_query}%'))
            )
        )
    
    public_links = query.order_by(PublicLink.created_at.desc()).all()
    
    return render_template('index.html', public_links=public_links, search_query=search_query)
