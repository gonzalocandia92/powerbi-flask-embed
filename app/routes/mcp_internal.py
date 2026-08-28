"""Private broker API used by mcp-aklara; never expose it directly."""
import asyncio

from datetime import datetime, timezone
from functools import wraps

import jwt
from flask import Blueprint, current_app, g, jsonify, request

from app import csrf, db, limiter
from app.models import McpSecurityAuditLog
from app.services.mcp_access_service import list_user_grants, resolve_grant
from app.services.mcp_jwt_service import audit, validate_access_token
from app.services.mcp_skill_service import (
    SkillReportContextError,
    SkillSelectionProviderError,
    list_grant_skills,
    resolve_mcp_skill_scope,
    select_grant_skills,
)
from app.services.mcp_schema_service import get_grant_relevant_schema
from app.services.schema_retrieval_service import (
    RelevantSchemaProviderError,
    SchemaEmbeddingsUnavailable,
)
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
        'mcp.model.skills.read': 'mcp:models:read',
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


def _authorized_skill_grant(body):
    auth, error = _user_token(['mcp:models:read'])
    if error:
        return None, None, error
    claims, oauth_session = auth
    grant_public_id = body.get('grant_public_id')
    if not isinstance(grant_public_id, str) or not grant_public_id.strip():
        return None, oauth_session, (jsonify({'error': 'grant_public_id_required'}), 400)
    grant, reason = resolve_grant(
        int(claims['sub']), grant_public_id, 'mcp.model.skills.read'
    )
    if grant is None:
        audit(
            'mcp.access.denied', oauth_session=oauth_session, outcome='denied',
            details={'reason': reason, 'grant_public_id': grant_public_id},
        )
        db.session.commit()
        status = 403 if reason == 'permission_denied' else 404
        return None, oauth_session, (jsonify({'error': reason}), status)
    return grant, oauth_session, None


def _authorized_schema_grant(body):
    auth, error = _user_token(['mcp:models:read'])
    if error:
        return None, None, error
    claims, oauth_session = auth
    grant_public_id = body.get('grant_public_id')
    if not isinstance(grant_public_id, str) or not grant_public_id.strip():
        return None, oauth_session, (jsonify({'error': 'grant_public_id_required'}), 400)
    grant, reason = resolve_grant(
        int(claims['sub']), grant_public_id, 'mcp.model.schema.read'
    )
    if grant is None:
        audit(
            'mcp.access.denied', oauth_session=oauth_session, outcome='denied',
            details={'reason': reason, 'grant_public_id': grant_public_id},
        )
        db.session.commit()
        status = 403 if reason == 'permission_denied' else 404
        return None, oauth_session, (jsonify({'error': reason}), status)
    return grant, oauth_session, None


def _skill_context_audit_details(resolution=None):
    if resolution is None:
        return {}
    return {
        'report_context_source': resolution.source,
        'report_association_count': resolution.association_count,
    }


@bp.route('/list-skills', methods=['POST'])
@csrf.exempt
@limiter.limit('300 per minute')
@_service_required
def list_skills():
    body = request.get_json(silent=True) or {}
    grant, oauth_session, error = _authorized_skill_grant(body)
    if error:
        return error
    domain_key = body.get('domain_key')
    if domain_key is not None and not isinstance(domain_key, str):
        return jsonify({'error': 'invalid_domain_key'}), 400

    resolution = None
    try:
        resolution = resolve_mcp_skill_scope(grant.config, grant.empresa_id)
        result = list_grant_skills(
            grant, domain_key=domain_key, resolution=resolution
        )
        audit(
            'mcp.skills.listed', oauth_session=oauth_session, grant=grant,
            details={
                'count': len(result['skills']),
                'domain_filtered': bool(domain_key),
                **_skill_context_audit_details(resolution),
            },
        )
        db.session.commit()
        return jsonify(result), 200, {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}
    except SkillReportContextError as exc:
        db.session.rollback()
        current_app.logger.warning(
            'MCP skill report context could not be resolved: source=%s associations=%s',
            exc.source,
            exc.association_count,
        )
        audit(
            'mcp.skills.list_failed', oauth_session=oauth_session, grant=grant,
            outcome='failure',
            details={
                'error_code': exc.code,
                'report_context_source': exc.source,
                'report_association_count': exc.association_count,
            },
        )
        db.session.commit()
        return jsonify({'error': exc.code, 'error_description': str(exc)}), 409
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to list MCP analytics skills')
        audit(
            'mcp.skills.list_failed', oauth_session=oauth_session, grant=grant,
            outcome='failure',
            details={
                'error_code': 'internal_error',
                **_skill_context_audit_details(resolution),
            },
        )
        db.session.commit()
        return jsonify({'error': 'skill_catalog_unavailable'}), 503


