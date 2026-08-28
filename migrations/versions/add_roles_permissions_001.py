"""Add roles and permissions for user management

Revision ID: add_roles_permissions_001
Revises: 1512c11ce3b0
Create Date: 2026-06-29

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = 'add_roles_permissions_001'
down_revision = '1512c11ce3b0'
branch_labels = None
depends_on = None


def upgrade():
    # Create permissions table
    op.create_table(
        'permissions',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
    )
    op.create_index(op.f('ix_permissions_name'), 'permissions', ['name'], unique=False)

    # Create roles table
    op.create_table(
        'roles',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
    )
    op.create_index(op.f('ix_roles_name'), 'roles', ['name'], unique=False)

    # Create user_role association table
    op.create_table(
        'user_role',
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('role_id', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('user_id', 'role_id')
    )

    # Create role_permission association table
    op.create_table(
        'role_permission',
        sa.Column('role_id', sa.BigInteger(), nullable=False),
        sa.Column('permission_id', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(['permission_id'], ['permissions.id'], ),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ),
        sa.PrimaryKeyConstraint('role_id', 'permission_id')
    )

    # Modify users table: add new columns
    op.add_column('users', sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('users', sa.Column('created_at', sa.DateTime(), nullable=True))
    op.add_column('users', sa.Column('updated_at', sa.DateTime(), nullable=True))
    
    # Change is_admin default from True to False
    op.alter_column('users', 'is_admin', existing_type=sa.Boolean(), server_default=sa.false())


def downgrade():
    bind = op.get_bind()
    is_sqlite = 'sqlite' in str(bind.engine.url)

    # Remove columns from users table
    if not is_sqlite:
        op.drop_column('users', 'updated_at')
        op.drop_column('users', 'created_at')
        op.drop_column('users', 'is_active')
    
    # Reset is_admin default (SQLite has limitations)
    if not is_sqlite:
        op.alter_column('users', 'is_admin', existing_type=sa.Boolean(), server_default=sa.true())

    # Drop association tables
    op.drop_table('role_permission')
    op.drop_table('user_role')

    # Drop index and table for roles
    op.drop_index(op.f('ix_roles_name'), table_name='roles')
    op.drop_table('roles')

    # Drop index and table for permissions
    op.drop_index(op.f('ix_permissions_name'), table_name='permissions')
    op.drop_table('permissions')
