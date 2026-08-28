"""add MCP OAuth sessions and company-scoped model grants

Revision ID: e7f8a9b0c1d2
Revises: c31b032b9034
Create Date: 2026-07-31
"""

from datetime import datetime, timezone
import uuid

from alembic import op
import sqlalchemy as sa


revision = 'e7f8a9b0c1d2'
down_revision = 'c31b032b9034'
branch_labels = None
depends_on = None
ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')


def _now():
    return datetime.now(timezone.utc)


def upgrade():
    bind = op.get_bind()

    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column(
            'is_admin', existing_type=sa.Boolean(), nullable=False, server_default=sa.false()
        )

    with op.batch_alter_table('mcp_agent_configs') as batch_op:
        batch_op.add_column(sa.Column('public_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('model_key', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('description', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('domain', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('workspace_id_fk', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('credential_report_id_fk', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))
        batch_op.create_foreign_key(
            'fk_mcp_agent_configs_workspace', 'workspaces', ['workspace_id_fk'], ['id'], ondelete='SET NULL'
        )
        batch_op.create_foreign_key(
            'fk_mcp_agent_configs_credential_report', 'reports', ['credential_report_id_fk'], ['id'], ondelete='SET NULL'
        )

    configs = sa.table(
        'mcp_agent_configs',
        sa.column('id', sa.BigInteger()),
        sa.column('public_id', sa.String()),
        sa.column('model_key', sa.String()),
        sa.column('dataset_id', sa.String()),
        sa.column('updated_at', sa.DateTime()),
    )
    for row in bind.execute(sa.select(configs.c.id, configs.c.dataset_id)):
        bind.execute(
            configs.update().where(configs.c.id == row.id).values(
                public_id=str(uuid.uuid4()), model_key=row.dataset_id, updated_at=_now()
            )
        )

    with op.batch_alter_table('mcp_agent_configs') as batch_op:
        batch_op.alter_column('public_id', existing_type=sa.String(length=36), nullable=False)
        batch_op.alter_column('updated_at', existing_type=sa.DateTime(), nullable=False)
        batch_op.create_index('ix_mcp_agent_configs_public_id', ['public_id'], unique=True)
        batch_op.create_index('ix_mcp_agent_configs_model_key', ['model_key'], unique=False)

    bind.execute(sa.text(
        "UPDATE mcp_agent_configs SET workspace_id_fk = ("
        "SELECT MIN(workspaces.id) FROM workspaces WHERE workspaces.workspace_id = mcp_agent_configs.workspace_id"
        ") WHERE workspace_id_fk IS NULL"
    ))
    bind.execute(sa.text(
        "UPDATE mcp_agent_configs SET credential_report_id_fk = ("
        "SELECT MIN(reports.id) FROM reports JOIN workspaces ON workspaces.id = reports.workspace_id_fk "
        "WHERE workspaces.workspace_id = mcp_agent_configs.workspace_id"
        ") WHERE credential_report_id_fk IS NULL"
    ))

    op.create_table(
        'user_empresa',
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('empresa_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['empresa_id'], ['clientes_privados.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'empresa_id'),
    )
    op.create_table(
        'mcp_config_empresas',
        sa.Column('config_id', sa.BigInteger(), nullable=False),
        sa.Column('empresa_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['config_id'], ['mcp_agent_configs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['empresa_id'], ['clientes_privados.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('config_id', 'empresa_id'),
    )

    # Preserve legacy company assignments as explicit model/company links.
    bind.execute(sa.text(
        "INSERT INTO mcp_config_empresas (config_id, empresa_id, created_at) "
        "SELECT id, empresa_id, CURRENT_TIMESTAMP FROM mcp_agent_configs WHERE empresa_id IS NOT NULL"
    ))

    op.create_table(
        'mcp_model_roles',
        sa.Column('id', ID_TYPE, autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_index('ix_mcp_model_roles_name', 'mcp_model_roles', ['name'], unique=True)
    op.create_table(
        'mcp_model_role_permissions',
        sa.Column('role_id', sa.BigInteger(), nullable=False),
        sa.Column('permission_id', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(['role_id'], ['mcp_model_roles.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['permission_id'], ['permissions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('role_id', 'permission_id'),
    )
    op.create_table(
        'mcp_model_grants',
        sa.Column('id', ID_TYPE, autoincrement=True, nullable=False),
        sa.Column('public_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('config_id', sa.BigInteger(), nullable=False),
        sa.Column('empresa_id', sa.BigInteger(), nullable=False),
        sa.Column('role_id', sa.BigInteger(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['config_id'], ['mcp_agent_configs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['empresa_id'], ['clientes_privados.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['role_id'], ['mcp_model_roles.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('public_id'),
        sa.UniqueConstraint('user_id', 'config_id', 'empresa_id', name='uq_mcp_grant_user_config_empresa'),
    )
    op.create_index('ix_mcp_model_grants_public_id', 'mcp_model_grants', ['public_id'], unique=True)

    op.create_table(
        'mcp_oauth_clients',
        sa.Column('id', ID_TYPE, autoincrement=True, nullable=False),
        sa.Column('client_id', sa.String(length=120), nullable=False),
        sa.Column('client_secret_hash', sa.String(length=256), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('redirect_uris', sa.JSON(), nullable=False),
        sa.Column('allowed_scopes', sa.JSON(), nullable=False),
        sa.Column('grant_types', sa.JSON(), nullable=False),
        sa.Column('response_types', sa.JSON(), nullable=False),
        sa.Column('token_endpoint_auth_method', sa.String(length=40), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('client_id'),
    )
    op.create_index('ix_mcp_oauth_clients_client_id', 'mcp_oauth_clients', ['client_id'], unique=True)
    op.create_table(
        'mcp_oauth_authorization_requests',
        sa.Column('id', ID_TYPE, autoincrement=True, nullable=False),
        sa.Column('request_id', sa.String(length=64), nullable=False),
        sa.Column('client_id', sa.String(length=120), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('request_uri', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_id'),
    )
    op.create_index('ix_mcp_oauth_authorization_requests_request_id', 'mcp_oauth_authorization_requests', ['request_id'], unique=True)
    op.create_index('ix_mcp_oauth_authorization_requests_client_id', 'mcp_oauth_authorization_requests', ['client_id'])
    op.create_table(
        'mcp_authorization_codes',
        sa.Column('id', ID_TYPE, autoincrement=True, nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('client_id', sa.String(length=120), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('redirect_uri', sa.Text(), nullable=True),
        sa.Column('scope', sa.Text(), nullable=True),
        sa.Column('nonce', sa.String(length=255), nullable=True),
        sa.Column('code_challenge', sa.String(length=255), nullable=True),
        sa.Column('code_challenge_method', sa.String(length=20), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code_hash'),
    )
    op.create_index('ix_mcp_authorization_codes_code_hash', 'mcp_authorization_codes', ['code_hash'], unique=True)
    op.create_index('ix_mcp_authorization_codes_client_id', 'mcp_authorization_codes', ['client_id'])
    op.create_table(
        'mcp_oauth_sessions',
        sa.Column('id', ID_TYPE, autoincrement=True, nullable=False),
        sa.Column('public_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('client_id', sa.String(length=120), nullable=False),
        sa.Column('scope', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoke_reason', sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('public_id'),
    )
    op.create_index('ix_mcp_oauth_sessions_public_id', 'mcp_oauth_sessions', ['public_id'], unique=True)
    op.create_index('ix_mcp_oauth_sessions_client_id', 'mcp_oauth_sessions', ['client_id'])
    op.create_index('ix_mcp_oauth_sessions_revoked_at', 'mcp_oauth_sessions', ['revoked_at'])
    op.create_table(
        'mcp_refresh_tokens',
        sa.Column('id', ID_TYPE, autoincrement=True, nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('session_id', sa.BigInteger(), nullable=False),
        sa.Column('client_id', sa.String(length=120), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('scope', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('replaced_by_hash', sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(['session_id'], ['mcp_oauth_sessions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_mcp_refresh_tokens_token_hash', 'mcp_refresh_tokens', ['token_hash'], unique=True)
    op.create_index('ix_mcp_refresh_tokens_client_id', 'mcp_refresh_tokens', ['client_id'])
    op.create_table(
        'mcp_security_audit_log',
        sa.Column('id', ID_TYPE, autoincrement=True, nullable=False),
        sa.Column('event_type', sa.String(length=120), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('client_id', sa.String(length=120), nullable=True),
        sa.Column('session_public_id', sa.String(length=36), nullable=True),
        sa.Column('grant_public_id', sa.String(length=36), nullable=True),
        sa.Column('empresa_id', sa.BigInteger(), nullable=True),
        sa.Column('outcome', sa.String(length=40), nullable=False),
        sa.Column('details', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['empresa_id'], ['clientes_privados.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    for column in ('event_type', 'client_id', 'session_public_id', 'grant_public_id', 'created_at'):
        op.create_index(f'ix_mcp_security_audit_log_{column}', 'mcp_security_audit_log', [column])

    permissions = sa.table(
        'permissions', sa.column('id', sa.BigInteger()), sa.column('name', sa.String()),
        sa.column('description', sa.String()), sa.column('created_at', sa.DateTime())
    )
    permission_rows = [
        ('backoffice.access', 'Acceder al backoffice'),
        ('mcp.config.manage', 'Administrar modelos MCP'),
        ('mcp.oauth_clients.manage', 'Administrar clientes OAuth MCP'),
        ('mcp.access.manage', 'Administrar grants MCP'),
        ('mcp.model.schema.read', 'Leer esquema de un modelo'),
        ('mcp.model.query.execute', 'Ejecutar consultas sobre un modelo'),
        ('mcp.model.measure.create', 'Crear medidas en un modelo'),
        ('mcp.model.measure.update', 'Actualizar medidas en un modelo'),
    ]
    for name, description in permission_rows:
        if not bind.execute(sa.select(permissions.c.id).where(permissions.c.name == name)).first():
            bind.execute(permissions.insert().values(name=name, description=description, created_at=_now()))

    roles = sa.table(
        'mcp_model_roles', sa.column('id', sa.BigInteger()), sa.column('name', sa.String()),
        sa.column('description', sa.String()), sa.column('created_at', sa.DateTime())
    )
    role_permissions = sa.table(
        'mcp_model_role_permissions', sa.column('role_id', sa.BigInteger()), sa.column('permission_id', sa.BigInteger())
    )
    role_definitions = {
        'reader': ['mcp.model.schema.read', 'mcp.model.query.execute'],
        'editor': ['mcp.model.schema.read', 'mcp.model.query.execute', 'mcp.model.measure.create', 'mcp.model.measure.update'],
    }
    for role_name, permission_names in role_definitions.items():
        bind.execute(roles.insert().values(name=role_name, description=f'MCP model {role_name}', created_at=_now()))
        role_id = bind.execute(sa.select(roles.c.id).where(roles.c.name == role_name)).scalar_one()
        for permission_name in permission_names:
            permission_id = bind.execute(sa.select(permissions.c.id).where(permissions.c.name == permission_name)).scalar_one()
            bind.execute(role_permissions.insert().values(role_id=role_id, permission_id=permission_id))


def downgrade():
    for table in (
        'mcp_security_audit_log', 'mcp_refresh_tokens', 'mcp_oauth_sessions',
        'mcp_authorization_codes', 'mcp_oauth_authorization_requests', 'mcp_oauth_clients',
        'mcp_model_grants', 'mcp_model_role_permissions', 'mcp_model_roles',
        'mcp_config_empresas', 'user_empresa',
    ):
        op.drop_table(table)

    with op.batch_alter_table('mcp_agent_configs') as batch_op:
        batch_op.drop_constraint('fk_mcp_agent_configs_credential_report', type_='foreignkey')
        batch_op.drop_constraint('fk_mcp_agent_configs_workspace', type_='foreignkey')
        for column in ('updated_at', 'credential_report_id_fk', 'workspace_id_fk', 'domain', 'description', 'model_key', 'public_id'):
            batch_op.drop_column(column)
