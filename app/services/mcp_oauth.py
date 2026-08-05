"""Authlib-backed OAuth 2.0 authorization server for static MCP clients."""

from datetime import datetime, timedelta, timezone
import secrets

from authlib.integrations.flask_oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant, RefreshTokenGrant
from authlib.oauth2.rfc6749.errors import InvalidGrantError, InvalidRequestError
from authlib.oauth2.rfc7636 import CodeChallenge
from flask import current_app, g

from app import db
from app.models import (
    McpAuthorizationCode,
    McpOAuthClient,
    McpOAuthSession,
    McpRefreshToken,
    User,
)
from app.services.mcp_jwt_service import audit, issue_access_token


authorization_server = AuthorizationServer()


def _now():
    return datetime.now(timezone.utc)


def _not_expired(column):
    return column > _now()


def _resource_values(payload):
    values = payload.datalist.get('resource', [])
    if isinstance(values, str):
        values = [values]
    return [value for value in values if value]


def _resolve_resource(payload, expected=None):
    values = _resource_values(payload)
    if len(values) > 1:
        raise InvalidRequestError("At most one 'resource' parameter is allowed")
    configured = current_app.config['MCP_RESOURCE_URL']
    resource = values[0].rstrip('/') if values else (expected or configured)
    if resource != configured:
        raise InvalidRequestError("The requested 'resource' is not allowed")
    if expected is not None and resource != expected:
        raise InvalidGrantError("The 'resource' does not match the authorization grant")
    g.mcp_oauth_resource = resource
    return resource


class RequiredCodeChallenge(CodeChallenge):
    """Require S256 PKCE for confidential clients as well as public clients."""

    def validate_code_challenge(self, grant, redirect_uri):
        super().validate_code_challenge(grant, redirect_uri)
        payload = grant.request.payload.data
        if not payload.get('code_challenge'):
            raise InvalidRequestError("Missing 'code_challenge'")
        if payload.get('code_challenge_method') != 'S256':
            raise InvalidRequestError("Only S256 'code_challenge_method' is supported")


