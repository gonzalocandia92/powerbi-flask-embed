"""add_private_filter_columns_to_reports

Revision ID: b1c2d3e4f5a6
Revises: 8e2b7c6d4f10
Create Date: 2026-06-06 18:40:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = 'b1c2d3e4f5a6'
down_revision = '8e2b7c6d4f10'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    column_names = {col["name"] for col in inspector.get_columns("reports")}

    with op.batch_alter_table('reports', schema=None) as batch_op:
        if 'filter_enabled' not in column_names:
            batch_op.add_column(sa.Column('filter_enabled', sa.Boolean(), nullable=False, server_default='false'))
        if 'filter_table' not in column_names:
            batch_op.add_column(sa.Column('filter_table', sa.String(length=200), nullable=True))
        if 'filter_column' not in column_names:
            batch_op.add_column(sa.Column('filter_column', sa.String(length=200), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    column_names = {col["name"] for col in inspector.get_columns("reports")}

    with op.batch_alter_table('reports', schema=None) as batch_op:
        if 'filter_column' in column_names:
            batch_op.drop_column('filter_column')
        if 'filter_table' in column_names:
            batch_op.drop_column('filter_table')
        if 'filter_enabled' in column_names:
            batch_op.drop_column('filter_enabled')
