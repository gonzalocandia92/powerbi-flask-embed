"""
Client management routes.
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Client, ReportConfig
from app.forms import ClientForm
from app.utils.decorators import retry_on_db_error

bp = Blueprint('clients', __name__, url_prefix='/clients')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    """Display list of all clients."""
    clients = Client.query.all()
    
    return render_template(
        'base_list.html',
        items=clients,
        title='Clients',
        model_name='Client',
        model_name_plural='clients',
        new_url=url_for('clients.new'),
        headers=['#', 'Nombre', 'Client ID', 'Secret'],
        fields=['id', 'name', 'client_id', 'client_secret'],
        has_actions=True,
        detail_endpoint='clients.detail',
        edit_endpoint='clients.edit',
        delete_endpoint='clients.delete'
    )


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    """Create a new client."""
    form = ClientForm()
    
    if form.validate_on_submit():
        client = Client(
            name=form.name.data,
            client_id=form.client_id.data
        )
        
        if form.client_secret.data:
            client.set_secret(form.client_secret.data)
        
        db.session.add(client)
        db.session.commit()
        flash("Client creado", "success")
        return redirect(url_for('clients.list'))
    
    return render_template(
        'base_form.html',
        form=form,
        title='Nuevo Client',
        back_url=url_for('clients.list')
    )


@bp.route('/<int:client_id>/detail')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(client_id):
    """Display client details."""
    client = Client.query.get_or_404(client_id)
    configs = ReportConfig.query.filter_by(client_id=client_id).all()
    
    return render_template(
        'clients/detail.html',
        client=client,
        configs=configs
    )


@bp.route('/<int:client_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(client_id):
    """Edit a client."""
    client = Client.query.get_or_404(client_id)
    form = ClientForm(obj=client)
    
    if form.validate_on_submit():
        client.name = form.name.data
        client.client_id = form.client_id.data
        
        if form.client_secret.data:
            client.set_secret(form.client_secret.data)
        
        db.session.commit()
        flash("Client actualizado", "success")
        return redirect(url_for('clients.detail', client_id=client_id))
    
    return render_template(
        'base_form.html',
        form=form,
        title='Editar Client',
        back_url=url_for('clients.detail', client_id=client_id)
    )


@bp.route('/<int:client_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(client_id):
    """Delete a client."""
    client = Client.query.get_or_404(client_id)
    
    # Check if client is in use
    config_count = ReportConfig.query.filter_by(client_id=client_id).count()
    if config_count > 0:
        flash(f"No se puede eliminar el client porque está asociado a {config_count} configuraciones", "danger")
        return redirect(url_for('clients.detail', client_id=client_id))
    
    name = client.name
    db.session.delete(client)
    db.session.commit()
    
    logging.info(f"Client deleted: {name} (ID: {client_id})")
    flash(f"Client '{name}' eliminado", "success")
    return redirect(url_for('clients.list'))
