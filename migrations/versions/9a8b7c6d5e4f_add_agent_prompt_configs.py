"""add agent prompt configs

Revision ID: 9a8b7c6d5e4f
Revises: 6c7d8e9f0a1b
Create Date: 2026-06-24 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = '9a8b7c6d5e4f'
down_revision = '6c7d8e9f0a1b'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing_tables = set(inspect(bind).get_table_names())

    if "agent_prompt_configs" not in existing_tables:
        op.create_table(
            "agent_prompt_configs",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("scope_type", sa.String(length=20), nullable=False),
            sa.Column("scope_id", sa.String(length=120), nullable=True),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("instructions", sa.Text(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("starts_at", sa.DateTime(), nullable=True),
            sa.Column("ends_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("agent_prompt_configs", schema=None) as batch_op:
            batch_op.create_index("ix_agent_prompt_configs_scope_type", ["scope_type"], unique=False)
            batch_op.create_index("ix_agent_prompt_configs_scope_id", ["scope_id"], unique=False)
            batch_op.create_index("ix_agent_prompt_configs_is_active", ["is_active"], unique=False)
            batch_op.create_index("ix_agent_prompt_configs_scope", ["scope_type", "scope_id"], unique=False)


def downgrade():
    bind = op.get_bind()
    existing_tables = set(inspect(bind).get_table_names())

    if "agent_prompt_configs" in existing_tables:
        with op.batch_alter_table("agent_prompt_configs", schema=None) as batch_op:
            batch_op.drop_index("ix_agent_prompt_configs_scope")
            batch_op.drop_index("ix_agent_prompt_configs_is_active")
            batch_op.drop_index("ix_agent_prompt_configs_scope_id")
            batch_op.drop_index("ix_agent_prompt_configs_scope_type")
        op.drop_table("agent_prompt_configs")
