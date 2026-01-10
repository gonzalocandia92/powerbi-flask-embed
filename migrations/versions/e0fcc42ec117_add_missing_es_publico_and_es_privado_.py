"""add missing es_publico and es_privado to report_configs

Revision ID: e0fcc42ec117
Revises: 117b07c24e37
Create Date: 2026-01-10 12:20:37.570670

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e0fcc42ec117'
down_revision = '117b07c24e37'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'report_configs',
        sa.Column(
            'es_publico',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true')
        )
    )
    op.add_column(
        'report_configs',
        sa.Column(
            'es_privado',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false')
        )
    )

def downgrade():
    op.drop_column('report_configs', 'es_privado')
    op.drop_column('report_configs', 'es_publico')
