"""
Admin routes for managing empresas (CRUD operations).
Empresas are companies that can access private reports via API.
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required

from app import db
from app.models import Empresa, ReportConfig
from app.forms import EmpresaForm
from app.services.credentials_service import generate_client_id, generate_client_secret, hash_client_secret
from app.utils.decorators import retry_on_db_error

bp = Blueprint('empresas', __name__, url_prefix='/admin/empresas')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    """Display list of all empresas."""
    empresas = Empresa.query.order_by(Empresa.nombre).all()
    
    return render_template(
        'admin/empresas/list.html',
        empresas=empresas,
        title='Empresas'
    )


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    """Create a new empresa."""
    form = EmpresaForm()
    
    if form.validate_on_submit():
        # Check if nombre is unique
        existing = Empresa.query.filter_by(nombre=form.nombre.data).first()
        if existing:
            flash("Ya existe una empresa con ese nombre", "danger")
            return render_template('admin/empresas/form.html', form=form, title='Nueva Empresa', is_new=True)
        
        # Generate credentials
        client_id = generate_client_id()
        client_secret = generate_client_secret()
        
        # Create new empresa
        empresa = Empresa(
            nombre=form.nombre.data,
            cuit=form.cuit.data,
            client_id=client_id,
            client_secret_hash=hash_client_secret(client_secret),
            estado_activo=True
        )
        
        db.session.add(empresa)
        db.session.commit()
        
        logging.info(f"Empresa created: {empresa.nombre} (ID: {empresa.id})")
        
        # Show credentials to user (only time they'll see the secret)
        flash(f"Empresa creada exitosamente. IMPORTANTE: Guarde estas credenciales, no se mostrarán nuevamente.", "success")
        
        return render_template(
            'admin/empresas/credentials.html',
            empresa=empresa,
            client_id=client_id,
            client_secret=client_secret,
            title='Credenciales de la Empresa'
        )
    
    return render_template(
        'admin/empresas/form.html',
        form=form,
        title='Nueva Empresa',
        is_new=True
    )


@bp.route('/<int:empresa_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(empresa_id):
    """Edit an empresa."""
    empresa = Empresa.query.get_or_404(empresa_id)
    form = EmpresaForm(obj=empresa)
    
    if form.validate_on_submit():
        # Check if nombre is unique (excluding current empresa)
        existing = Empresa.query.filter(
            Empresa.nombre == form.nombre.data,
            Empresa.id != empresa_id
        ).first()
        
        if existing:
            flash("Ya existe una empresa con ese nombre", "danger")
            return render_template(
                'admin/empresas/form.html',
                form=form,
                title='Editar Empresa',
                empresa=empresa,
                is_new=False
            )
        
        empresa.nombre = form.nombre.data
        empresa.cuit = form.cuit.data
        db.session.commit()
        
        logging.info(f"Empresa updated: {empresa.nombre} (ID: {empresa.id})")
        flash("Empresa actualizada", "success")
        return redirect(url_for('empresas.list'))
    
    return render_template(
        'admin/empresas/form.html',
        form=form,
        title='Editar Empresa',
        empresa=empresa,
        is_new=False
    )


@bp.route('/<int:empresa_id>/toggle-status', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def toggle_status(empresa_id):
    """Toggle active status of an empresa."""
    empresa = Empresa.query.get_or_404(empresa_id)
    
    empresa.estado_activo = not empresa.estado_activo
    db.session.commit()
    
    status = "activada" if empresa.estado_activo else "desactivada"
    logging.info(f"Empresa {status}: {empresa.nombre} (ID: {empresa.id})")
    flash(f"Empresa {status}", "success")
    
    return redirect(url_for('empresas.list'))


@bp.route('/<int:empresa_id>/regenerate-credentials', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def regenerate_credentials(empresa_id):
    """Regenerate credentials for an empresa."""
    empresa = Empresa.query.get_or_404(empresa_id)
    
    # Generate new credentials
    new_client_id = generate_client_id()
    new_client_secret = generate_client_secret()
    
    empresa.client_id = new_client_id
    empresa.client_secret_hash = hash_client_secret(new_client_secret)
    db.session.commit()
    
    logging.info(f"Empresa credentials regenerated: {empresa.nombre} (ID: {empresa.id})")
    flash("Credenciales regeneradas. IMPORTANTE: Guarde estas credenciales, no se mostrarán nuevamente.", "warning")
    
    return render_template(
        'admin/empresas/credentials.html',
        empresa=empresa,
        client_id=new_client_id,
        client_secret=new_client_secret,
        title='Nuevas Credenciales de la Empresa'
    )


@bp.route('/<int:empresa_id>/detail')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(empresa_id):
    """Display detailed information about an empresa including associated reports."""
    empresa = Empresa.query.options(
        db.joinedload(Empresa.report_configs)
    ).get_or_404(empresa_id)
    
    # Manually load related objects for each report config
    for config in empresa.report_configs:
        _ = config.report
        _ = config.tenant
        _ = config.workspace
    
    return render_template(
        'admin/empresas/detail.html',
        empresa=empresa,
        title=f'Empresa: {empresa.nombre}'
    )


@bp.route('/<int:empresa_id>/reports/manage', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def manage_reports(empresa_id):
    """Manage report associations for an empresa."""
    empresa = Empresa.query.get_or_404(empresa_id)
    
    # Get all private configs
    all_configs = ReportConfig.query.filter_by(es_privado=True).options(
        db.joinedload(ReportConfig.report),
        db.joinedload(ReportConfig.tenant),
        db.joinedload(ReportConfig.workspace)
    ).order_by(ReportConfig.name).all()
    
    if request.method == 'POST':
        # Get selected config IDs from form
        selected_config_ids = request.form.getlist('configs')
        selected_config_ids = [int(id) for id in selected_config_ids if id]
        
        # Clear existing associations and add new ones
        empresa.report_configs = []
        for config_id in selected_config_ids:
            config = ReportConfig.query.get(config_id)
            if config and config.es_privado:
                empresa.report_configs.append(config)
        
        db.session.commit()
        
        logging.info(f"Report associations updated for empresa {empresa.nombre}: {len(selected_config_ids)} configs")
        flash(f"Reportes asociados actualizados: {len(selected_config_ids)} configuraciones", "success")
        return redirect(url_for('empresas.detail', empresa_id=empresa_id))
    
    # Get currently associated config IDs
    current_config_ids = [c.id for c in empresa.report_configs]
    
    return render_template(
        'admin/empresas/manage_reports.html',
        empresa=empresa,
        all_configs=all_configs,
        current_config_ids=current_config_ids,
        title=f'Gestionar Reportes: {empresa.nombre}'
    )


@bp.route('/<int:empresa_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(empresa_id):
    """Delete an empresa."""
    empresa = Empresa.query.get_or_404(empresa_id)
    
    # Check if empresa has associated report configs
    if empresa.report_configs:
        flash("No se puede eliminar la empresa porque tiene configuraciones de reportes asociadas", "danger")
        return redirect(url_for('empresas.list'))
    
    nombre = empresa.nombre
    db.session.delete(empresa)
    db.session.commit()
    
    logging.info(f"Empresa deleted: {nombre} (ID: {empresa_id})")
    flash("Empresa eliminada", "success")
    
    return redirect(url_for('empresas.list'))
