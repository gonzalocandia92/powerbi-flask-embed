"""add missing fields to clientes_privados

Revision ID: d40ed6098959
Revises: e0fcc42ec117
Create Date: 2026-01-10 12:25:30.253888

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd40ed6098959'
down_revision = 'e0fcc42ec117'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'clientes_privados',
        sa.Column('cuit', sa.String(length=20), nullable=True)
    )


def downgrade():
    op.drop_column('clientes_privados', 'cuit')