class McpAuthorizationCodeGrant(AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ['client_secret_basic', 'client_secret_post', 'none']

    def validate_authorization_request(self):
        redirect_uri = super().validate_authorization_request()
        _resolve_resource(self.request.payload)
        return redirect_uri

    def validate_token_request(self):
        super().validate_token_request()
        _resolve_resource(
            self.request.payload, expected=self.request.authorization_code.resource
        )

    def save_authorization_code(self, code, request):
        payload = request.payload.data
        item = McpAuthorizationCode(
            code_hash=McpAuthorizationCode.digest(code),
            client_id=request.client.get_client_id(),
            user_id=request.user.id,
            resource=_resolve_resource(request.payload),
            redirect_uri=request.payload.redirect_uri,
            scope=request.scope,
            nonce=payload.get('nonce'),
            code_challenge=payload.get('code_challenge'),
            code_challenge_method=payload.get('code_challenge_method'),
            expires_at=_now() + timedelta(minutes=5),
        )
        db.session.add(item)
        db.session.commit()

    def query_authorization_code(self, code, client):
        return McpAuthorizationCode.query.filter(
            McpAuthorizationCode.code_hash == McpAuthorizationCode.digest(code),
            McpAuthorizationCode.client_id == client.get_client_id(),
            McpAuthorizationCode.consumed_at.is_(None),
            _not_expired(McpAuthorizationCode.expires_at),
        ).first()

    def delete_authorization_code(self, authorization_code):
        authorization_code.consumed_at = _now()
        db.session.commit()

    def authenticate_user(self, authorization_code):
        return User.query.filter_by(id=authorization_code.user_id, is_active=True).first()


class McpRefreshTokenGrant(RefreshTokenGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ['client_secret_basic', 'client_secret_post', 'none']
    INCLUDE_NEW_REFRESH_TOKEN = True

    def authenticate_refresh_token(self, refresh_token):
        item = (
            McpRefreshToken.query
            .filter_by(token_hash=McpRefreshToken.digest(refresh_token))
            .with_for_update()
            .first()
        )
        if item is None or item.client_id != self.request.client.get_client_id():
            return None
        if item.replaced_by_hash:
            now = _now()
            McpRefreshToken.query.filter_by(
                family_id=item.family_id, revoked_at=None
            ).update({'revoked_at': now}, synchronize_session=False)
            oauth_session = item.oauth_session
            if oauth_session.revoked_at is None:
                oauth_session.revoked_at = now
                oauth_session.revoke_reason = 'refresh_token_reuse'
            audit(
                'oauth.refresh_token.reuse_detected', oauth_session=oauth_session,
                outcome='denied', details={'family_id': item.family_id},
            )
            db.session.commit()
            raise InvalidGrantError('Refresh token reuse detected')
        if not item.is_expired() and not item.is_revoked():
            g.mcp_refresh_credential = item
            g.mcp_refresh_family_id = item.family_id
            g.mcp_oauth_session_id = item.oauth_session.public_id
            return item
        return None

    def validate_token_request(self):
        super().validate_token_request()
        _resolve_resource(
            self.request.payload, expected=self.request.refresh_token.resource
        )

    def authenticate_user(self, refresh_token):
        return User.query.filter_by(id=refresh_token.user_id, is_active=True).first()

    def revoke_old_credential(self, refresh_token):
        refresh_token.revoked_at = _now()
        refresh_token.replaced_by_hash = getattr(g, 'mcp_new_refresh_hash', None)
        audit(
            'oauth.refresh_token.rotated', oauth_session=refresh_token.oauth_session,
            details={'family_id': refresh_token.family_id},
        )
        db.session.commit()


def _query_client(client_id):
    return McpOAuthClient.query.filter_by(client_id=client_id, is_active=True).first()


def _generate_token(grant_type, client, user=None, scope=None, expires_in=None, include_refresh_token=True):
    oauth_session = None
    session_public_id = getattr(g, 'mcp_oauth_session_id', None)
    if session_public_id:
        oauth_session = McpOAuthSession.query.filter_by(public_id=session_public_id).first()
    if oauth_session is None:
        oauth_session = McpOAuthSession(
            user_id=user.id,
            client_id=client.get_client_id(),
            resource=getattr(g, 'mcp_oauth_resource', None),
            scope=scope or '',
        )
        db.session.add(oauth_session)
        db.session.flush()
        g.mcp_oauth_session_id = oauth_session.public_id
        audit('oauth.session.created', oauth_session=oauth_session)
    elif oauth_session.resource != getattr(g, 'mcp_oauth_resource', None):
        raise InvalidGrantError("OAuth session resource does not match the token request")

    lifetime = int(expires_in or current_app.config['MCP_OAUTH_ACCESS_TOKEN_TTL'])
    token = {
        'token_type': 'Bearer',
        'access_token': issue_access_token(oauth_session, scope, lifetime),
        'expires_in': lifetime,
        'scope': scope or oauth_session.scope,
        'resource': oauth_session.resource,
    }
    if include_refresh_token:
        token['refresh_token'] = secrets.token_urlsafe(64)
    return token


def _save_token(token, request):
    raw_refresh_token = token.get('refresh_token')
    if not raw_refresh_token:
        return
    oauth_session = McpOAuthSession.query.filter_by(
        public_id=getattr(g, 'mcp_oauth_session_id', None)
    ).one()
    token_hash = McpRefreshToken.digest(raw_refresh_token)
    item = McpRefreshToken(
        token_hash=token_hash,
        session_id=oauth_session.id,
        family_id=getattr(g, 'mcp_refresh_family_id', None) or secrets.token_hex(16),
        client_id=request.client.get_client_id(),
        user_id=request.user.id,
        resource=oauth_session.resource,
        scope=token.get('scope') or oauth_session.scope,
        expires_at=_now() + timedelta(seconds=current_app.config['MCP_OAUTH_REFRESH_TOKEN_TTL']),
    )
    db.session.add(item)
    g.mcp_new_refresh_hash = token_hash
    audit('oauth.token.issued', oauth_session=oauth_session, details={'grant_type': request.payload.grant_type})
    if request.payload.grant_type != 'refresh_token':
        db.session.commit()


def init_oauth_server(app):
    authorization_server.init_app(app, query_client=_query_client, save_token=_save_token)
    authorization_server.register_token_generator('default', _generate_token)
    authorization_server.register_grant(McpAuthorizationCodeGrant, [RequiredCodeChallenge(required=True)])
    authorization_server.register_grant(McpRefreshTokenGrant)
