"""add_show_dax_query_to_reports

Revision ID: 77592430fed9
Revises: b1c2d3e4f5a6
Create Date: 2026-06-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = '77592430fed9'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = [c['name'] for c in inspect(bind).get_columns('reports')]
    if 'show_dax_query' not in columns:
        op.add_column('reports', sa.Column('show_dax_query', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    bind = op.get_bind()
    columns = [c['name'] for c in inspect(bind).get_columns('reports')]
    if 'show_dax_query' in columns:
        op.drop_column('reports', 'show_dax_query')
