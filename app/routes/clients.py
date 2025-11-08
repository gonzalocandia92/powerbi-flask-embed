"""
Client management routes.
"""
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Client
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
        fields=['id', 'name', 'client_id', 'client_secret']
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
