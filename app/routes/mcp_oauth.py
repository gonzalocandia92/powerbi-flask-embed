"""OAuth 2.0 and discovery endpoints for MCP clients."""

from datetime import datetime, timedelta, timezone
import base64
import json
import secrets
from urllib.parse import urlsplit

import jwt
from authlib.oauth2 import OAuth2Error
from flask import Blueprint, abort, current_app, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import csrf, db, limiter
from app.models import McpOAuthAuthorizationRequest, McpOAuthClient, McpOAuthSession, McpRefreshToken
from app.services.mcp_jwt_service import audit, decode_access_token, jwks_document
from app.services.mcp_oauth import authorization_server


bp = Blueprint('mcp_oauth', __name__)

MCP_SCOPES = (
    'mcp:models:list',
    'mcp:models:read',
    'mcp:models:query',
    'mcp:models:write',
)
DCR_AUTH_METHODS = {'none', 'client_secret_basic', 'client_secret_post'}
DCR_MAX_BODY_BYTES = 16 * 1024
DCR_MAX_REDIRECT_URIS = 10
DCR_MAX_REDIRECT_URI_LENGTH = 2048
DCR_LOOPBACK_HOSTS = {'localhost', '127.0.0.1', '::1'}


def _now():
    return datetime.now(timezone.utc)


def _oauth_error(error):
    payload = {'error': error.error}
    if error.description:
        payload['error_description'] = error.description
    return jsonify(payload), error.status_code, {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}


def _dcr_enabled():
    return bool(current_app.config.get('MCP_OAUTH_DCR_ENABLED', False))


def _dcr_error(error, description, *, status=400):
    current_app.logger.warning(json.dumps({
        'event': 'oauth.client.registration_rejected',
        'error': error,
        'error_description': description,
        'remote_addr': request.remote_addr,
    }, separators=(',', ':')))
    return (
        jsonify({'error': error, 'error_description': description}),
        status,
        {'Cache-Control': 'no-store', 'Pragma': 'no-cache'},
    )


def _validated_redirect_uris(value):
    if not isinstance(value, list) or not value or len(value) > DCR_MAX_REDIRECT_URIS:
        raise ValueError(f'redirect_uris must contain between 1 and {DCR_MAX_REDIRECT_URIS} values')
    if any(not isinstance(uri, str) for uri in value):
        raise ValueError('Every redirect URI must be a string')
    if len(set(value)) != len(value):
        raise ValueError('Duplicate redirect URIs are not allowed')

    for uri in value:
        if not uri or uri != uri.strip() or len(uri) > DCR_MAX_REDIRECT_URI_LENGTH:
            raise ValueError('A redirect URI is empty, padded, or too long')
        try:
            parsed = urlsplit(uri)
            host = (parsed.hostname or '').lower()
            parsed.port  # Force validation of malformed ports.
        except ValueError as exc:
            raise ValueError('A redirect URI is malformed') from exc
        if not parsed.scheme or not parsed.netloc or not host or parsed.fragment:
            raise ValueError('Redirect URIs must be absolute and must not contain fragments')
        if parsed.username is not None or parsed.password is not None:
            raise ValueError('Redirect URIs must not contain user information')
        scheme = parsed.scheme.lower()
        if scheme == 'https':
            continue
        if scheme == 'http' and host in DCR_LOOPBACK_HOSTS:
            continue
        raise ValueError('Redirect URIs must use HTTPS, except for HTTP loopback callbacks')
    return value


def _validated_string_list(payload, name, default, allowed, *, required_value=None):
    value = payload.get(name, default)
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise ValueError(f'{name} must be a non-empty string array')
    if len(set(value)) != len(value) or any(item not in allowed for item in value):
        raise ValueError(f'{name} contains duplicate or unsupported values')
    if required_value is not None and required_value not in value:
        raise ValueError(f'{name} must include {required_value}')
    return [item for item in allowed if item in value]


@bp.route('/.well-known/oauth-authorization-server')
def authorization_server_metadata():
    issuer = current_app.config['MCP_OAUTH_ISSUER'].rstrip('/')
    metadata = {
        'issuer': issuer,
        'authorization_endpoint': f'{issuer}/oauth/authorize',
        'token_endpoint': f'{issuer}/oauth/token',
        'revocation_endpoint': f'{issuer}/oauth/revoke',
        'jwks_uri': f'{issuer}/.well-known/jwks.json',
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'token_endpoint_auth_methods_supported': ['none', 'client_secret_basic', 'client_secret_post'],
        'code_challenge_methods_supported': ['S256'],
        'scopes_supported': list(MCP_SCOPES),
    }
    if _dcr_enabled():
        metadata['registration_endpoint'] = f'{issuer}/oauth/register'
    return jsonify(metadata)


@bp.route('/.well-known/oauth-protected-resource')
def protected_resource_metadata():
    issuer = current_app.config['MCP_OAUTH_ISSUER'].rstrip('/')
    return jsonify({
        'resource': current_app.config.get('MCP_RESOURCE_URL', 'mcp-aklara'),
        'authorization_servers': [issuer],
        'bearer_methods_supported': ['header'],
        'scopes_supported': list(MCP_SCOPES),
    })


