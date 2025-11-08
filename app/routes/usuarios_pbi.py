"""
Power BI user management routes.
"""
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import UsuarioPBI
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
        fields=['id', 'nombre', 'username']
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
