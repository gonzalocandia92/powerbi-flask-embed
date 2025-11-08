"""
Report management routes.
"""
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Report
from app.forms import ReportForm
from app.utils.decorators import retry_on_db_error

bp = Blueprint('reports', __name__, url_prefix='/reports')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    """Display list of all reports."""
    reports = Report.query.all()
    
    return render_template(
        'base_list.html',
        items=reports,
        title='Reports',
        model_name='Report',
        model_name_plural='reports',
        new_url=url_for('reports.new'),
        headers=['#', 'Nombre', 'Report ID', 'Embed URL'],
        fields=['id', 'name', 'report_id', 'embed_url']
    )


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    """Create a new report."""
    form = ReportForm()
    
    if form.validate_on_submit():
        report = Report(
            name=form.name.data,
            report_id=form.report_id.data,
            embed_url=form.embed_url.data
        )
        db.session.add(report)
        db.session.commit()
        flash("Reporte creado", "success")
        return redirect(url_for('reports.list'))
    
    return render_template(
        'base_form.html',
        form=form,
        title='Nuevo Report',
        back_url=url_for('reports.list')
    )
