"""merge user management and MCP heads

Revision ID: c31b032b9034
Revises: a3b4c5d6e7f8, f6a7b8c9d0e1
Create Date: 2026-07-31 11:47:09.534803

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c31b032b9034'
down_revision = ('a3b4c5d6e7f8', 'f6a7b8c9d0e1')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
