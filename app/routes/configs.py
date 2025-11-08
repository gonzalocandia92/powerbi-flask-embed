"""
Report configuration management routes.
"""
import uuid
import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required

from app import db
from app.models import ReportConfig, Tenant, Client, Workspace, Report, UsuarioPBI, PublicLink
from app.forms import ReportConfigForm, PublicLinkForm
from app.utils.decorators import retry_on_db_error
from app.utils.powerbi import get_embed_for_config

bp = Blueprint('configs', __name__, url_prefix='/configs')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    """Display list of all report configurations."""
    configs = ReportConfig.query.options(
        db.joinedload(ReportConfig.tenant),
        db.joinedload(ReportConfig.client),
        db.joinedload(ReportConfig.workspace),
        db.joinedload(ReportConfig.report),
        db.joinedload(ReportConfig.usuario_pbi)
    ).all()
    
    return render_template(
        'base_list.html',
        items=configs,
        title='Configuraciones',
        model_name='Configuración',
        model_name_plural='configuraciones',
        new_url=url_for('configs.new'),
        headers=['#', 'Nombre', 'Tenant', 'Client', 'Workspace', 'Report', 'Usuario PBI'],
        fields=['id', 'name', 'tenant.name', 'client.name', 'workspace.name', 'report.name', 'usuario_pbi.nombre']
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
        config = ReportConfig(
            name=form.name.data,
            tenant_id=form.tenant.data,
            client_id=form.client.data,
            workspace_id=form.workspace.data,
            report_id_fk=form.report.data,
            usuario_pbi_id=form.usuario_pbi.data
        )
        db.session.add(config)
        db.session.commit()
        flash("Configuración creada", "success")
        return redirect(url_for('configs.list'))
    
    return render_template(
        'base_form.html',
        form=form,
        title='Nueva Configuración',
        back_url=url_for('configs.list')
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
