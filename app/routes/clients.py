"""
Client management routes.
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Client, Tenant
from app.forms import ClientForm
from app.utils.decorators import retry_on_db_error

bp = Blueprint('clients', __name__, url_prefix='/clients')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    clients = Client.query.all()
    return render_template(
        'base_list.html', items=clients, title='Clients',
        model_name='Client', model_name_plural='clients',
        new_url=url_for('clients.new'),
        headers=['#', 'Nombre', 'Client ID', 'Secret'],
        fields=['id', 'name', 'client_id', 'client_secret'],
        has_actions=True, detail_endpoint='clients.detail',
        edit_endpoint='clients.edit', delete_endpoint='clients.delete',
        id_param='client_id'
    )


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    form = ClientForm()
    if form.validate_on_submit():
        client = Client(name=form.name.data, client_id=form.client_id.data)
        if form.client_secret.data:
            client.set_secret(form.client_secret.data)
        db.session.add(client)
        db.session.commit()
        flash("Client creado", "success")
        return redirect(url_for('clients.list'))
    return render_template('base_form.html', form=form, title='Nuevo Client', back_url=url_for('clients.list'))


@bp.route('/<int:client_id>/detail')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(client_id):
    client = Client.query.get_or_404(client_id)
    tenants = Tenant.query.filter_by(client_id_fk=client_id).all()
    return render_template('clients/detail.html', client=client, tenants=tenants)


@bp.route('/<int:client_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(client_id):
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
    return render_template('base_form.html', form=form, title='Editar Client', back_url=url_for('clients.detail', client_id=client_id))


@bp.route('/<int:client_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(client_id):
    client = Client.query.get_or_404(client_id)
    tenant_count = Tenant.query.filter_by(client_id_fk=client_id).count()
    if tenant_count > 0:
        flash(f"No se puede eliminar el client porque tiene {tenant_count} tenants asociados", "danger")
        return redirect(url_for('clients.detail', client_id=client_id))
    name = client.name
    db.session.delete(client)
    db.session.commit()
    logging.info(f"Client deleted: {name} (ID: {client_id})")
    flash(f"Client '{name}' eliminado", "success")
    return redirect(url_for('clients.list'))
