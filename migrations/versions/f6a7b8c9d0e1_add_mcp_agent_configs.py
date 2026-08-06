"""add mcp agent configs

Revision ID: f6a7b8c9d0e1
Revises: 1512c11ce3b0
Create Date: 2026-07-23 18:05:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = 'f6a7b8c9d0e1'
down_revision = '1512c11ce3b0'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = inspector.get_table_names()

    if 'mcp_agent_configs' not in table_names:
        op.create_table(
            'mcp_agent_configs',
            sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
            sa.Column('api_key_hash', sa.String(length=256), nullable=False),
            sa.Column('workspace_id', sa.String(length=120), nullable=False),
            sa.Column('workspace_name', sa.String(length=200), nullable=False),
            sa.Column('dataset_id', sa.String(length=120), nullable=False),
            sa.Column('dataset_name', sa.String(length=200), nullable=False),
            sa.Column('empresa_id', sa.BigInteger(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.ForeignKeyConstraint(['empresa_id'], ['clientes_privados.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )

    indexes = {index['name'] for index in inspect(bind).get_indexes('mcp_agent_configs')}
    with op.batch_alter_table('mcp_agent_configs', schema=None) as batch_op:
        if 'ix_mcp_agent_configs_api_key_hash' not in indexes:
            batch_op.create_index('ix_mcp_agent_configs_api_key_hash', ['api_key_hash'], unique=True)
        if 'ix_mcp_agent_configs_empresa_id' not in indexes:
            batch_op.create_index('ix_mcp_agent_configs_empresa_id', ['empresa_id'], unique=False)

    if bind.dialect.name != 'sqlite':
        with op.batch_alter_table('mcp_agent_configs', schema=None) as batch_op:
            batch_op.alter_column('is_active', server_default=None)


def downgrade():
    bind = op.get_bind()
    if 'mcp_agent_configs' not in inspect(bind).get_table_names():
        return

    indexes = {index['name'] for index in inspect(bind).get_indexes('mcp_agent_configs')}
    with op.batch_alter_table('mcp_agent_configs', schema=None) as batch_op:
        if 'ix_mcp_agent_configs_empresa_id' in indexes:
            batch_op.drop_index('ix_mcp_agent_configs_empresa_id')
        if 'ix_mcp_agent_configs_api_key_hash' in indexes:
            batch_op.drop_index('ix_mcp_agent_configs_api_key_hash')

    op.drop_table('mcp_agent_configs')
