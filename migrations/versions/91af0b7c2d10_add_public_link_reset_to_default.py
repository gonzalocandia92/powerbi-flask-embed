"""Add allow_reset_to_default column to public_links

Revision ID: 91af0b7c2d10
Revises: b8c9d0e1f2a3
Adapted from hotfix/reports revision c2f4b6a8d0e1; preserves the local KLARA history.
Create Date: 2026-09-09

Some databases already carry this column from the public-link hotfix even
though their Alembic head follows the local KLARA history. Keep its values
intact; the next revision moves them to reports.

"""
from alembic import op
import sqlalchemy as sa


revision = '91af0b7c2d10'
down_revision = 'b8c9d0e1f2a3'
branch_labels = None
depends_on = None


def upgrade():
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('public_links')}
    if 'allow_reset_to_default' in columns:
        return
    op.add_column(
        'public_links',
        sa.Column('allow_reset_to_default', sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade():
    with op.batch_alter_table('public_links') as batch_op:
        batch_op.drop_column('allow_reset_to_default')
