"""add report schema retrieval config

Revision ID: ab12cd34ef56
Revises: 9a8b7c6d5e4f
Create Date: 2026-06-25 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = 'ab12cd34ef56'
down_revision = '9a8b7c6d5e4f'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing_columns = {column["name"] for column in inspect(bind).get_columns("reports")}

    with op.batch_alter_table("reports", schema=None) as batch_op:
        if "schema_retrieval_prompt" not in existing_columns:
            batch_op.add_column(sa.Column("schema_retrieval_prompt", sa.Text(), nullable=True))
        if "schema_table_context_limit" not in existing_columns:
            batch_op.add_column(sa.Column("schema_table_context_limit", sa.Integer(), nullable=True))
        if "schema_measure_context_limit" not in existing_columns:
            batch_op.add_column(sa.Column("schema_measure_context_limit", sa.Integer(), nullable=True))


def downgrade():
    bind = op.get_bind()
    existing_columns = {column["name"] for column in inspect(bind).get_columns("reports")}

    with op.batch_alter_table("reports", schema=None) as batch_op:
        if "schema_measure_context_limit" in existing_columns:
            batch_op.drop_column("schema_measure_context_limit")
        if "schema_table_context_limit" in existing_columns:
            batch_op.drop_column("schema_table_context_limit")
        if "schema_retrieval_prompt" in existing_columns:
            batch_op.drop_column("schema_retrieval_prompt")
