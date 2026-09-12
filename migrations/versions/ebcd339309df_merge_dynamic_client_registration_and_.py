"""merge dynamic client registration and public links heads

Revision ID: ebcd339309df
Revises: c2d3e4f5a6b7, c2f4b6a8d0e1
Create Date: 2026-09-11 19:27:58.166506

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ebcd339309df'
down_revision = ('c2d3e4f5a6b7', 'c2f4b6a8d0e1')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
