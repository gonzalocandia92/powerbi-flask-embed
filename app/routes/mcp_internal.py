"""Private broker API used by mcp-aklara; never expose it directly."""

from datetime import datetime, timezone
from functools import wraps

import jwt
from flask import Blueprint, current_app, g, jsonify, request

from app import csrf, db, limiter
from app.models import McpSecurityAuditLog
from app.services.mcp_access_service import list_user_grants, resolve_grant
from app.services.mcp_jwt_service import audit, validate_access_token
from app.utils.powerbi import _get_access_token


bp = Blueprint('mcp_internal', __name__, url_prefix='/internal/mcp')


def _now():
    return datetime.now(timezone.utc)


def _service_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_app.config['MCP_INTERNAL_REQUIRE_MTLS']:
            if request.headers.get('X-Client-Cert-Verified') != 'SUCCESS':
                return jsonify({'error': 'service_mtls_required'}), 401
        secret = current_app.config.get('MCP_INTERNAL_JWT_SECRET')
        authorization = request.headers.get('Authorization', '')
        if not secret or not authorization.startswith('Bearer '):
            return jsonify({'error': 'service_auth_required'}), 401
        try:
            g.service_claims = jwt.decode(
                authorization[7:], secret, algorithms=['HS256'],
                audience='powerbi-flask-internal', issuer='mcp-aklara',
            )
        except jwt.PyJWTError:
            return jsonify({'error': 'invalid_service_token'}), 401
        return view(*args, **kwargs)
    return wrapped


def _user_token(required_scopes=None):
    value = request.headers.get('X-MCP-User-Token', '')
    if value.startswith('Bearer '):
        value = value[7:]
    if not value:
        body = request.get_json(silent=True) or {}
        value = body.get('access_token', '')
    try:
        claims, oauth_session = validate_access_token(value, required_scopes=required_scopes)
    except jwt.PyJWTError as exc:
        return None, (jsonify({'error': 'invalid_user_token', 'error_description': str(exc)}), 401)
    return (claims, oauth_session), None


@bp.route('/validate-token', methods=['POST'])
@csrf.exempt
@limiter.limit('300 per minute')
@_service_required
def validate_token():
    auth, error = _user_token()
    if error:
        return error
    claims, oauth_session = auth
    return jsonify({
        'active': True,
        'sub': claims['sub'],
        'client_id': claims['client_id'],
        'scope': claims.get('scope', ''),
        'sid': oauth_session.public_id,
        'exp': claims['exp'],
    })


@bp.route('/list-access', methods=['POST'])
@csrf.exempt
@limiter.limit('300 per minute')
@_service_required
def list_access():
    auth, error = _user_token(['mcp:models:list'])
    if error:
        return error
    claims, oauth_session = auth
    models = []
    for grant, permissions in list_user_grants(int(claims['sub'])):
        models.append({
            'grant_public_id': grant.public_id,
            'model': {
                'public_id': grant.config.public_id,
                'key': grant.config.model_key,
                'name': grant.config.dataset_name,
                'description': grant.config.description,
                'domain': grant.config.domain,
            },
            'company': {'id': grant.empresa_id, 'name': grant.empresa.nombre},
            'role': grant.role.name,
            'permissions': sorted(permissions),
        })
    audit('mcp.access.listed', oauth_session=oauth_session, details={'count': len(models)})
    db.session.commit()
    return jsonify({'models': models})


@bp.route('/resolve-access', methods=['POST'])
@csrf.exempt
@limiter.limit('300 per minute')
@_service_required
def resolve_access():
    body = request.get_json(silent=True) or {}
    scope_by_permission = {
        'mcp.model.schema.read': 'mcp:models:read',
        'mcp.model.query.execute': 'mcp:models:query',
        'mcp.model.measure.create': 'mcp:models:write',
        'mcp.model.measure.update': 'mcp:models:write',
    }
    grant_public_id = body.get('grant_public_id')
    required_permission = body.get('required_permission')
    if not isinstance(grant_public_id, str) or not grant_public_id.strip():
        return jsonify({'error': 'grant_public_id_required'}), 400
    if required_permission not in scope_by_permission:
        return jsonify({'error': 'invalid_permission'}), 400

    required_scope = scope_by_permission[required_permission]
    auth, error = _user_token([required_scope])
    if error:
        return error
    claims, oauth_session = auth
    grant, permissions = resolve_grant(
        int(claims['sub']), grant_public_id, required_permission
    )
    if grant is None:
        audit(
            'mcp.access.denied', oauth_session=oauth_session, outcome='denied',
            details={'reason': permissions, 'grant_public_id': grant_public_id},
        )
        db.session.commit()
        status = 403 if permissions == 'permission_denied' else 404
        return jsonify({'error': permissions}), status
    if grant.config.credential_report is None:
        return jsonify({'error': 'powerbi_credential_source_missing'}), 409

    powerbi_token = _get_access_token(grant.config.credential_report)
    audit(
        'mcp.access.resolved', oauth_session=oauth_session, grant=grant,
        details={'required_permission': required_permission},
    )
    db.session.commit()
    return jsonify({
        'grant_public_id': grant.public_id,
        'company': {'id': grant.empresa_id, 'name': grant.empresa.nombre},
        'workspace_id': grant.config.workspace_id,
        'workspace_name': grant.config.workspace_name,
        'dataset_id': grant.config.dataset_id,
        'dataset_name': grant.config.dataset_name,
        'permissions': sorted(permissions),
        'powerbi_access_token': powerbi_token,
    }), 200, {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}


@bp.route('/audit-result', methods=['POST'])
@csrf.exempt
@limiter.limit('600 per minute')
@_service_required
def audit_result():
    auth, error = _user_token()
    if error:
        return error
    claims, oauth_session = auth
    body = request.get_json(silent=True) or {}
    grant, _ = resolve_grant(int(claims['sub']), body.get('grant_public_id'))
    if grant is None:
        return jsonify({'error': 'grant_not_found'}), 404
    allowed_details = {
        key: body[key] for key in ('operation', 'status', 'duration_ms', 'row_count', 'error_code')
        if key in body
    }
    audit(
        'mcp.operation.completed', oauth_session=oauth_session, grant=grant,
        outcome='success' if body.get('status') == 'success' else 'failure', details=allowed_details,
    )
    db.session.commit()
    return '', 204
