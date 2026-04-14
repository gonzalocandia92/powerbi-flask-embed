"""
Tenant management routes.
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required

from app import db
from app.models import Tenant, Client, Workspace
from app.forms import TenantForm
from app.utils.decorators import retry_on_db_error

bp = Blueprint('tenants', __name__, url_prefix='/tenants')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    tenants = Tenant.query.options(db.joinedload(Tenant.client)).all()
    return render_template('tenants/list.html', tenants=tenants, title='Tenants')


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    form = TenantForm()
    form.client.choices = [(c.id, c.name) for c in Client.query.order_by(Client.name).all()]
    
    if form.validate_on_submit():
        tenant = Tenant(
            name=form.name.data,
            tenant_id=form.tenant_id.data,
            client_id_fk=form.client.data
        )
        db.session.add(tenant)
        db.session.commit()
        flash("Tenant creado", "success")
        return redirect(url_for('tenants.list'))
    
    return render_template('base_form.html', form=form, title='Nuevo Tenant', back_url=url_for('tenants.list'))


@bp.route('/<int:tenant_id>/detail')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(tenant_id):
    tenant = Tenant.query.options(db.joinedload(Tenant.client)).get_or_404(tenant_id)
    workspaces = Workspace.query.filter_by(tenant_id_fk=tenant_id).all()
    return render_template('tenants/detail.html', tenant=tenant, workspaces=workspaces)


@bp.route('/<int:tenant_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    form = TenantForm(obj=tenant)
    form.client.choices = [(c.id, c.name) for c in Client.query.order_by(Client.name).all()]
    
    if request.method == 'GET':
        form.client.data = tenant.client_id_fk
    
    if form.validate_on_submit():
        tenant.name = form.name.data
        tenant.tenant_id = form.tenant_id.data
        tenant.client_id_fk = form.client.data
        db.session.commit()
        flash("Tenant actualizado", "success")
        return redirect(url_for('tenants.detail', tenant_id=tenant_id))
    
    return render_template('base_form.html', form=form, title='Editar Tenant', back_url=url_for('tenants.detail', tenant_id=tenant_id))


@bp.route('/<int:tenant_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    ws_count = Workspace.query.filter_by(tenant_id_fk=tenant_id).count()
    if ws_count > 0:
        flash(f"No se puede eliminar el tenant porque tiene {ws_count} workspaces asociados", "danger")
        return redirect(url_for('tenants.detail', tenant_id=tenant_id))
    name = tenant.name
    db.session.delete(tenant)
    db.session.commit()
    logging.debug(f"Tenant deleted: {name} (ID: {tenant_id})")
    flash(f"Tenant '{name}' eliminado", "success")
    return redirect(url_for('tenants.list'))
