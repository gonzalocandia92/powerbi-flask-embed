"""Add allow_refresh column to public_links

Revision ID: add_allow_refresh_001
Revises: refactor_domain_001
Create Date: 2026-04-14

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = 'add_allow_refresh_001'
down_revision = 'refactor_domain_001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'public_links',
        sa.Column('allow_refresh', sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade():
    bind = op.get_bind()
    is_sqlite = 'sqlite' in str(bind.engine.url)

    if not is_sqlite:
        op.drop_column('public_links', 'allow_refresh')
    # SQLite does not support DROP COLUMN in older versions; migration is a no-op on downgrade for SQLite
