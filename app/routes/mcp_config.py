"""Administration routes for MCP agent configurations."""
import logging

import requests
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy.orm import joinedload

from app import db
from app.forms import McpConfigCreateForm, McpConfigEditForm
from app.models import (
    Empresa, McpAgentConfig, McpConfigEmpresa, McpModelGrant, Report, Workspace,
)
from app.utils.decorators import permission_required, retry_on_db_error
from app.utils.powerbi import get_mcp_metadata_for_report, hash_mcp_api_key, parse_powerbi_url


bp = Blueprint('mcp_config', __name__, url_prefix='/admin/mcp-config')
LOG = logging.getLogger(__name__)


def _empresa_choices():
    empresas = Empresa.query.order_by(Empresa.nombre).all()
    return [(0, "Sin empresa")] + [(empresa.id, empresa.nombre) for empresa in empresas]


def _selected_empresa_id(value):
    return value or None


def _ensure_company_link(config, empresa_id):
    if not empresa_id:
        return
    if not McpConfigEmpresa.query.filter_by(config_id=config.id, empresa_id=empresa_id).first():
        db.session.add(McpConfigEmpresa(config_id=config.id, empresa_id=empresa_id))


def _hash_fingerprint(value):
    if not value:
        return "Sin hash"
    if len(value) <= 24:
        return value
    return f"{value[:12]}...{value[-8:]}"


def _find_report_from_url(report_url):
    workspace_guid, report_guid = parse_powerbi_url(report_url)
    if not workspace_guid or not report_guid:
        raise ValueError(
            "URL invalida. Usa el formato canonico: "
            "https://app.powerbi.com/groups/{workspace_id}/reports/{report_id}/..."
        )

    report = (
        Report.query
        .options(
            joinedload(Report.workspace).joinedload(Workspace.tenant),
            joinedload(Report.usuario_pbi),
        )
        .join(Workspace)
        .filter(
            Workspace.workspace_id == workspace_guid,
            Report.report_id == report_guid,
        )
        .first()
    )
    if report is None:
        raise LookupError(
            "No existe un reporte local para ese link. Primero cargalo desde Configuracion > Agregar desde URL."
        )
    return report


def _powerbi_error_message(exc):
    if isinstance(exc, KeyError):
        return "Power BI no devolvio datasetId para ese reporte."
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            return "Power BI rechazo la consulta: revisa permisos del usuario PBI asociado al reporte."
        if status == 404:
            return "Power BI no encontro el workspace, reporte o dataset para ese link."
        return f"Power BI devolvio un error al consultar metadata ({status})."
    return "No se pudo consultar metadata en Power BI."


def _resolve_metadata_from_url(report_url):
    report = _find_report_from_url(report_url)
    metadata = get_mcp_metadata_for_report(report)
    metadata['credential_report_id_fk'] = report.id
    metadata['workspace_id_fk'] = report.workspace.id
    return metadata


def _set_api_key_hash(config, raw_api_key):
    api_key_hash = hash_mcp_api_key(raw_api_key)
    duplicate = McpAgentConfig.query.filter_by(api_key_hash=api_key_hash).first()
    if duplicate is not None and duplicate.id != config.id:
        raise ValueError("Ya existe una configuracion MCP con esa API key.")
    config.api_key_hash = api_key_hash


def _apply_metadata(config, metadata):
    config.workspace_id = metadata["workspace_id"]
    config.workspace_name = metadata["workspace_name"]
    config.dataset_id = metadata["dataset_id"]
    config.dataset_name = metadata["dataset_name"]
    config.workspace_id_fk = metadata.get('workspace_id_fk')
    config.credential_report_id_fk = metadata.get('credential_report_id_fk')


def _add_value_error_to_form(form, exc):
    message = str(exc)
    if "API key" in message:
        form.api_key.errors.append(message)
    elif "URL" in message:
        form.report_url.errors.append(message)
    else:
        flash(message, "danger")


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    configs = (
        McpAgentConfig.query
        .options(
            joinedload(McpAgentConfig.empresa),
            joinedload(McpAgentConfig.empresa_links).joinedload(McpConfigEmpresa.empresa),
        )
        .order_by(McpAgentConfig.created_at.desc(), McpAgentConfig.id.desc())
        .all()
    )
    return render_template(
        'admin/mcp_config/list.html',
        configs=configs,
        hash_fingerprint=_hash_fingerprint,
        title='MCP Config',
    )


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def new():
    form = McpConfigCreateForm()
    form.empresa_id.choices = _empresa_choices()

    if form.validate_on_submit():
        config = McpAgentConfig()
        try:
            _set_api_key_hash(config, form.api_key.data)
            metadata = _resolve_metadata_from_url(form.report_url.data)
            _apply_metadata(config, metadata)
        except ValueError as exc:
            db.session.rollback()
            _add_value_error_to_form(form, exc)
        except LookupError as exc:
            db.session.rollback()
            flash(str(exc), "warning")
        except Exception as exc:
            db.session.rollback()
            LOG.exception("Could not create MCP config from Power BI URL")
            flash(_powerbi_error_message(exc), "danger")
        else:
            config.empresa_id = _selected_empresa_id(form.empresa_id.data)
            config.is_active = bool(form.is_active.data)
            db.session.add(config)
            db.session.commit()
            flash("Configuracion MCP creada correctamente.", "success")
            return redirect(url_for('mcp_config.detail', config_id=config.id))

    return render_template(
        'admin/mcp_config/form.html',
        form=form,
        title='Nueva MCP Config',
        config=None,
        back_url=url_for('mcp_config.list'),
    )


