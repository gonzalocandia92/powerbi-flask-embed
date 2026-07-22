"""
Admin routes for managing empresas (CRUD operations).
"""
import logging
import re
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required

from app import db
from app.models import Empresa, Report, WhatsAppAuthorizedNumber
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
        
        logging.debug(f"Empresa created: {empresa.nombre} (ID: {empresa.id})")
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
        logging.debug(f"Empresa updated: {empresa.nombre} (ID: {empresa.id})")
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
    empresa = Empresa.query.options(
        db.joinedload(Empresa.reports),
        db.joinedload(Empresa.whatsapp_authorized_numbers).joinedload(WhatsAppAuthorizedNumber.report),
    ).get_or_404(empresa_id)
    for report in empresa.reports:
        _ = report.workspace
        _ = report.workspace.tenant
    return render_template('admin/empresas/detail.html', empresa=empresa, title=f'Empresa: {empresa.nombre}')


@bp.route('/<int:empresa_id>/toggle-whatsapp', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def toggle_whatsapp(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    empresa.whatsapp_enabled = not empresa.whatsapp_enabled
    db.session.commit()
    status = "habilitado" if empresa.whatsapp_enabled else "deshabilitado"
    flash(f"Chat de WhatsApp {status} para {empresa.nombre}", "success")
    return redirect(url_for('empresas.detail', empresa_id=empresa_id))


@bp.route('/<int:empresa_id>/whatsapp/add', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def add_whatsapp_number(empresa_id):
    empresa = Empresa.query.options(db.joinedload(Empresa.reports)).get_or_404(empresa_id)
    chatbot_reports = [r for r in empresa.reports if r.chatbot_enabled]

    if request.method == 'POST':
        phone_number = (request.form.get('phone_number') or '').strip()
        phone_number = re.sub(r'[^0-9]', '', phone_number)
        selected_ids = [int(rid) for rid in request.form.getlist('reports') if rid]

        if not phone_number:
            flash("Ingrese un numero de telefono valido", "danger")
        elif not selected_ids:
            flash("Seleccione al menos un reporte", "danger")
        else:
            chatbot_report_ids = {r.id for r in chatbot_reports}
            added = 0
            for report_id in selected_ids:
                if report_id not in chatbot_report_ids:
                    continue
                exists = WhatsAppAuthorizedNumber.query.filter_by(
                    phone_number=phone_number, report_id_fk=report_id
                ).first()
                if exists:
                    continue
                db.session.add(WhatsAppAuthorizedNumber(
                    phone_number=phone_number, empresa_id_fk=empresa.id, report_id_fk=report_id
                ))
                added += 1
            db.session.commit()
            flash(f"Numero {phone_number} autorizado para {added} reporte(s)", "success")
            return redirect(url_for('empresas.detail', empresa_id=empresa_id))

    return render_template(
        'admin/empresas/whatsapp_add_number.html',
        empresa=empresa, chatbot_reports=chatbot_reports, title=f'Autorizar Numero: {empresa.nombre}'
    )


@bp.route('/<int:empresa_id>/whatsapp/<int:number_id>/remove', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def remove_whatsapp_number(empresa_id, number_id):
    entry = WhatsAppAuthorizedNumber.query.filter_by(id=number_id, empresa_id_fk=empresa_id).first_or_404()
    db.session.delete(entry)
    db.session.commit()
    flash("Acceso de WhatsApp eliminado", "success")
    return redirect(url_for('empresas.detail', empresa_id=empresa_id))


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
    logging.debug(f"Empresa deleted: {nombre} (ID: {empresa_id})")
    flash("Empresa eliminada", "success")
    return redirect(url_for('empresas.list'))
