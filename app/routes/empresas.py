"""
Admin routes for managing empresas (CRUD operations).
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required

from app import db
from app.models import Empresa, Report
from app.forms import EmpresaForm
from app.services.credentials_service import generate_client_id, generate_client_secret, hash_client_secret
from app.utils.decorators import retry_on_db_error

bp = Blueprint('empresas', __name__, url_prefix='/admin/empresas')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    empresas = Empresa.query.order_by(Empresa.nombre).all()
    return render_template('admin/empresas/list.html', empresas=empresas, title='Empresas')


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    form = EmpresaForm()
    if form.validate_on_submit():
        existing = Empresa.query.filter_by(nombre=form.nombre.data).first()
        if existing:
            flash("Ya existe una empresa con ese nombre", "danger")
            return render_template('admin/empresas/form.html', form=form, title='Nueva Empresa', is_new=True)
        
        client_id = generate_client_id()
        client_secret = generate_client_secret()
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
        flash("Empresa creada exitosamente. IMPORTANTE: Guarde estas credenciales.", "success")
        return render_template(
            'admin/empresas/credentials.html',
            empresa=empresa, client_id=client_id, client_secret=client_secret,
            title='Credenciales de la Empresa'
        )
    
    return render_template('admin/empresas/form.html', form=form, title='Nueva Empresa', is_new=True)


@bp.route('/<int:empresa_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    form = EmpresaForm(obj=empresa)
    if form.validate_on_submit():
        existing = Empresa.query.filter(Empresa.nombre == form.nombre.data, Empresa.id != empresa_id).first()
        if existing:
            flash("Ya existe una empresa con ese nombre", "danger")
            return render_template('admin/empresas/form.html', form=form, title='Editar Empresa', empresa=empresa, is_new=False)
        empresa.nombre = form.nombre.data
        empresa.cuit = form.cuit.data
        db.session.commit()
        logging.info(f"Empresa updated: {empresa.nombre} (ID: {empresa.id})")
        flash("Empresa actualizada", "success")
        return redirect(url_for('empresas.list'))
    return render_template('admin/empresas/form.html', form=form, title='Editar Empresa', empresa=empresa, is_new=False)


@bp.route('/<int:empresa_id>/toggle-status', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def toggle_status(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    empresa.estado_activo = not empresa.estado_activo
    db.session.commit()
    status = "activada" if empresa.estado_activo else "desactivada"
    flash(f"Empresa {status}", "success")
    return redirect(url_for('empresas.list'))


@bp.route('/<int:empresa_id>/regenerate-credentials', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def regenerate_credentials(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    new_client_id = generate_client_id()
    new_client_secret = generate_client_secret()
    empresa.client_id = new_client_id
    empresa.client_secret_hash = hash_client_secret(new_client_secret)
    db.session.commit()
    flash("Credenciales regeneradas. IMPORTANTE: Guarde estas credenciales.", "warning")
    return render_template(
        'admin/empresas/credentials.html',
        empresa=empresa, client_id=new_client_id, client_secret=new_client_secret,
        title='Nuevas Credenciales de la Empresa'
    )


@bp.route('/<int:empresa_id>/detail')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(empresa_id):
    empresa = Empresa.query.options(db.joinedload(Empresa.reports)).get_or_404(empresa_id)
    for report in empresa.reports:
        _ = report.workspace
        _ = report.workspace.tenant
    return render_template('admin/empresas/detail.html', empresa=empresa, title=f'Empresa: {empresa.nombre}')


@bp.route('/<int:empresa_id>/reports/manage', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def manage_reports(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    
    from app.models import Workspace, Tenant
    all_reports = Report.query.filter_by(es_privado=True).options(
        db.joinedload(Report.workspace).joinedload(Workspace.tenant)
    ).order_by(Report.name).all()
    
    if request.method == 'POST':
        selected_ids = request.form.getlist('reports')
        selected_ids = [int(rid) for rid in selected_ids if rid]
        empresa.reports = []
        for rid in selected_ids:
            report = db.session.get(Report, rid)
            if report and report.es_privado:
                empresa.reports.append(report)
        db.session.commit()
        flash(f"Reportes asociados actualizados: {len(selected_ids)} reportes", "success")
        return redirect(url_for('empresas.detail', empresa_id=empresa_id))
    
    current_report_ids = [r.id for r in empresa.reports]
    return render_template(
        'admin/empresas/manage_reports.html',
        empresa=empresa, all_reports=all_reports, current_report_ids=current_report_ids,
        title=f'Gestionar Reportes: {empresa.nombre}'
    )


@bp.route('/<int:empresa_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    if empresa.reports:
        flash("No se puede eliminar la empresa porque tiene reportes asociados", "danger")
        return redirect(url_for('empresas.list'))
    nombre = empresa.nombre
    db.session.delete(empresa)
    db.session.commit()
    logging.info(f"Empresa deleted: {nombre} (ID: {empresa_id})")
    flash("Empresa eliminada", "success")
    return redirect(url_for('empresas.list'))