@bp.route('/.well-known/jwks.json')
def jwks():
    return jsonify(jwks_document()), 200, {'Cache-Control': 'public, max-age=300'}


@bp.route('/oauth/register', methods=['POST'])
@csrf.exempt
@limiter.limit('20 per minute')
@limiter.limit('200 per day')
def register_client():
    """Register an OAuth client using the RFC 7591 DCR protocol."""
    if not _dcr_enabled():
        abort(404)
    if request.content_length is not None and request.content_length > DCR_MAX_BODY_BYTES:
        return _dcr_error('invalid_client_metadata', 'Registration request body is too large')
    if not request.is_json:
        return _dcr_error('invalid_client_metadata', 'Content-Type must be application/json')
    raw_body = request.stream.read(DCR_MAX_BODY_BYTES + 1)
    if len(raw_body) > DCR_MAX_BODY_BYTES:
        return _dcr_error('invalid_client_metadata', 'Registration request body is too large')
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return _dcr_error('invalid_client_metadata', 'Registration metadata must be a JSON object')

    try:
        redirect_uris = _validated_redirect_uris(payload.get('redirect_uris'))
    except ValueError as exc:
        return _dcr_error('invalid_redirect_uri', str(exc))

    client_name = payload.get('client_name', 'MCP Client')
    if not isinstance(client_name, str):
        return _dcr_error('invalid_client_metadata', 'client_name must be a string')
    client_name = client_name.strip()
    if not client_name or len(client_name) > 200:
        return _dcr_error(
            'invalid_client_metadata', 'client_name must contain between 1 and 200 characters'
        )

    auth_method = payload.get('token_endpoint_auth_method', 'client_secret_basic')
    if auth_method not in DCR_AUTH_METHODS:
        return _dcr_error(
            'invalid_client_metadata', 'Unsupported token_endpoint_auth_method'
        )
    try:
        grant_types = _validated_string_list(
            payload,
            'grant_types',
            ['authorization_code'],
            ['authorization_code', 'refresh_token'],
            required_value='authorization_code',
        )
        response_types = _validated_string_list(
            payload, 'response_types', ['code'], ['code'], required_value='code'
        )
    except ValueError as exc:
        return _dcr_error('invalid_client_metadata', str(exc))

    requested_scope = payload.get('scope')
    if requested_scope is None:
        allowed_scopes = list(MCP_SCOPES)
    elif not isinstance(requested_scope, str):
        return _dcr_error('invalid_client_metadata', 'scope must be a space-separated string')
    else:
        allowed_scopes = requested_scope.split()
        if (
            not allowed_scopes
            or len(set(allowed_scopes)) != len(allowed_scopes)
            or any(scope not in MCP_SCOPES for scope in allowed_scopes)
        ):
            return _dcr_error('invalid_client_metadata', 'scope contains duplicate or unsupported values')
        allowed_scopes = [scope for scope in MCP_SCOPES if scope in allowed_scopes]

    for _ in range(5):
        client_id = f'mcp-dcr-{secrets.token_urlsafe(32)}'
        if McpOAuthClient.query.filter_by(client_id=client_id).first() is None:
            break
    else:  # pragma: no cover - cryptographically improbable
        current_app.logger.error('Could not allocate a unique OAuth DCR client ID')
        return _dcr_error('invalid_client_metadata', 'Unable to allocate a client identifier', status=500)

    issued_at = _now()
    raw_secret = secrets.token_urlsafe(48) if auth_method != 'none' else None
    client = McpOAuthClient(
        client_id=client_id,
        name=client_name,
        redirect_uris=redirect_uris,
        allowed_scopes=allowed_scopes,
        grant_types=grant_types,
        response_types=response_types,
        token_endpoint_auth_method=auth_method,
        registration_method='dynamic',
        is_active=True,
        created_at=issued_at,
        updated_at=issued_at,
    )
    if raw_secret:
        client.set_client_secret(raw_secret)
    db.session.add(client)
    audit(
        'oauth.client.registered',
        client_id=client_id,
        details={
            'registration_method': 'dynamic',
            'client_name': client_name,
            'token_endpoint_auth_method': auth_method,
            'redirect_uri_count': len(redirect_uris),
        },
    )
    db.session.commit()

    response_payload = {
        'client_id': client_id,
        'client_id_issued_at': int(issued_at.timestamp()),
        'client_name': client_name,
        'redirect_uris': redirect_uris,
        'grant_types': grant_types,
        'response_types': response_types,
        'token_endpoint_auth_method': auth_method,
        'scope': ' '.join(allowed_scopes),
    }
    if raw_secret:
        response_payload.update({
            'client_secret': raw_secret,
            'client_secret_expires_at': 0,
        })
    return (
        jsonify(response_payload),
        201,
        {'Cache-Control': 'no-store', 'Pragma': 'no-cache'},
    )


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
