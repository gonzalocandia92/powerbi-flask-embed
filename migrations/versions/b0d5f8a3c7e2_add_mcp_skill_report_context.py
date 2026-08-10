"""add explicit MCP skill report context

Revision ID: b0d5f8a3c7e2
Revises: a9c4e7f2b6d1
"""

from alembic import op
import sqlalchemy as sa


revision = 'b0d5f8a3c7e2'
down_revision = 'a9c4e7f2b6d1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('mcp_agent_configs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('skill_report_id_fk', sa.BigInteger(), nullable=True))
        batch_op.create_index(
            'ix_mcp_agent_configs_skill_report_id_fk',
            ['skill_report_id_fk'],
            unique=False,
        )
        batch_op.create_foreign_key(
            'fk_mcp_agent_configs_skill_report',
            'reports',
            ['skill_report_id_fk'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade():
    with op.batch_alter_table('mcp_agent_configs', schema=None) as batch_op:
        batch_op.drop_constraint(
            'fk_mcp_agent_configs_skill_report', type_='foreignkey'
        )
        batch_op.drop_index('ix_mcp_agent_configs_skill_report_id_fk')
        batch_op.drop_column('skill_report_id_fk')
