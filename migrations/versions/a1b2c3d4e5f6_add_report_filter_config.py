"""add_report_filter_config

Revision ID: a1b2c3d4e5f6
Revises: f5c653ddb450
Create Date: 2026-06-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'f5c653ddb450'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [col['name'] for col in inspector.get_columns('reports')]

    if 'filter_enabled' not in columns:
        op.add_column('reports', sa.Column('filter_enabled', sa.Boolean(), nullable=False, server_default='0'))
    if 'filter_table' not in columns:
        op.add_column('reports', sa.Column('filter_table', sa.String(200), nullable=True))
    if 'filter_column' not in columns:
        op.add_column('reports', sa.Column('filter_column', sa.String(200), nullable=True))


def downgrade():
    op.drop_column('reports', 'filter_column')
    op.drop_column('reports', 'filter_table')
    op.drop_column('reports', 'filter_enabled')
