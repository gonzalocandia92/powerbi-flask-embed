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
    previous_auth_method = client.token_endpoint_auth_method if client is not None else None
    if is_new:
        client = McpOAuthClient(client_id=client_id)
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
        if is_new:
            client.set_client_secret(secrets.token_urlsafe(48))
    elif (is_new or previous_auth_method == 'none') and not raw_secret:
        raw_secret = secrets.token_urlsafe(48)
    if auth_method != 'none' and raw_secret:
        client.set_client_secret(raw_secret)
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
    if client.token_endpoint_auth_method == 'none':
        flash('Los clientes públicos con PKCE no utilizan client secret.', 'warning')
        return redirect(url_for('mcp_oauth_admin.index'))
    raw_secret = secrets.token_urlsafe(48)
    client.set_client_secret(raw_secret)
    db.session.commit()
    return _show_client_secret(client, raw_secret, rotated=True)


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
