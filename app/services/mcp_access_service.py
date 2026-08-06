"""Company-scoped authorization resolution for MCP semantic models."""

from app.models import McpConfigEmpresa, McpModelGrant, UserEmpresa


MODEL_PERMISSIONS = {
    'mcp.model.schema.read',
    'mcp.model.query.execute',
    'mcp.model.measure.create',
    'mcp.model.measure.update',
}


def resolve_grant(user_id, grant_public_id, required_permission=None):
    grant = McpModelGrant.query.filter_by(
        public_id=grant_public_id, user_id=user_id, is_active=True
    ).first()
    if grant is None or not grant.config.is_active or not grant.empresa.estado_activo:
        return None, 'grant_not_found'

    membership = UserEmpresa.query.filter_by(user_id=user_id, empresa_id=grant.empresa_id).first()
    model_company = McpConfigEmpresa.query.filter_by(
        config_id=grant.config_id, empresa_id=grant.empresa_id
    ).first()
    if membership is None or model_company is None:
        return None, 'invalid_tenancy_context'

    permissions = {item.name for item in grant.role.permissions}
    if required_permission:
        if required_permission not in MODEL_PERMISSIONS:
            return None, 'invalid_permission'
        if required_permission not in permissions:
            return None, 'permission_denied'
    return grant, permissions


def list_user_grants(user_id):
    grants = McpModelGrant.query.filter_by(user_id=user_id, is_active=True).all()
    result = []
    for grant in grants:
        resolved, permissions = resolve_grant(user_id, grant.public_id)
        if resolved is not None:
            result.append((resolved, permissions))
    return result

