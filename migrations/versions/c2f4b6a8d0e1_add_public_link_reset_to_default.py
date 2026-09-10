"""Add allow_reset_to_default column to public_links

Revision ID: c2f4b6a8d0e1
Revises: b0d5f8a3c7e2
Create Date: 2026-09-09

"""
from alembic import op
import sqlalchemy as sa


revision = 'c2f4b6a8d0e1'
down_revision = 'b0d5f8a3c7e2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'public_links',
        sa.Column('allow_reset_to_default', sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade():
    bind = op.get_bind()
    is_sqlite = 'sqlite' in str(bind.engine.url)

    if not is_sqlite:
        op.drop_column('public_links', 'allow_reset_to_default')
