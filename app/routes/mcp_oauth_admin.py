"""Backoffice management for static MCP OAuth clients and sessions."""

from datetime import datetime, timezone
import secrets

from flask import Blueprint, flash, make_response, redirect, render_template, request, url_for
from flask_login import login_required

from app import db
from app.models import McpOAuthClient, McpOAuthSession, McpRefreshToken
from app.services.mcp_jwt_service import audit
from app.utils.decorators import permission_required


bp = Blueprint('mcp_oauth_admin', __name__, url_prefix='/admin/mcp-oauth')


def _now():
    return datetime.now(timezone.utc)


def _revoke_client_sessions(client_id, reason):
    now = _now()
    sessions = McpOAuthSession.query.filter_by(client_id=client_id, revoked_at=None).all()
    for oauth_session in sessions:
        oauth_session.revoked_at = now
        oauth_session.revoke_reason = reason
        McpRefreshToken.query.filter_by(
            session_id=oauth_session.id, revoked_at=None
        ).update({'revoked_at': now}, synchronize_session=False)
        audit(
            'oauth.session.revoked',
            oauth_session=oauth_session,
            details={'source': 'client_deactivation'},
        )
    return len(sessions)


@bp.route('/')
@login_required
@permission_required('mcp.oauth_clients.manage')
def index():
    return render_template(
        'admin/mcp_oauth/index.html',
        clients=McpOAuthClient.query.order_by(McpOAuthClient.name).all(),
        sessions=McpOAuthSession.query.order_by(McpOAuthSession.created_at.desc()).limit(100).all(),
    )


def _show_client_secret(client, raw_secret, *, rotated=False):
    """Render an OAuth client secret exactly once, immediately after hashing it."""
    response = make_response(
        render_template(
            'admin/mcp_oauth/credentials.html',
            client=client,
            client_secret=raw_secret,
            rotated=rotated,
        )
    )
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response


@bp.route('/clients', methods=['POST'])
@login_required
@permission_required('mcp.oauth_clients.manage')
def save_client():
    client_id = (request.form.get('client_id') or '').strip()
    if not client_id:
        flash('Client ID requerido.', 'danger')
        return redirect(url_for('mcp_oauth_admin.index'))
    auth_method = (request.form.get('token_endpoint_auth_method') or 'none').strip()
    if auth_method not in {'none', 'client_secret_basic', 'client_secret_post'}:
        flash('Método de autenticación OAuth inválido.', 'danger')
        return redirect(url_for('mcp_oauth_admin.index'))
    client = McpOAuthClient.query.filter_by(client_id=client_id).first()
    is_new = client is None
    if client is not None and client.registration_method == 'dynamic':
        flash('Los clientes dinámicos no se pueden editar; sólo se pueden desactivar.', 'danger')
        return redirect(url_for('mcp_oauth_admin.index'))
    previous_auth_method = client.token_endpoint_auth_method if client is not None else None
    was_active = bool(client and client.is_active)
    if is_new:
        client = McpOAuthClient(client_id=client_id, registration_method='static')
        db.session.add(client)
    client.name = (request.form.get('name') or client_id).strip()
    client.redirect_uris = [
        item.strip() for item in (request.form.get('redirect_uris') or '').splitlines() if item.strip()
    ]
    client.allowed_scopes = [
        item.strip() for item in (request.form.get('allowed_scopes') or '').split() if item.strip()
    ]
    client.grant_types = ['authorization_code', 'refresh_token']
    client.response_types = ['code']
    client.token_endpoint_auth_method = auth_method
    client.is_active = request.form.get('is_active') == 'on'
    raw_secret = (request.form.get('client_secret') or '').strip()
    if auth_method == 'none':
        raw_secret = None
        client.client_secret_hash = None
    elif (is_new or previous_auth_method == 'none') and not raw_secret:
        raw_secret = secrets.token_urlsafe(48)
    if auth_method != 'none' and raw_secret:
        client.set_client_secret(raw_secret)
    if was_active and not client.is_active:
        revoked_count = _revoke_client_sessions(client.client_id, 'oauth_client_deactivated')
        audit(
            'oauth.client.status_changed',
            client_id=client.client_id,
            details={
                'is_active': False,
                'registration_method': client.registration_method,
                'revoked_session_count': revoked_count,
            },
        )
    db.session.commit()
    if raw_secret:
        return _show_client_secret(client, raw_secret, rotated=not is_new)
    flash('Cliente OAuth guardado.', 'success')
    return redirect(url_for('mcp_oauth_admin.index'))


@bp.route('/clients/<int:client_pk>/regenerate-secret', methods=['POST'])
@login_required
@permission_required('mcp.oauth_clients.manage')
def regenerate_client_secret(client_pk):
    """Replace a client's secret and show the new plaintext value once."""
    client = db.get_or_404(McpOAuthClient, client_pk)
    if client.registration_method == 'dynamic':
        flash('Los secretos de clientes dinámicos no se pueden regenerar.', 'warning')
        return redirect(url_for('mcp_oauth_admin.index'))
    if client.token_endpoint_auth_method == 'none':
        flash('Los clientes públicos con PKCE no utilizan client secret.', 'warning')
        return redirect(url_for('mcp_oauth_admin.index'))
    raw_secret = secrets.token_urlsafe(48)
    client.set_client_secret(raw_secret)
    db.session.commit()
    return _show_client_secret(client, raw_secret, rotated=True)


@bp.route('/clients/<int:client_pk>/status', methods=['POST'])
@login_required
@permission_required('mcp.oauth_clients.manage')
def set_client_status(client_pk):
    client = db.get_or_404(McpOAuthClient, client_pk)
    activate = request.form.get('is_active') == 'true'
    if client.is_active == activate:
        return redirect(url_for('mcp_oauth_admin.index'))
    client.is_active = activate
    revoked_count = 0
    if not activate:
        revoked_count = _revoke_client_sessions(client.client_id, 'oauth_client_deactivated')
    audit(
        'oauth.client.status_changed',
        client_id=client.client_id,
        details={
            'is_active': activate,
            'registration_method': client.registration_method,
            'revoked_session_count': revoked_count,
        },
    )
    db.session.commit()
    flash('Cliente OAuth activado.' if activate else 'Cliente OAuth desactivado.', 'success')
    return redirect(url_for('mcp_oauth_admin.index'))


@bp.route('/sessions/<string:session_public_id>/revoke', methods=['POST'])
@login_required
@permission_required('mcp.oauth_clients.manage')
def revoke_session(session_public_id):
    oauth_session = McpOAuthSession.query.filter_by(public_id=session_public_id).first_or_404()
    if oauth_session.revoked_at is None:
        oauth_session.revoked_at = _now()
        oauth_session.revoke_reason = 'backoffice_revocation'
        McpRefreshToken.query.filter_by(session_id=oauth_session.id, revoked_at=None).update(
            {'revoked_at': _now()}, synchronize_session=False
        )
        audit('oauth.session.revoked', oauth_session=oauth_session, details={'source': 'backoffice'})
        db.session.commit()
    flash('Sesión OAuth revocada.', 'success')
    return redirect(url_for('mcp_oauth_admin.index'))
