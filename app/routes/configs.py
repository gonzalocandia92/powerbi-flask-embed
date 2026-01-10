"""
Report configuration management routes.
"""
import uuid
import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required

from app import db
from app.models import ReportConfig, Tenant, Client, Workspace, Report, UsuarioPBI, PublicLink, Empresa
from app.forms import ReportConfigForm, PublicLinkForm
from app.utils.decorators import retry_on_db_error
from app.utils.powerbi import get_embed_for_config

bp = Blueprint('configs', __name__, url_prefix='/configs')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    """Display list of all report configurations with full details."""
    configs = ReportConfig.query.options(
        db.joinedload(ReportConfig.tenant),
        db.joinedload(ReportConfig.client),
        db.joinedload(ReportConfig.workspace),
        db.joinedload(ReportConfig.report),
        db.joinedload(ReportConfig.usuario_pbi),
        db.joinedload(ReportConfig.empresas)
    ).all()
    
    # Get public links for each config
    public_links = PublicLink.query.filter_by(is_active=True).all()
    links_by_config = {}
    for link in public_links:
        if link.report_config_id not in links_by_config:
            links_by_config[link.report_config_id] = []
        links_by_config[link.report_config_id].append(link)
    
    return render_template(
        'configs/list.html',
        configs=configs,
        links_by_config=links_by_config,
        title='Configuraciones'
    )


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    """Create a new report configuration."""
    form = ReportConfigForm()
    
    form.tenant.choices = [(t.id, t.name) for t in Tenant.query.order_by(Tenant.name).all()]
    form.client.choices = [(c.id, c.name) for c in Client.query.order_by(Client.name).all()]
    form.workspace.choices = [(w.id, w.name) for w in Workspace.query.order_by(Workspace.name).all()]
    form.report.choices = [(r.id, r.name) for r in Report.query.order_by(Report.name).all()]
    form.usuario_pbi.choices = [(u.id, u.nombre) for u in UsuarioPBI.query.order_by(UsuarioPBI.nombre).all()]
    
    if form.validate_on_submit():
        es_publico = form.es_publico.data
        es_privado = form.es_privado.data
        
        # Validation: at least one must be selected
        if not es_publico and not es_privado:
            flash("El reporte debe ser público, privado, o ambos", "danger")
            return render_template(
                'configs/form.html',
                form=form,
                title='Nueva Configuración'
            )
        
        config = ReportConfig(
            name=form.name.data,
            tenant_id=form.tenant.data,
            client_id=form.client.data,
            workspace_id=form.workspace.data,
            report_id_fk=form.report.data,
            usuario_pbi_id=form.usuario_pbi.data,
            es_publico=es_publico,
            es_privado=es_privado
        )
        db.session.add(config)
        db.session.flush()  # Get config ID
        
        flash("Configuración creada", "success")
        return redirect(url_for('configs.edit', config_id=config.id))
    
    return render_template(
        'configs/form.html',
        form=form,
        title='Nueva Configuración'
    )


