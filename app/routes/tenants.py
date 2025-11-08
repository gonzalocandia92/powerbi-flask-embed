"""
Tenant management routes.
"""
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Tenant
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
        fields=['id', 'name', 'tenant_id']
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
