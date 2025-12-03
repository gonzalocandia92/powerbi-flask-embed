"""
Admin routes for managing private clients (CRUD operations).
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required

from app import db
from app.models import ClientePrivado
from app.forms import ClientePrivadoForm
from app.services.credentials_service import generate_client_id, generate_client_secret, hash_client_secret
from app.utils.decorators import retry_on_db_error

bp = Blueprint('admin_clientes_privados', __name__, url_prefix='/admin/clientes-privados')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    """Display list of all private clients."""
    clientes = ClientePrivado.query.order_by(ClientePrivado.nombre).all()
    
    return render_template(
        'admin/clientes_privados/list.html',
        clientes=clientes,
        title='Clientes Privados'
    )


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    """Create a new private client."""
    form = ClientePrivadoForm()
    
    if form.validate_on_submit():
        # Check if nombre is unique
        existing = ClientePrivado.query.filter_by(nombre=form.nombre.data).first()
        if existing:
            flash("Ya existe un cliente con ese nombre", "danger")
            return render_template('admin/clientes_privados/form.html', form=form, title='Nuevo Cliente Privado', is_new=True)
        
        # Generate credentials
        client_id = generate_client_id()
        client_secret = generate_client_secret()
        
        # Create new client
        cliente = ClientePrivado(
            nombre=form.nombre.data,
            client_id=client_id,
            client_secret_hash=hash_client_secret(client_secret),
            estado_activo=True
        )
        
        db.session.add(cliente)
        db.session.commit()
        
        logging.info(f"Private client created: {cliente.nombre} (ID: {cliente.id})")
        
        # Show credentials to user (only time they'll see the secret)
        flash(f"Cliente creado exitosamente. IMPORTANTE: Guarde estas credenciales, no se mostrarán nuevamente.", "success")
        
        return render_template(
            'admin/clientes_privados/credentials.html',
            cliente=cliente,
            client_id=client_id,
            client_secret=client_secret,
            title='Credenciales del Cliente'
        )
    
    return render_template(
        'admin/clientes_privados/form.html',
        form=form,
        title='Nuevo Cliente Privado',
        is_new=True
    )


@bp.route('/<int:cliente_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(cliente_id):
    """Edit a private client."""
    cliente = ClientePrivado.query.get_or_404(cliente_id)
    form = ClientePrivadoForm(obj=cliente)
    
    if form.validate_on_submit():
        # Check if nombre is unique (excluding current client)
        existing = ClientePrivado.query.filter(
            ClientePrivado.nombre == form.nombre.data,
            ClientePrivado.id != cliente_id
        ).first()
        
        if existing:
            flash("Ya existe un cliente con ese nombre", "danger")
            return render_template(
                'admin/clientes_privados/form.html',
                form=form,
                title='Editar Cliente Privado',
                cliente=cliente,
                is_new=False
            )
        
        cliente.nombre = form.nombre.data
        db.session.commit()
        
        logging.info(f"Private client updated: {cliente.nombre} (ID: {cliente.id})")
        flash("Cliente actualizado", "success")
        return redirect(url_for('admin_clientes_privados.list'))
    
    return render_template(
        'admin/clientes_privados/form.html',
        form=form,
        title='Editar Cliente Privado',
        cliente=cliente,
        is_new=False
    )


@bp.route('/<int:cliente_id>/toggle-status', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def toggle_status(cliente_id):
    """Toggle active status of a private client."""
    cliente = ClientePrivado.query.get_or_404(cliente_id)
    
    cliente.estado_activo = not cliente.estado_activo
    db.session.commit()
    
    status = "activado" if cliente.estado_activo else "desactivado"
    logging.info(f"Private client {status}: {cliente.nombre} (ID: {cliente.id})")
    flash(f"Cliente {status}", "success")
    
    return redirect(url_for('admin_clientes_privados.list'))


@bp.route('/<int:cliente_id>/regenerate-credentials', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def regenerate_credentials(cliente_id):
    """Regenerate credentials for a private client."""
    cliente = ClientePrivado.query.get_or_404(cliente_id)
    
    # Generate new credentials
    new_client_id = generate_client_id()
    new_client_secret = generate_client_secret()
    
    cliente.client_id = new_client_id
    cliente.client_secret_hash = hash_client_secret(new_client_secret)
    db.session.commit()
    
    logging.info(f"Private client credentials regenerated: {cliente.nombre} (ID: {cliente.id})")
    flash("Credenciales regeneradas. IMPORTANTE: Guarde estas credenciales, no se mostrarán nuevamente.", "warning")
    
    return render_template(
        'admin/clientes_privados/credentials.html',
        cliente=cliente,
        client_id=new_client_id,
        client_secret=new_client_secret,
        title='Nuevas Credenciales del Cliente'
    )


@bp.route('/<int:cliente_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(cliente_id):
    """Delete a private client."""
    cliente = ClientePrivado.query.get_or_404(cliente_id)
    
    # Check if client has associated report configs
    if cliente.report_configs:
        flash("No se puede eliminar el cliente porque tiene configuraciones de reportes asociadas", "danger")
        return redirect(url_for('admin_clientes_privados.list'))
    
    nombre = cliente.nombre
    db.session.delete(cliente)
    db.session.commit()
    
    logging.info(f"Private client deleted: {nombre} (ID: {cliente_id})")
    flash("Cliente eliminado", "success")
    
    return redirect(url_for('admin_clientes_privados.list'))