@bp.route('/<int:config_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(config_id):
    """Edit a report configuration."""
    config = ReportConfig.query.options(
        db.joinedload(ReportConfig.empresas)
    ).get_or_404(config_id)
    
    form = ReportConfigForm(obj=config)
    
    form.tenant.choices = [(t.id, t.name) for t in Tenant.query.order_by(Tenant.name).all()]
    form.client.choices = [(c.id, c.name) for c in Client.query.order_by(Client.name).all()]
    form.workspace.choices = [(w.id, w.name) for w in Workspace.query.order_by(Workspace.name).all()]
    form.report.choices = [(r.id, r.name) for r in Report.query.order_by(Report.name).all()]
    form.usuario_pbi.choices = [(u.id, u.nombre) for u in UsuarioPBI.query.order_by(UsuarioPBI.nombre).all()]
    form.empresas.choices = [
        (e.id, e.nombre)
        for e in Empresa.query.filter_by(estado_activo=True).order_by(Empresa.nombre).all()
    ]
    # Get all active empresas
    all_empresas = Empresa.query.filter_by(estado_activo=True).order_by(Empresa.nombre).all()
    
    if request.method == 'POST':
        if not form.validate_on_submit():
            flash("Hay errores en el formulario. Revisá los campos marcados.", "danger")
            return render_template(
                'configs/form.html',
                form=form,
                config=config,
                all_empresas=all_empresas,
                title='Editar Configuración'
            )
        if form.validate_on_submit():
            es_publico = form.es_publico.data
            es_privado = form.es_privado.data
            
            # Validation: at least one must be selected
            if not es_publico and not es_privado:
                flash("El reporte debe ser público, privado, o ambos", "danger")
                return render_template(
                    'configs/form.html',
                    form=form,
                    config=config,
                    all_empresas=all_empresas,
                    title='Editar Configuración'
                )
            
            config.name = form.name.data
            config.tenant_id = form.tenant.data
            config.client_id = form.client.data
            config.workspace_id = form.workspace.data
            config.report_id_fk = form.report.data
            config.usuario_pbi_id = form.usuario_pbi.data
            config.es_publico = es_publico
            config.es_privado = es_privado
            
            # Update empresa associations
            selected_empresa_ids = request.form.getlist('empresas')
            selected_empresa_ids = [int(id) for id in selected_empresa_ids if id]
            
            # Clear existing associations and add new ones
            config.empresas = []
            for empresa_id in selected_empresa_ids:
                empresa = Empresa.query.get(empresa_id)
                if empresa:
                    config.empresas.append(empresa)
            
            db.session.commit()
            flash("Configuración actualizada", "success")
            return redirect(url_for('configs.list'))
    
    return render_template(
        'configs/form.html',
        form=form,
        config=config,
        all_empresas=all_empresas,
        title='Editar Configuración'
    )


@bp.route('/<int:config_id>/view')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def view(config_id):
    """View a report in private mode (requires login)."""
    config = ReportConfig.query.get_or_404(config_id)
    
    try:
        embed_token, embed_url, report_id = get_embed_for_config(config)
    except Exception as e:
        logging.error(f"Error generating embed token: {e}")
        flash(f"Error cargando reporte: {e}", "danger")
        return redirect(url_for('configs.list'))
    
    return render_template(
        'report_base.html',
        embed_token=embed_token,
        embed_url=embed_url,
        report_id=report_id,
        config_name=config.name,
        is_public=False
    )


@bp.route('/<int:config_id>/detail')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(config_id):
    """Display detailed information about a report configuration."""
    config = ReportConfig.query.options(
        db.joinedload(ReportConfig.tenant),
        db.joinedload(ReportConfig.client),
        db.joinedload(ReportConfig.workspace),
        db.joinedload(ReportConfig.report),
        db.joinedload(ReportConfig.usuario_pbi),
        db.joinedload(ReportConfig.empresas)
    ).get_or_404(config_id)
    
    # Get public links for this config
    public_links = PublicLink.query.filter_by(
        report_config_id=config_id,
        is_active=True
    ).all()
    
    return render_template(
        'configs/detail.html',
        config=config,
        public_links=public_links,
        title=f'Configuración: {config.name}'
    )


@bp.route('/<int:config_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(config_id):
    """Delete a report configuration."""
    config = ReportConfig.query.get_or_404(config_id)
    
    # Check if config has active public links
    active_links = PublicLink.query.filter_by(
        report_config_id=config_id,
        is_active=True
    ).count()
    
    if active_links > 0:
        flash("No se puede eliminar la configuración porque tiene links públicos activos. Desactívelos primero.", "danger")
        return redirect(url_for('configs.detail', config_id=config_id))
    
    name = config.name
    db.session.delete(config)
    db.session.commit()
    
    logging.info(f"Config deleted: {name} (ID: {config_id})")
    flash("Configuración eliminada exitosamente", "success")
    
    return redirect(url_for('configs.list'))


@bp.route('/<int:config_id>/link/<int:link_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit_link(config_id, link_id):
    """Edit a public link."""
    config = ReportConfig.query.get_or_404(config_id)
    link = PublicLink.query.get_or_404(link_id)
    
    # Verify link belongs to config
    if link.report_config_id != config_id:
        flash("Este link no pertenece a esta configuración", "danger")
        return redirect(url_for('main.index'))
    
    form = PublicLinkForm(obj=link)
    
    if form.validate_on_submit():
        new_slug = form.custom_slug.data.lower().strip()
        
        # Check if slug is unique (excluding current link)
        existing_link = PublicLink.query.filter(
            PublicLink.custom_slug == new_slug,
            PublicLink.id != link_id
        ).first()
        
        if existing_link:
            flash("Este nombre personalizado ya está en uso. Por favor elige otro.", "danger")
            return render_template('edit_public_link.html', form=form, config=config, link=link)
        
        link.custom_slug = new_slug
        db.session.commit()
        
        logging.info(f"Public link edited: {link.custom_slug} (ID: {link.id})")
        flash(f"Link público actualizado: /p/{new_slug}", "success")
        return redirect(url_for('main.index'))
    
    return render_template('edit_public_link.html', form=form, config=config, link=link)


@bp.route('/<int:config_id>/link/<int:link_id>/toggle', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def toggle_link(config_id, link_id):
    """Toggle active status of a public link (soft delete)."""
    link = PublicLink.query.get_or_404(link_id)
    
    # Verify link belongs to config
    if link.report_config_id != config_id:
        flash("Este link no pertenece a esta configuración", "danger")
        return redirect(url_for('main.index'))
    
    link.is_active = not link.is_active
    db.session.commit()
    
    status = "activado" if link.is_active else "desactivado"
    logging.info(f"Public link {status}: {link.custom_slug} (ID: {link.id})")
    flash(f"Link público {status}: /p/{link.custom_slug}", "success")
    
    return redirect(url_for('main.index'))


@bp.route('/<int:config_id>/link/<int:link_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete_link(config_id, link_id):
    """Delete a public link permanently."""
    link = PublicLink.query.get_or_404(link_id)
    
    # Verify link belongs to config
    if link.report_config_id != config_id:
        flash("Este link no pertenece a esta configuración", "danger")
        return redirect(url_for('main.index'))
    
    slug = link.custom_slug
    db.session.delete(link)
    db.session.commit()
    
    logging.info(f"Public link deleted: {slug} (ID: {link_id})")
    flash(f"Link público eliminado: /p/{slug}", "success")
    
    return redirect(url_for('main.index'))


@bp.route('/<int:config_id>/link/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new_link(config_id):
    """Create a new public link for a report configuration."""
    config = ReportConfig.query.get_or_404(config_id)
    form = PublicLinkForm()
    
    if form.validate_on_submit():
        custom_slug = form.custom_slug.data.lower().strip()
        
        existing_link = PublicLink.query.filter_by(custom_slug=custom_slug).first()
        if existing_link:
            flash("Este nombre personalizado ya está en uso. Por favor elige otro.", "danger")
            return render_template('create_public_link.html', form=form, config=config)
        
        token = uuid.uuid4().hex[:16]
        
        link = PublicLink(
            token=token,
            custom_slug=custom_slug,
            report_config_id=config.id,
            is_active=True
        )
        db.session.add(link)
        db.session.commit()
        
        base_url = f"https://{request.host}"
        public_url = f"{base_url}/p/{custom_slug}"
        
        flash(f"Link público creado: {public_url}", "success")
        return redirect(url_for('configs.list'))
    
    return render_template('create_public_link.html', form=form, config=config)
