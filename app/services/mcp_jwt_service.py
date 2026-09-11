"""RS256 access-token issuance and stateful validation for MCP OAuth."""

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import current_app

from app import db
from app.models import McpOAuthSession, McpSecurityAuditLog


LOG = logging.getLogger(__name__)
def _now():
    return datetime.now(timezone.utc)


def _issuer():
    return current_app.config['MCP_OAUTH_ISSUER'].rstrip('/')


def _load_private_key():
    cached = current_app.extensions.get('mcp_oauth_private_key')
    if cached is not None:
        return cached

    configured = current_app.config.get('MCP_OAUTH_PRIVATE_KEY')
    if configured:
        configured = configured.replace('\\n', '\n')
        key = serialization.load_pem_private_key(configured.encode(), password=None)
        current_app.extensions['mcp_oauth_private_key'] = key
        return key

    key_path = current_app.config.get('MCP_OAUTH_PRIVATE_KEY_FILE')
    if key_path and Path(key_path).exists():
        key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
        current_app.extensions['mcp_oauth_private_key'] = key
        return key

    # Local/test fallback. Production deployments should mount one stable key
    # through MCP_OAUTH_PRIVATE_KEY or MCP_OAUTH_PRIVATE_KEY_FILE.
    key_path = Path(current_app.instance_path) / 'mcp_oauth_private.pem'
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        current_app.extensions['mcp_oauth_private_key'] = key
        return key

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    current_app.extensions['mcp_oauth_private_key'] = key
    LOG.warning('Generated a local MCP OAuth signing key at %s', key_path)
    return key


def _key_id(public_key):
    der = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    import hashlib
    return hashlib.sha256(der).hexdigest()[:16]


def issue_access_token(oauth_session, scope, expires_in=None):
    now = _now()
    lifetime = int(expires_in or current_app.config['MCP_OAUTH_ACCESS_TOKEN_TTL'])
    key = _load_private_key()
    kid = _key_id(key.public_key())
    claims = {
        'iss': _issuer(),
        'sub': str(oauth_session.user_id),
        'aud': oauth_session.resource,
        'client_id': oauth_session.client_id,
        'scope': scope or oauth_session.scope,
        'sid': oauth_session.public_id,
        'iat': now,
        'exp': now + timedelta(seconds=lifetime),
        'jti': str(uuid.uuid4()),
    }
    return jwt.encode(claims, key, algorithm='RS256', headers={'kid': kid, 'typ': 'at+jwt'})


def decode_access_token(token, verify_exp=True):
    key = _load_private_key().public_key()
    return jwt.decode(
        token,
        key,
        algorithms=['RS256'],
        audience=current_app.config['MCP_RESOURCE_URL'],
        issuer=_issuer(),
        options={'verify_exp': verify_exp},
    )


def validate_access_token(token, required_scopes=None, touch=True):
    claims = decode_access_token(token)
    oauth_session = McpOAuthSession.query.filter_by(public_id=claims.get('sid')).first()
    if oauth_session is None or not oauth_session.is_active:
        raise jwt.InvalidTokenError('OAuth session is revoked or inactive')
    if str(oauth_session.user_id) != claims.get('sub') or oauth_session.client_id != claims.get('client_id'):
        raise jwt.InvalidTokenError('OAuth session does not match token')
    if oauth_session.resource != claims.get('aud'):
        raise jwt.InvalidTokenError('OAuth session resource does not match token audience')

    scopes = set((claims.get('scope') or '').split())
    missing = set(required_scopes or []) - scopes
    if missing:
        raise jwt.InvalidTokenError(f'Missing scopes: {" ".join(sorted(missing))}')

    if touch:
        oauth_session.last_used_at = _now()
        db.session.commit()
    return claims, oauth_session


def jwks_document():
    key = _load_private_key()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({'kid': _key_id(key.public_key()), 'use': 'sig', 'alg': 'RS256'})
    return {'keys': [jwk]}


def audit(
    event_type, *, oauth_session=None, grant=None, client_id=None,
    outcome='success', details=None
):
    entry = McpSecurityAuditLog(
        event_type=event_type,
        user_id=oauth_session.user_id if oauth_session else (grant.user_id if grant else None),
        client_id=oauth_session.client_id if oauth_session else client_id,
        session_public_id=oauth_session.public_id if oauth_session else None,
        grant_public_id=grant.public_id if grant else None,
        empresa_id=grant.empresa_id if grant else None,
        outcome=outcome,
        details=details or {},
    )
    db.session.add(entry)
    return entry
