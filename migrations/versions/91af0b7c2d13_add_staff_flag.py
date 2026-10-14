"""Add the staff flag to users and the activity tables

users.is_staff marks Sudata staff. Chat sessions, MCP interactions and visits
store a snapshot of that flag when they are recorded, so internal activity can
be kept apart from client activity without depending on the current value of
the user flag.

Revision ID: 91af0b7c2d13
Revises: 91af0b7c2d12
Create Date: 2026-10-14

"""
from alembic import op
import sqlalchemy as sa


revision = '91af0b7c2d13'
down_revision = '91af0b7c2d12'
branch_labels = None
depends_on = None

TABLES = ('users', 'chat_sessions', 'mcp_interactions', 'visits')


def upgrade():
    for table in TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column('is_staff', sa.Boolean(), nullable=False, server_default=sa.false())
            )


def downgrade():
    for table in reversed(TABLES):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column('is_staff')
