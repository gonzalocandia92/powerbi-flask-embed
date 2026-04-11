"""
Report management routes.
Handles CRUD for reports, public link management, and URL-based report creation.
"""
import re
import uuid
import logging
from urllib.parse import urlparse, parse_qs
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required

from app import db
from app.models import Report, Workspace, Tenant, UsuarioPBI, PublicLink, Empresa
from app.forms import (
    ReportForm, PublicLinkForm,
    PublicUrlForm, PublicUrlWorkspaceForm, PublicUrlReportForm, PublicUrlLinkForm
)
from app.utils.decorators import retry_on_db_error
from app.utils.powerbi import get_embed_for_report

bp = Blueprint('reports', __name__, url_prefix='/reports')

# Regex to parse Power BI URLs
POWERBI_URL_PATTERN = re.compile(
    r'https?://app\.powerbi\.com/groups/([0-9a-f\-]{36})/reports/([0-9a-f\-]{36})',
    re.IGNORECASE
)


def parse_powerbi_url(url):
    """Parse a Power BI URL and extract workspace_id and report_id GUIDs."""
    url = url.strip()
    match = POWERBI_URL_PATTERN.search(url)
    if not match:
        return None, None
    return match.group(1), match.group(2)


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    """Display list of all reports with related information."""
    reports = Report.query.options(
        db.joinedload(Report.workspace).joinedload(Workspace.tenant),
        db.joinedload(Report.usuario_pbi),
        db.joinedload(Report.empresas)
    ).all()
    
    active_links = PublicLink.query.filter_by(is_active=True).all()
    links_by_report = {}
    for link in active_links:
        if link.report_id_fk not in links_by_report:
            links_by_report[link.report_id_fk] = []
        links_by_report[link.report_id_fk].append(link)
    
    return render_template(
        'reports/list.html',
        reports=reports,
        links_by_report=links_by_report,
        title='Reports'
    )


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    """Create a new report."""
    form = ReportForm()
    form.workspace.choices = [(w.id, f"{w.name} ({w.workspace_id[:8]}...)") for w in Workspace.query.order_by(Workspace.name).all()]
    form.usuario_pbi.choices = [(u.id, u.nombre) for u in UsuarioPBI.query.order_by(UsuarioPBI.nombre).all()]
    form.empresas.choices = [
        (e.id, e.nombre) for e in Empresa.query.filter_by(estado_activo=True).order_by(Empresa.nombre).all()
    ]
    
    if form.validate_on_submit():
        es_publico = form.es_publico.data
        es_privado = form.es_privado.data
        
        if not es_publico and not es_privado:
            flash("El reporte debe ser público, privado, o ambos", "danger")
            return render_template('reports/form.html', form=form, title='Nuevo Report')
        
        report = Report(
            name=form.name.data,
            report_id=form.report_id.data,
            embed_url=form.embed_url.data or None,
            workspace_id_fk=form.workspace.data,
            usuario_pbi_id=form.usuario_pbi.data,
            es_publico=es_publico,
            es_privado=es_privado
        )
        db.session.add(report)
        db.session.commit()
        
        flash("Report creado", "success")
        return redirect(url_for('reports.detail', report_id=report.id))
    
    return render_template('reports/form.html', form=form, title='Nuevo Report')


