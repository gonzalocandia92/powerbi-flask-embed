"""Add email column to users table

Revision ID: a3b4c5d6e7f8
Revises: add_roles_permissions_001
Create Date: 2026-07-23

"""
from alembic import op
import sqlalchemy as sa

revision = 'a3b4c5d6e7f8'
down_revision = 'add_roles_permissions_001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('email', sa.String(254), nullable=True))
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    # Allow password_hash to be nullable (for Google-only users)
    op.alter_column('users', 'password_hash', existing_type=sa.String(256), nullable=True)


def downgrade():
    op.alter_column('users', 'password_hash', existing_type=sa.String(256), nullable=False)
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_column('users', 'email')
