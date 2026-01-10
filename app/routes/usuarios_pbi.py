"""
Power BI user management routes.
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import UsuarioPBI, ReportConfig
from app.forms import UsuarioPBIForm
from app.utils.decorators import retry_on_db_error

bp = Blueprint('usuarios_pbi', __name__, url_prefix='/usuarios-pbi')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    """Display list of all Power BI users."""
    usuarios = UsuarioPBI.query.all()
    
    return render_template(
        'base_list.html',
        items=usuarios,
        title='Usuarios Power BI',
        model_name='Usuario PBI',
        model_name_plural='usuarios PBI',
        new_url=url_for('usuarios_pbi.new'),
        headers=['#', 'Nombre', 'Username'],
        fields=['id', 'nombre', 'username'],
        has_actions=True,
        detail_endpoint='usuarios_pbi.detail',
        edit_endpoint='usuarios_pbi.edit',
        delete_endpoint='usuarios_pbi.delete'
    )


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    """Create a new Power BI user."""
    form = UsuarioPBIForm()
    
    if form.validate_on_submit():
        usuario = UsuarioPBI(
            nombre=form.nombre.data,
            username=form.username.data
        )
        usuario.set_password(form.password.data)
        db.session.add(usuario)
        db.session.commit()
        flash("Usuario PBI creado", "success")
        return redirect(url_for('usuarios_pbi.list'))
    
    return render_template(
        'base_form.html',
        form=form,
        title='Nuevo Usuario PBI',
        back_url=url_for('usuarios_pbi.list')
    )


@bp.route('/<int:usuario_id>/detail')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(usuario_id):
    """Display usuario PBI details."""
    usuario = UsuarioPBI.query.get_or_404(usuario_id)
    configs = ReportConfig.query.filter_by(usuario_pbi_id=usuario_id).all()
    
    return render_template(
        'usuarios_pbi/detail.html',
        usuario=usuario,
        configs=configs
    )


@bp.route('/<int:usuario_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(usuario_id):
    """Edit a usuario PBI."""
    usuario = UsuarioPBI.query.get_or_404(usuario_id)
    form = UsuarioPBIForm(obj=usuario)
    
    if form.validate_on_submit():
        usuario.nombre = form.nombre.data
        usuario.username = form.username.data
        
        if form.password.data:
            usuario.set_password(form.password.data)
        
        db.session.commit()
        flash("Usuario PBI actualizado", "success")
        return redirect(url_for('usuarios_pbi.detail', usuario_id=usuario_id))
    
    return render_template(
        'base_form.html',
        form=form,
        title='Editar Usuario PBI',
        back_url=url_for('usuarios_pbi.detail', usuario_id=usuario_id)
    )


@bp.route('/<int:usuario_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(usuario_id):
    """Delete a usuario PBI."""
    usuario = UsuarioPBI.query.get_or_404(usuario_id)
    
    # Check if usuario is in use
    config_count = ReportConfig.query.filter_by(usuario_pbi_id=usuario_id).count()
    if config_count > 0:
        flash(f"No se puede eliminar el usuario porque está asociado a {config_count} configuraciones", "danger")
        return redirect(url_for('usuarios_pbi.detail', usuario_id=usuario_id))
    
    name = usuario.nombre
    db.session.delete(usuario)
    db.session.commit()
    
    logging.info(f"Usuario PBI deleted: {name} (ID: {usuario_id})")
    flash(f"Usuario PBI '{name}' eliminado", "success")
    return redirect(url_for('usuarios_pbi.list'))
