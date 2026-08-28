"""OAuth 2.0 and discovery endpoints for MCP clients."""

from datetime import datetime, timedelta, timezone
import base64
import secrets

import jwt
from authlib.oauth2 import OAuth2Error
from flask import Blueprint, current_app, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import csrf, db, limiter
from app.models import McpOAuthAuthorizationRequest, McpOAuthClient, McpOAuthSession, McpRefreshToken
from app.services.mcp_jwt_service import audit, decode_access_token, jwks_document
from app.services.mcp_oauth import authorization_server


bp = Blueprint('mcp_oauth', __name__)


def _now():
    return datetime.now(timezone.utc)


def _oauth_error(error):
    payload = {'error': error.error}
    if error.description:
        payload['error_description'] = error.description
    return jsonify(payload), error.status_code, {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}


@bp.route('/.well-known/oauth-authorization-server')
def authorization_server_metadata():
    issuer = current_app.config['MCP_OAUTH_ISSUER'].rstrip('/')
    return jsonify({
        'issuer': issuer,
        'authorization_endpoint': f'{issuer}/oauth/authorize',
        'token_endpoint': f'{issuer}/oauth/token',
        'revocation_endpoint': f'{issuer}/oauth/revoke',
        'jwks_uri': f'{issuer}/.well-known/jwks.json',
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'token_endpoint_auth_methods_supported': ['none', 'client_secret_basic', 'client_secret_post'],
        'code_challenge_methods_supported': ['S256'],
        'scopes_supported': ['mcp:models:list', 'mcp:models:read', 'mcp:models:query', 'mcp:models:write'],
    })


@bp.route('/.well-known/oauth-protected-resource')
def protected_resource_metadata():
    issuer = current_app.config['MCP_OAUTH_ISSUER'].rstrip('/')
    return jsonify({
        'resource': current_app.config.get('MCP_RESOURCE_URL', 'mcp-aklara'),
        'authorization_servers': [issuer],
        'bearer_methods_supported': ['header'],
        'scopes_supported': ['mcp:models:list', 'mcp:models:read', 'mcp:models:query', 'mcp:models:write'],
    })


@bp.route('/.well-known/jwks.json')
def jwks():
    return jsonify(jwks_document()), 200, {'Cache-Control': 'public, max-age=300'}


@bp.route('/oauth/authorize', methods=['GET', 'POST'])
@limiter.limit('30 per minute')
def authorize():
    if not current_user.is_authenticated:
        return redirect(url_for('auth.login', next=request.url))
    if not current_user.is_active:
        return jsonify({'error': 'access_denied', 'error_description': 'Inactive user'}), 403

    try:
        grant = authorization_server.get_consent_grant(end_user=current_user)
    except OAuth2Error as error:
        return _oauth_error(error)

    if request.method == 'GET':
        request_id = secrets.token_urlsafe(32)
        pending = McpOAuthAuthorizationRequest(
            request_id=request_id,
            client_id=grant.client.get_client_id(),
            user_id=current_user.id,
            resource=g.mcp_oauth_resource,
            request_uri=request.url,
            expires_at=_now() + timedelta(minutes=10),
        )
        db.session.add(pending)
        db.session.commit()
        return render_template(
            'oauth/authorize.html',
            client=grant.client,
            scopes=(grant.request.scope or '').split(),
            request_id=request_id,
            oauth_parameters=request.args,
        )

    pending = McpOAuthAuthorizationRequest.query.filter_by(
        request_id=request.form.get('request_id'), user_id=current_user.id, consumed_at=None
    ).first()
    expires_at = pending.expires_at if pending is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    submitted_resource = request.form.get('resource')
    if submitted_resource:
        submitted_resource = submitted_resource.rstrip('/')
    elif pending is not None:
        submitted_resource = pending.resource
    if (
        pending is None
        or expires_at <= _now()
        or pending.client_id != request.form.get('client_id')
        or pending.resource != submitted_resource
    ):
        return jsonify({'error': 'invalid_request', 'error_description': 'Authorization request expired'}), 400
    pending.consumed_at = _now()
    db.session.commit()
    approved = request.form.get('decision') == 'approve'
    return authorization_server.create_authorization_response(
        grant_user=current_user if approved else None
    )


@bp.route('/oauth/token', methods=['POST'])
@csrf.exempt
@limiter.limit('60 per minute')
def token():
    response = authorization_server.create_token_response()
    if response.status_code >= 400:
        payload = response.get_json(silent=True) or {}
        current_app.logger.warning(
            'OAuth token request failed: status=%s grant_type=%s client_id=%s error=%s description=%s',
            response.status_code,
            request.form.get('grant_type'),
            request.form.get('client_id'),
            payload.get('error'),
            payload.get('error_description'),
        )
    return response


def _authenticate_client():
    client_id = request.form.get('client_id')
    client_secret = request.form.get('client_secret')
    used_basic_auth = False
    if request.authorization and request.authorization.type.lower() == 'basic':
        used_basic_auth = True
        client_id = request.authorization.username
        client_secret = request.authorization.password
    client = McpOAuthClient.query.filter_by(client_id=client_id, is_active=True).first()
    if client is None:
        return None
    if client.token_endpoint_auth_method == 'none':
        return client if not used_basic_auth and not client_secret else None
    if not client.check_client_secret(client_secret):
        return None
    return client


@bp.route('/oauth/revoke', methods=['POST'])
@csrf.exempt
@limiter.limit('30 per minute')
def revoke():
    client = _authenticate_client()
    if client is None:
        return jsonify({'error': 'invalid_client'}), 401, {'WWW-Authenticate': 'Basic'}
    raw_token = request.form.get('token')
    if not raw_token:
        return jsonify({'error': 'invalid_request'}), 400

    oauth_session = None
    refresh = McpRefreshToken.query.filter_by(token_hash=McpRefreshToken.digest(raw_token)).first()
    if refresh and refresh.client_id == client.client_id:
        oauth_session = refresh.oauth_session
    else:
        try:
            claims = decode_access_token(raw_token, verify_exp=False)
            if claims.get('client_id') == client.client_id:
                oauth_session = McpOAuthSession.query.filter_by(public_id=claims.get('sid')).first()
        except jwt.PyJWTError:
            pass

    if oauth_session and oauth_session.revoked_at is None:
        oauth_session.revoked_at = _now()
        oauth_session.revoke_reason = 'oauth_revocation_endpoint'
        McpRefreshToken.query.filter_by(session_id=oauth_session.id, revoked_at=None).update(
            {'revoked_at': _now()}, synchronize_session=False
        )
        audit('oauth.session.revoked', oauth_session=oauth_session, details={'source': 'client'})
        db.session.commit()
    return '', 200, {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}


@bp.route('/oauth/sessions/<string:session_public_id>/revoke', methods=['POST'])
@login_required
def revoke_own_session(session_public_id):
    oauth_session = McpOAuthSession.query.filter_by(
        public_id=session_public_id, user_id=current_user.id
    ).first_or_404()
    if oauth_session.revoked_at is None:
        oauth_session.revoked_at = _now()
        oauth_session.revoke_reason = 'user_revocation'
        McpRefreshToken.query.filter_by(session_id=oauth_session.id, revoked_at=None).update(
            {'revoked_at': _now()}, synchronize_session=False
        )
        audit('oauth.session.revoked', oauth_session=oauth_session, details={'source': 'user'})
        db.session.commit()
    return '', 204
