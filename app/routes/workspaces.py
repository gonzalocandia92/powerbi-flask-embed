"""
Workspace management routes.
"""
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Workspace
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
        fields=['id', 'name', 'workspace_id']
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
