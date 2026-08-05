"""bind MCP OAuth grants and tokens to their canonical resource

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = 'f8a9b0c1d2e3'
down_revision = 'e7f8a9b0c1d2'
branch_labels = None
depends_on = None


LEGACY_RESOURCE = 'urn:mcp-aklara:legacy'
LEGACY_FAMILY = '00000000-0000-0000-0000-000000000000'


def upgrade():
    op.add_column(
        'mcp_oauth_authorization_requests',
        sa.Column('resource', sa.String(length=2048), nullable=True),
    )
    op.add_column(
        'mcp_authorization_codes',
        sa.Column('resource', sa.String(length=2048), nullable=True),
    )
    op.add_column(
        'mcp_oauth_sessions',
        sa.Column('resource', sa.String(length=2048), nullable=True),
    )
    op.add_column(
        'mcp_refresh_tokens',
        sa.Column('family_id', sa.String(length=36), nullable=True),
    )
    op.add_column(
        'mcp_refresh_tokens',
        sa.Column('resource', sa.String(length=2048), nullable=True),
    )

    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    bind.execute(sa.text('DELETE FROM mcp_oauth_authorization_requests'))
    bind.execute(sa.text('DELETE FROM mcp_authorization_codes'))
    bind.execute(
        sa.text(
            'UPDATE mcp_oauth_sessions '
            'SET resource = :resource, revoked_at = COALESCE(revoked_at, :now), '
            "revoke_reason = COALESCE(revoke_reason, 'resource_binding_migration')"
        ),
        {'resource': LEGACY_RESOURCE, 'now': now},
    )
    bind.execute(
        sa.text(
            'UPDATE mcp_refresh_tokens '
            'SET resource = :resource, family_id = :family_id, '
            'revoked_at = COALESCE(revoked_at, :now)'
        ),
        {'resource': LEGACY_RESOURCE, 'family_id': LEGACY_FAMILY, 'now': now},
    )

    with op.batch_alter_table('mcp_oauth_authorization_requests') as batch_op:
        batch_op.alter_column(
            'resource', existing_type=sa.String(length=2048), nullable=False
        )
    with op.batch_alter_table('mcp_authorization_codes') as batch_op:
        batch_op.alter_column(
            'resource', existing_type=sa.String(length=2048), nullable=False
        )
    with op.batch_alter_table('mcp_oauth_sessions') as batch_op:
        batch_op.alter_column(
            'resource', existing_type=sa.String(length=2048), nullable=False
        )
    with op.batch_alter_table('mcp_refresh_tokens') as batch_op:
        batch_op.alter_column(
            'family_id', existing_type=sa.String(length=36), nullable=False
        )
        batch_op.alter_column(
            'resource', existing_type=sa.String(length=2048), nullable=False
        )

    op.create_index(
        'ix_mcp_oauth_sessions_resource', 'mcp_oauth_sessions', ['resource']
    )
    op.create_index(
        'ix_mcp_refresh_tokens_family_id', 'mcp_refresh_tokens', ['family_id']
    )


def downgrade():
    op.drop_index('ix_mcp_refresh_tokens_family_id', table_name='mcp_refresh_tokens')
    op.drop_index('ix_mcp_oauth_sessions_resource', table_name='mcp_oauth_sessions')
    with op.batch_alter_table('mcp_refresh_tokens') as batch_op:
        batch_op.drop_column('resource')
        batch_op.drop_column('family_id')
    with op.batch_alter_table('mcp_oauth_sessions') as batch_op:
        batch_op.drop_column('resource')
    with op.batch_alter_table('mcp_authorization_codes') as batch_op:
        batch_op.drop_column('resource')
    with op.batch_alter_table('mcp_oauth_authorization_requests') as batch_op:
        batch_op.drop_column('resource')