@bp.route('/<int:report_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(report_id):
    """Edit a report."""
    report = Report.query.options(db.joinedload(Report.empresas)).get_or_404(report_id)
    form = ReportForm(obj=report)
    form.workspace.choices = [(w.id, f"{w.name} ({w.workspace_id[:8]}...)") for w in Workspace.query.order_by(Workspace.name).all()]
    form.usuario_pbi.choices = [(u.id, u.nombre) for u in UsuarioPBI.query.order_by(UsuarioPBI.nombre).all()]
    form.empresas.choices = [
        (e.id, e.nombre) for e in Empresa.query.filter_by(estado_activo=True).order_by(Empresa.nombre).all()
    ]
    all_empresas = Empresa.query.filter_by(estado_activo=True).order_by(Empresa.nombre).all()
    
    if request.method == 'GET':
        form.workspace.data = report.workspace_id_fk
        form.usuario_pbi.data = report.usuario_pbi_id
    
    if request.method == 'POST':
        if not form.validate_on_submit():
            flash("Hay errores en el formulario. Revisá los campos marcados.", "danger")
            return render_template('reports/form.html', form=form, report=report, all_empresas=all_empresas, title='Editar Report')
        
        es_publico = form.es_publico.data
        es_privado = form.es_privado.data
        if not es_publico and not es_privado:
            flash("El reporte debe ser público, privado, o ambos", "danger")
            return render_template('reports/form.html', form=form, report=report, all_empresas=all_empresas, title='Editar Report')
        
        report.name = form.name.data
        report.report_id = form.report_id.data
        report.embed_url = form.embed_url.data or None
        report.workspace_id_fk = form.workspace.data
        report.usuario_pbi_id = form.usuario_pbi.data
        report.es_publico = es_publico
        report.es_privado = es_privado
        
        # Update empresa associations
        selected_empresa_ids = request.form.getlist('empresas')
        selected_empresa_ids = [int(eid) for eid in selected_empresa_ids if eid]
        report.empresas = []
        for empresa_id in selected_empresa_ids:
            empresa = db.session.get(Empresa, empresa_id)
            if empresa:
                report.empresas.append(empresa)
        
        db.session.commit()
        flash("Report actualizado", "success")
        return redirect(url_for('reports.list'))
    
    return render_template('reports/form.html', form=form, report=report, all_empresas=all_empresas, title='Editar Report')


@bp.route('/<int:report_id>/detail')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(report_id):
    """Display detailed information about a report."""
    report = Report.query.options(
        db.joinedload(Report.workspace).joinedload(Workspace.tenant).joinedload(Tenant.client),
        db.joinedload(Report.usuario_pbi),
        db.joinedload(Report.empresas)
    ).get_or_404(report_id)
    
    public_links = PublicLink.query.filter_by(report_id_fk=report_id, is_active=True).all()
    
    return render_template(
        'reports/detail.html',
        report=report,
        public_links=public_links,
        title=f'Report: {report.name}'
    )


@bp.route('/<int:report_id>/view')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def view_report(report_id):
    """View a report in private mode (requires login)."""
    report = Report.query.options(
        db.joinedload(Report.workspace).joinedload(Workspace.tenant).joinedload(Tenant.client),
        db.joinedload(Report.usuario_pbi)
    ).get_or_404(report_id)
    
    try:
        embed_token, embed_url, rid = get_embed_for_report(report)
    except Exception as e:
        logging.error(f"Error generating embed token: {e}")
        flash(f"Error cargando reporte: {e}", "danger")
        return redirect(url_for('reports.list'))
    
    return render_template(
        'report_base.html',
        embed_token=embed_token,
        embed_url=embed_url,
        report_id=rid,
        config_name=report.name,
        is_public=False
    )


@bp.route('/<int:report_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(report_id):
    """Delete a report."""
    report = Report.query.get_or_404(report_id)
    
    active_links = PublicLink.query.filter_by(report_id_fk=report_id, is_active=True).count()
    if active_links > 0:
        flash("No se puede eliminar el report porque tiene links públicos activos. Desactívelos primero.", "danger")
        return redirect(url_for('reports.detail', report_id=report_id))
    
    name = report.name
    db.session.delete(report)
    db.session.commit()
    logging.info(f"Report deleted: {name} (ID: {report_id})")
    flash(f"Report '{name}' eliminado", "success")
    return redirect(url_for('reports.list'))


# --- Public Link Management ---

@bp.route('/<int:report_id>/link/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new_link(report_id):
    """Create a new public link for a report."""
    report = Report.query.get_or_404(report_id)
    form = PublicLinkForm()
    
    if form.validate_on_submit():
        custom_slug = form.custom_slug.data.lower().strip()
        existing = PublicLink.query.filter_by(custom_slug=custom_slug).first()
        if existing:
            flash("Este nombre personalizado ya está en uso. Por favor elige otro.", "danger")
            return render_template('create_public_link.html', form=form, report=report)
        
        token = uuid.uuid4().hex[:16]
        link = PublicLink(
            token=token,
            custom_slug=custom_slug,
            report_id_fk=report.id,
            is_active=True
        )
        db.session.add(link)
        db.session.commit()
        
        base_url = f"https://{request.host}"
        public_url = f"{base_url}/p/{custom_slug}"
        flash(f"Link público creado: {public_url}", "success")
        return redirect(url_for('reports.list'))
    
    return render_template('create_public_link.html', form=form, report=report)


@bp.route('/<int:report_id>/link/<int:link_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit_link(report_id, link_id):
    """Edit a public link."""
    report = Report.query.get_or_404(report_id)
    link = PublicLink.query.get_or_404(link_id)
    if link.report_id_fk != report_id:
        flash("Este link no pertenece a este report", "danger")
        return redirect(url_for('main.index'))
    
    form = PublicLinkForm(obj=link)
    if form.validate_on_submit():
        new_slug = form.custom_slug.data.lower().strip()
        existing = PublicLink.query.filter(
            PublicLink.custom_slug == new_slug,
            PublicLink.id != link_id
        ).first()
        if existing:
            flash("Este nombre personalizado ya está en uso. Por favor elige otro.", "danger")
            return render_template('edit_public_link.html', form=form, report=report, link=link)
        
        link.custom_slug = new_slug
        db.session.commit()
        logging.info(f"Public link edited: {link.custom_slug} (ID: {link.id})")
        flash(f"Link público actualizado: /p/{new_slug}", "success")
        return redirect(url_for('main.index'))
    
    return render_template('edit_public_link.html', form=form, report=report, link=link)


@bp.route('/<int:report_id>/link/<int:link_id>/toggle', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def toggle_link(report_id, link_id):
    """Toggle active status of a public link."""
    link = PublicLink.query.get_or_404(link_id)
    if link.report_id_fk != report_id:
        flash("Este link no pertenece a este report", "danger")
        return redirect(url_for('main.index'))
    link.is_active = not link.is_active
    db.session.commit()
    status = "activado" if link.is_active else "desactivado"
    logging.info(f"Public link {status}: {link.custom_slug} (ID: {link.id})")
    flash(f"Link público {status}: /p/{link.custom_slug}", "success")
    return redirect(url_for('main.index'))


@bp.route('/<int:report_id>/link/<int:link_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete_link(report_id, link_id):
    """Delete a public link permanently."""
    link = PublicLink.query.get_or_404(link_id)
    if link.report_id_fk != report_id:
        flash("Este link no pertenece a este report", "danger")
        return redirect(url_for('main.index'))
    slug = link.custom_slug
    db.session.delete(link)
    db.session.commit()
    logging.info(f"Public link deleted: {slug} (ID: {link_id})")
    flash(f"Link público eliminado: /p/{slug}", "success")
    return redirect(url_for('main.index'))


# --- URL-based Report Creation Wizard ---

@bp.route('/from-url', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def from_url():
    """Step 1: Parse a Power BI URL and begin the creation wizard."""
    form = PublicUrlForm()
    
    if form.validate_on_submit():
        url = form.url.data.strip()
        workspace_guid, report_guid = parse_powerbi_url(url)
        
        if not workspace_guid or not report_guid:
            flash("URL inválida. Debe tener el formato: https://app.powerbi.com/groups/{workspace_id}/reports/{report_id}/...", "danger")
            return render_template('reports/from_url.html', form=form, title='Agregar Report desde URL')
        
        workspace = Workspace.query.filter_by(workspace_id=workspace_guid).first()
        
        if not workspace:
            return redirect(url_for('reports.from_url_workspace', workspace_guid=workspace_guid, report_guid=report_guid))
        
        report = Report.query.filter_by(report_id=report_guid, workspace_id_fk=workspace.id).first()
        
        if not report:
            return redirect(url_for('reports.from_url_report', workspace_id=workspace.id, report_guid=report_guid))
        
        return redirect(url_for('reports.from_url_link', report_id=report.id))
    
    return render_template('reports/from_url.html', form=form, title='Agregar Report desde URL')


@bp.route('/from-url/workspace', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def from_url_workspace():
    """Step 2: Create workspace if it doesn't exist."""
    workspace_guid = request.args.get('workspace_guid', '')
    report_guid = request.args.get('report_guid', '')
    
    if not workspace_guid or not report_guid:
        flash("Parámetros incompletos. Comience el proceso nuevamente.", "danger")
        return redirect(url_for('reports.from_url'))
    
    existing = Workspace.query.filter_by(workspace_id=workspace_guid).first()
    if existing:
        return redirect(url_for('reports.from_url_report', workspace_id=existing.id, report_guid=report_guid))
    
    form = PublicUrlWorkspaceForm()
    form.tenant.choices = [(t.id, f"{t.name} ({t.tenant_id[:8]}...)") for t in Tenant.query.order_by(Tenant.name).all()]
    
    if form.validate_on_submit():
        workspace = Workspace(
            name=form.workspace_name.data,
            workspace_id=workspace_guid,
            tenant_id_fk=form.tenant.data
        )
        db.session.add(workspace)
        db.session.commit()
        flash(f"Workspace '{workspace.name}' creado", "success")
        return redirect(url_for('reports.from_url_report', workspace_id=workspace.id, report_guid=report_guid))
    
    return render_template(
        'reports/from_url_workspace.html',
        form=form,
        workspace_guid=workspace_guid,
        report_guid=report_guid,
        title='Crear Workspace'
    )


@bp.route('/from-url/report', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def from_url_report():
    """Step 3: Create report if it doesn't exist."""
    workspace_id = request.args.get('workspace_id', type=int)
    report_guid = request.args.get('report_guid', '')
    
    if not workspace_id or not report_guid:
        flash("Parámetros incompletos. Comience el proceso nuevamente.", "danger")
        return redirect(url_for('reports.from_url'))
    
    workspace = Workspace.query.get_or_404(workspace_id)
    
    existing = Report.query.filter_by(report_id=report_guid, workspace_id_fk=workspace_id).first()
    if existing:
        return redirect(url_for('reports.from_url_link', report_id=existing.id))
    
    form = PublicUrlReportForm()
    form.usuario_pbi.choices = [(u.id, u.nombre) for u in UsuarioPBI.query.order_by(UsuarioPBI.nombre).all()]
    
    if form.validate_on_submit():
        es_publico = form.es_publico.data
        es_privado = form.es_privado.data
        if not es_publico and not es_privado:
            es_publico = True  # Default to public
        
        report = Report(
            name=form.report_name.data,
            report_id=report_guid,
            workspace_id_fk=workspace_id,
            usuario_pbi_id=form.usuario_pbi.data,
            es_publico=es_publico,
            es_privado=es_privado
        )
        db.session.add(report)
        db.session.commit()
        flash(f"Report '{report.name}' creado", "success")
        return redirect(url_for('reports.from_url_link', report_id=report.id))
    
    return render_template(
        'reports/from_url_report.html',
        form=form,
        workspace=workspace,
        report_guid=report_guid,
        title='Crear Report'
    )


@bp.route('/from-url/link', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def from_url_link():
    """Step 4: Create public link for the report."""
    report_id = request.args.get('report_id', type=int)
    
    if not report_id:
        flash("Parámetros incompletos. Comience el proceso nuevamente.", "danger")
        return redirect(url_for('reports.from_url'))
    
    report = Report.query.options(
        db.joinedload(Report.workspace)
    ).get_or_404(report_id)
    
    form = PublicUrlLinkForm()
    
    if form.validate_on_submit():
        link_name = form.link_name.data.lower().strip()
        
        existing = PublicLink.query.filter_by(custom_slug=link_name).first()
        if existing:
            flash("Este nombre de link ya está en uso. Por favor elige otro.", "danger")
            return render_template('reports/from_url_link.html', form=form, report=report, title='Crear Link Público')
        
        token = uuid.uuid4().hex[:16]
        link = PublicLink(
            token=token,
            custom_slug=link_name,
            report_id_fk=report.id,
            is_active=True
        )
        db.session.add(link)
        db.session.commit()
        
        base_url = f"https://{request.host}"
        public_url = f"{base_url}/p/{link_name}"
        flash(f"Link público creado exitosamente: {public_url}", "success")
        return redirect(url_for('reports.list'))
    
    return render_template('reports/from_url_link.html', form=form, report=report, title='Crear Link Público')
