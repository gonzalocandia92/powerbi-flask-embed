"""
Workspace management routes.
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required

from app import db
from app.models import Workspace, Tenant, Report
from app.forms import WorkspaceForm
from app.utils.decorators import retry_on_db_error

bp = Blueprint('workspaces', __name__, url_prefix='/workspaces')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    workspaces = Workspace.query.options(db.joinedload(Workspace.tenant)).all()
    return render_template('workspaces/list.html', workspaces=workspaces, title='Workspaces')


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    form = WorkspaceForm()
    form.tenant.choices = [(t.id, t.name) for t in Tenant.query.order_by(Tenant.name).all()]
    
    if form.validate_on_submit():
        workspace = Workspace(
            name=form.name.data,
            workspace_id=form.workspace_id.data,
            tenant_id_fk=form.tenant.data
        )
        db.session.add(workspace)
        db.session.commit()
        flash("Workspace creado", "success")
        return redirect(url_for('workspaces.list'))
    
    return render_template('base_form.html', form=form, title='Nuevo Workspace', back_url=url_for('workspaces.list'))


@bp.route('/<int:workspace_id>/detail')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(workspace_id):
    workspace = Workspace.query.options(db.joinedload(Workspace.tenant)).get_or_404(workspace_id)
    reports = Report.query.filter_by(workspace_id_fk=workspace_id).all()
    return render_template('workspaces/detail.html', workspace=workspace, reports=reports)


@bp.route('/<int:workspace_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(workspace_id):
    workspace = Workspace.query.get_or_404(workspace_id)
    form = WorkspaceForm(obj=workspace)
    form.tenant.choices = [(t.id, t.name) for t in Tenant.query.order_by(Tenant.name).all()]
    
    if request.method == 'GET':
        form.tenant.data = workspace.tenant_id_fk
    
    if form.validate_on_submit():
        workspace.name = form.name.data
        workspace.workspace_id = form.workspace_id.data
        workspace.tenant_id_fk = form.tenant.data
        db.session.commit()
        flash("Workspace actualizado", "success")
        return redirect(url_for('workspaces.detail', workspace_id=workspace_id))
    
    return render_template('base_form.html', form=form, title='Editar Workspace', back_url=url_for('workspaces.detail', workspace_id=workspace_id))


@bp.route('/<int:workspace_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(workspace_id):
    workspace = Workspace.query.get_or_404(workspace_id)
    report_count = Report.query.filter_by(workspace_id_fk=workspace_id).count()
    if report_count > 0:
        flash(f"No se puede eliminar el workspace porque tiene {report_count} reports asociados", "danger")
        return redirect(url_for('workspaces.detail', workspace_id=workspace_id))
    name = workspace.name
    db.session.delete(workspace)
    db.session.commit()
    logging.info(f"Workspace deleted: {name} (ID: {workspace_id})")
    flash(f"Workspace '{name}' eliminado", "success")
    return redirect(url_for('workspaces.list'))
