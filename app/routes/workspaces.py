"""
Workspace management routes.
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Workspace, ReportConfig
from app.forms import WorkspaceForm
from app.utils.decorators import retry_on_db_error

bp = Blueprint('workspaces', __name__, url_prefix='/workspaces')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    """Display list of all workspaces."""
    workspaces = Workspace.query.all()
    
    return render_template(
        'base_list.html',
        items=workspaces,
        title='Workspaces',
        model_name='Workspace',
        model_name_plural='workspaces',
        new_url=url_for('workspaces.new'),
        headers=['#', 'Nombre', 'Workspace ID'],
        fields=['id', 'name', 'workspace_id'],
        has_actions=True,
        detail_endpoint='workspaces.detail',
        edit_endpoint='workspaces.edit',
        delete_endpoint='workspaces.delete'
    )


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    """Create a new workspace."""
    form = WorkspaceForm()
    
    if form.validate_on_submit():
        workspace = Workspace(
            name=form.name.data,
            workspace_id=form.workspace_id.data
        )
        db.session.add(workspace)
        db.session.commit()
        flash("Workspace creado", "success")
        return redirect(url_for('workspaces.list'))
    
    return render_template(
        'base_form.html',
        form=form,
        title='Nuevo Workspace',
        back_url=url_for('workspaces.list')
    )


@bp.route('/<int:workspace_id>/detail')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(workspace_id):
    """Display workspace details."""
    workspace = Workspace.query.get_or_404(workspace_id)
    configs = ReportConfig.query.filter_by(workspace_id=workspace_id).all()
    
    return render_template(
        'workspaces/detail.html',
        workspace=workspace,
        configs=configs
    )


@bp.route('/<int:workspace_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(workspace_id):
    """Edit a workspace."""
    workspace = Workspace.query.get_or_404(workspace_id)
    form = WorkspaceForm(obj=workspace)
    
    if form.validate_on_submit():
        workspace.name = form.name.data
        workspace.workspace_id = form.workspace_id.data
        db.session.commit()
        flash("Workspace actualizado", "success")
        return redirect(url_for('workspaces.detail', workspace_id=workspace_id))
    
    return render_template(
        'base_form.html',
        form=form,
        title='Editar Workspace',
        back_url=url_for('workspaces.detail', workspace_id=workspace_id)
    )


@bp.route('/<int:workspace_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(workspace_id):
    """Delete a workspace."""
    workspace = Workspace.query.get_or_404(workspace_id)
    
    # Check if workspace is in use
    config_count = ReportConfig.query.filter_by(workspace_id=workspace_id).count()
    if config_count > 0:
        flash(f"No se puede eliminar el workspace porque está asociado a {config_count} configuraciones", "danger")
        return redirect(url_for('workspaces.detail', workspace_id=workspace_id))
    
    name = workspace.name
    db.session.delete(workspace)
    db.session.commit()
    
    logging.info(f"Workspace deleted: {name} (ID: {workspace_id})")
    flash(f"Workspace '{name}' eliminado", "success")
    return redirect(url_for('workspaces.list'))
