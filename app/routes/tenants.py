"""
Tenant management routes.
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Tenant, ReportConfig
from app.forms import TenantForm
from app.utils.decorators import retry_on_db_error

bp = Blueprint('tenants', __name__, url_prefix='/tenants')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    """Display list of all tenants."""
    tenants = Tenant.query.all()
    
    return render_template(
        'base_list.html',
        items=tenants,
        title='Tenants',
        model_name='Tenant',
        model_name_plural='tenants',
        new_url=url_for('tenants.new'),
        headers=['#', 'Nombre', 'Tenant ID'],
        fields=['id', 'name', 'tenant_id'],
        has_actions=True,
        detail_endpoint='tenants.detail',
        edit_endpoint='tenants.edit',
        delete_endpoint='tenants.delete'
    )


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    """Create a new tenant."""
    form = TenantForm()
    
    if form.validate_on_submit():
        tenant = Tenant(
            name=form.name.data,
            tenant_id=form.tenant_id.data
        )
        db.session.add(tenant)
        db.session.commit()
        flash("Tenant creado", "success")
        return redirect(url_for('tenants.list'))
    
    return render_template(
        'base_form.html',
        form=form,
        title='Nuevo Tenant',
        back_url=url_for('tenants.list')
    )


@bp.route('/<int:tenant_id>/detail')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(tenant_id):
    """Display tenant details."""
    tenant = Tenant.query.get_or_404(tenant_id)
    configs = ReportConfig.query.filter_by(tenant_id=tenant_id).all()
    
    return render_template(
        'tenants/detail.html',
        tenant=tenant,
        configs=configs
    )


@bp.route('/<int:tenant_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(tenant_id):
    """Edit a tenant."""
    tenant = Tenant.query.get_or_404(tenant_id)
    form = TenantForm(obj=tenant)
    
    if form.validate_on_submit():
        tenant.name = form.name.data
        tenant.tenant_id = form.tenant_id.data
        db.session.commit()
        flash("Tenant actualizado", "success")
        return redirect(url_for('tenants.detail', tenant_id=tenant_id))
    
    return render_template(
        'base_form.html',
        form=form,
        title='Editar Tenant',
        back_url=url_for('tenants.detail', tenant_id=tenant_id)
    )


@bp.route('/<int:tenant_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(tenant_id):
    """Delete a tenant."""
    tenant = Tenant.query.get_or_404(tenant_id)
    
    # Check if tenant is in use
    config_count = ReportConfig.query.filter_by(tenant_id=tenant_id).count()
    if config_count > 0:
        flash(f"No se puede eliminar el tenant porque está asociado a {config_count} configuraciones", "danger")
        return redirect(url_for('tenants.detail', tenant_id=tenant_id))
    
    name = tenant.name
    db.session.delete(tenant)
    db.session.commit()
    
    logging.info(f"Tenant deleted: {name} (ID: {tenant_id})")
    flash(f"Tenant '{name}' eliminado", "success")
    return redirect(url_for('tenants.list'))
