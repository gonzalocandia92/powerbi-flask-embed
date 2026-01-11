"""
Report management routes.
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Report, ReportConfig
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
        fields=['id', 'name', 'report_id', 'embed_url'],
        has_actions=True,
        detail_endpoint='reports.detail',
        edit_endpoint='reports.edit',
        delete_endpoint='reports.delete',
        id_param='report_id'
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


@bp.route('/<int:report_id>/detail')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(report_id):
    """Display report details."""
    report = Report.query.get_or_404(report_id)
    configs = ReportConfig.query.filter_by(report_id=report_id).all()
    
    return render_template(
        'reports/detail.html',
        report=report,
        configs=configs
    )


@bp.route('/<int:report_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(report_id):
    """Edit a report."""
    report = Report.query.get_or_404(report_id)
    form = ReportForm(obj=report)
    
    if form.validate_on_submit():
        report.name = form.name.data
        report.report_id = form.report_id.data
        report.embed_url = form.embed_url.data
        db.session.commit()
        flash("Report actualizado", "success")
        return redirect(url_for('reports.detail', report_id=report_id))
    
    return render_template(
        'base_form.html',
        form=form,
        title='Editar Report',
        back_url=url_for('reports.detail', report_id=report_id)
    )


@bp.route('/<int:report_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(report_id):
    """Delete a report."""
    report = Report.query.get_or_404(report_id)
    
    # Check if report is in use
    config_count = ReportConfig.query.filter_by(report_id=report_id).count()
    if config_count > 0:
        flash(f"No se puede eliminar el report porque está asociado a {config_count} configuraciones", "danger")
        return redirect(url_for('reports.detail', report_id=report_id))
    
    name = report.name
    db.session.delete(report)
    db.session.commit()
    
    logging.info(f"Report deleted: {name} (ID: {report_id})")
    flash(f"Report '{name}' eliminado", "success")
    return redirect(url_for('reports.list'))
