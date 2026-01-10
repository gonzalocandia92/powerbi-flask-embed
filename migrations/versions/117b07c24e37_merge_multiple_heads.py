"""merge multiple heads

Revision ID: 117b07c24e37
Revises: 6f3e4fbb14a7, add_empresa_features
Create Date: 2026-01-10 12:12:56.367258

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '117b07c24e37'
down_revision = ('6f3e4fbb14a7', 'add_empresa_features')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