@bp.route('/<int:config_id>')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def detail(config_id):
    config = (
        McpAgentConfig.query
        .options(
            joinedload(McpAgentConfig.empresa),
            joinedload(McpAgentConfig.empresa_links).joinedload(McpConfigEmpresa.empresa),
            joinedload(McpAgentConfig.grants).joinedload(McpModelGrant.user),
            joinedload(McpAgentConfig.grants).joinedload(McpModelGrant.empresa),
            joinedload(McpAgentConfig.grants).joinedload(McpModelGrant.role),
        )
        .get_or_404(config_id)
    )
    return render_template(
        'admin/mcp_config/detail.html',
        config=config,
        hash_fingerprint=_hash_fingerprint,
        title='Detalle MCP Config',
        empresas=Empresa.query.filter_by(estado_activo=True).order_by(Empresa.nombre).all(),
        enabled_company_ids={link.empresa_id for link in config.empresa_links},
    )


@bp.route('/<int:config_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def edit(config_id):
    config = McpAgentConfig.query.get_or_404(config_id)
    form = McpConfigEditForm()
    form.empresa_id.choices = _empresa_choices()

    if form.validate_on_submit():
        try:
            if form.api_key.data:
                _set_api_key_hash(config, form.api_key.data)
            if form.report_url.data:
                metadata = _resolve_metadata_from_url(form.report_url.data)
                _apply_metadata(config, metadata)
        except ValueError as exc:
            db.session.rollback()
            _add_value_error_to_form(form, exc)
        except LookupError as exc:
            db.session.rollback()
            flash(str(exc), "warning")
        except Exception as exc:
            db.session.rollback()
            LOG.exception("Could not update MCP config")
            flash(_powerbi_error_message(exc), "danger")
        else:
            config.empresa_id = _selected_empresa_id(form.empresa_id.data)
            config.is_active = bool(form.is_active.data)
            db.session.commit()
            flash("Configuracion MCP actualizada correctamente.", "success")
            return redirect(url_for('mcp_config.detail', config_id=config.id))

    elif form.is_submitted() is False:
        form.empresa_id.data = config.empresa_id or 0
        form.is_active.data = config.is_active

    return render_template(
        'admin/mcp_config/form.html',
        form=form,
        title='Editar MCP Config',
        config=config,
        back_url=url_for('mcp_config.detail', config_id=config.id),
    )


@bp.route('/<int:config_id>/toggle-active', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def toggle_active(config_id):
    config = McpAgentConfig.query.get_or_404(config_id)
    config.is_active = not config.is_active
    db.session.commit()
    estado = "activada" if config.is_active else "desactivada"
    flash(f"Configuracion MCP {estado}.", "success")
    return redirect(url_for('mcp_config.detail', config_id=config.id))


@bp.route('/<int:config_id>/delete', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def delete(config_id):
    config = McpAgentConfig.query.get_or_404(config_id)
    db.session.delete(config)
    db.session.commit()
    flash("Configuracion MCP eliminada.", "success")
    return redirect(url_for('mcp_config.list'))


@bp.route('/<int:config_id>/companies', methods=['POST'])
@login_required
@permission_required('mcp.config.manage')
def add_company(config_id):
    config = McpAgentConfig.query.get_or_404(config_id)
    empresa = Empresa.query.get_or_404(request.form.get('empresa_id', type=int))
    _ensure_company_link(config, empresa.id)
    db.session.commit()
    flash('Empresa habilitada para este modelo.', 'success')
    return redirect(url_for('mcp_config.detail', config_id=config.id))


@bp.route('/<int:config_id>/companies/<int:empresa_id>/remove', methods=['POST'])
@login_required
@permission_required('mcp.config.manage')
def remove_company(config_id, empresa_id):
    config = McpAgentConfig.query.get_or_404(config_id)
    link = McpConfigEmpresa.query.filter_by(
        config_id=config.id,
        empresa_id=empresa_id,
    ).first_or_404()

    McpModelGrant.query.filter_by(
        config_id=config.id,
        empresa_id=empresa_id,
        is_active=True,
    ).update({'is_active': False}, synchronize_session=False)
    db.session.delete(link)
    db.session.commit()
    flash('Empresa deshabilitada; sus accesos MCP fueron revocados.', 'success')
    return redirect(url_for('mcp_config.detail', config_id=config.id))
