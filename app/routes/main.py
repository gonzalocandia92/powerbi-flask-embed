"""
Main application routes.
"""
from flask import Blueprint, render_template
from flask_login import login_required

from app import db
from app.models import PublicLink, Report, Workspace
from app.utils.decorators import retry_on_db_error

bp = Blueprint('main', __name__)


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def index():
    """Display the main page with all public links."""
    public_links = PublicLink.query.filter_by(is_active=True).options(
        db.joinedload(PublicLink.report).joinedload(Report.workspace).joinedload(Workspace.tenant)
    ).order_by(PublicLink.created_at.desc()).all()
    
    return render_template('index.html', public_links=public_links)