@bp.route('/select-skills', methods=['POST'])
@csrf.exempt
@limiter.limit('60 per minute')
@_service_required
def select_skills():
    body = request.get_json(silent=True) or {}
    grant, oauth_session, error = _authorized_skill_grant(body)
    if error:
        return error

    resolution = None
    try:
        resolution = resolve_mcp_skill_scope(grant.config, grant.empresa_id)
        result = asyncio.run(
            select_grant_skills(
                grant,
                question=body.get('question'),
                candidate_skill_keys=body.get('candidate_skill_keys'),
                resolution=resolution,
            )
        )
        audit(
            'mcp.skills.selected', oauth_session=oauth_session, grant=grant,
            details={
                'count': len(result['skills']),
                'matched': result['selection']['matched'],
                'truncated': result['truncated'],
                'candidate_filter': body.get('candidate_skill_keys') is not None,
                **_skill_context_audit_details(resolution),
            },
        )
        db.session.commit()
        return jsonify(result), 200, {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}
    except ValueError as exc:
        db.session.rollback()
        audit(
            'mcp.skills.selection_failed', oauth_session=oauth_session, grant=grant,
            outcome='failure',
            details={
                'error_code': 'invalid_request',
                **_skill_context_audit_details(resolution),
            },
        )
        db.session.commit()
        return jsonify({'error': 'invalid_skill_request', 'error_description': str(exc)}), 400
    except SkillReportContextError as exc:
        db.session.rollback()
        current_app.logger.warning(
            'MCP skill report context could not be resolved: source=%s associations=%s',
            exc.source,
            exc.association_count,
        )
        audit(
            'mcp.skills.selection_failed', oauth_session=oauth_session, grant=grant,
            outcome='failure',
            details={
                'error_code': exc.code,
                'report_context_source': exc.source,
                'report_association_count': exc.association_count,
            },
        )
        db.session.commit()
        return jsonify({'error': exc.code, 'error_description': str(exc)}), 409
    except SkillSelectionProviderError:
        audit(
            'mcp.skills.selection_failed', oauth_session=oauth_session, grant=grant,
            outcome='failure',
            details={
                'error_code': 'provider_error',
                **_skill_context_audit_details(resolution),
            },
        )
        db.session.commit()
        return jsonify({'error': 'skill_selection_unavailable'}), 503
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to select MCP analytics skills')
        audit(
            'mcp.skills.selection_failed', oauth_session=oauth_session, grant=grant,
            outcome='failure',
            details={
                'error_code': 'internal_error',
                **_skill_context_audit_details(resolution),
            },
        )
        db.session.commit()
        return jsonify({'error': 'skill_selection_unavailable'}), 503


@bp.route('/relevant-schema', methods=['POST'])
@csrf.exempt
@limiter.limit('60 per minute')
@_service_required
def relevant_schema():
    body = request.get_json(silent=True) or {}
    grant, oauth_session, error = _authorized_schema_grant(body)
    if error:
        return error

    resolution = None
    try:
        resolution = resolve_mcp_skill_scope(grant.config, grant.empresa_id)
        result, selected_skill_count = asyncio.run(
            get_grant_relevant_schema(
                grant,
                question=body.get('question'),
                selected_skill_keys=body.get('selected_skill_keys'),
                resolution=resolution,
            )
        )
        retrieval = result['retrieval']
        audit(
            'mcp.schema.relevant_retrieved',
            oauth_session=oauth_session,
            grant=grant,
            details={
                'table_count': retrieval['table_count'],
                'measure_count': retrieval['measure_count'],
                'matched': retrieval['matched'],
                'selected_skill_count': selected_skill_count,
                'missing_required_count': len(result['missing_required_items']),
                'truncated': retrieval['truncated'],
                'fallback_recommended': retrieval['fallback_recommended'],
                **_skill_context_audit_details(resolution),
            },
        )
        db.session.commit()
        return jsonify(result), 200, {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}
    except ValueError as exc:
        db.session.rollback()
        audit(
            'mcp.schema.relevant_failed', oauth_session=oauth_session, grant=grant,
            outcome='failure',
            details={
                'error_code': 'invalid_request',
                **_skill_context_audit_details(resolution),
            },
        )
        db.session.commit()
        return jsonify({
            'error': 'invalid_relevant_schema_request',
            'error_description': str(exc),
        }), 400
    except SkillReportContextError as exc:
        db.session.rollback()
        audit(
            'mcp.schema.relevant_failed', oauth_session=oauth_session, grant=grant,
            outcome='failure',
            details={
                'error_code': exc.code,
                'report_context_source': exc.source,
                'report_association_count': exc.association_count,
            },
        )
        db.session.commit()
        return jsonify({'error': exc.code, 'error_description': str(exc)}), 409
    except SchemaEmbeddingsUnavailable:
        db.session.rollback()
        audit(
            'mcp.schema.relevant_failed', oauth_session=oauth_session, grant=grant,
            outcome='failure',
            details={
                'error_code': 'schema_embeddings_unavailable',
                **_skill_context_audit_details(resolution),
            },
        )
        db.session.commit()
        return jsonify({
            'error': 'schema_embeddings_unavailable',
            'error_description': (
                'No hay un indice de schema disponible; usa get_powerbi_schema.'
            ),
        }), 409
    except RelevantSchemaProviderError:
        audit(
            'mcp.schema.relevant_failed', oauth_session=oauth_session, grant=grant,
            outcome='failure',
            details={
                'error_code': 'relevant_schema_unavailable',
                **_skill_context_audit_details(resolution),
            },
        )
        db.session.commit()
        return jsonify({
            'error': 'relevant_schema_unavailable',
            'error_description': (
                'No se pudo consultar el indice de schema; usa get_powerbi_schema.'
            ),
        }), 503
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to retrieve relevant MCP schema')
        audit(
            'mcp.schema.relevant_failed', oauth_session=oauth_session, grant=grant,
            outcome='failure',
            details={
                'error_code': 'internal_error',
                **_skill_context_audit_details(resolution),
            },
        )
        db.session.commit()
        return jsonify({'error': 'relevant_schema_unavailable'}), 503


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
