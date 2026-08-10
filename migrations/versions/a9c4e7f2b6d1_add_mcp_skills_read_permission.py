"""add MCP analytics skills read permission

Revision ID: a9c4e7f2b6d1
Revises: f8a9b0c1d2e3
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = 'a9c4e7f2b6d1'
down_revision = 'f8a9b0c1d2e3'
branch_labels = None
depends_on = None


PERMISSION_NAME = 'mcp.model.skills.read'


def upgrade():
    bind = op.get_bind()
    permissions = sa.table(
        'permissions',
        sa.column('id', sa.BigInteger()),
        sa.column('name', sa.String()),
        sa.column('description', sa.String()),
        sa.column('created_at', sa.DateTime()),
    )
    roles = sa.table(
        'mcp_model_roles',
        sa.column('id', sa.BigInteger()),
        sa.column('name', sa.String()),
    )
    role_permissions = sa.table(
        'mcp_model_role_permissions',
        sa.column('role_id', sa.BigInteger()),
        sa.column('permission_id', sa.BigInteger()),
    )

    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.name == PERMISSION_NAME)
    ).scalar_one_or_none()
    if permission_id is None:
        bind.execute(
            permissions.insert().values(
                name=PERMISSION_NAME,
                description='Leer skills analiticas de un modelo',
                created_at=datetime.now(timezone.utc),
            )
        )
        permission_id = bind.execute(
            sa.select(permissions.c.id).where(permissions.c.name == PERMISSION_NAME)
        ).scalar_one()

    role_rows = bind.execute(
        sa.select(roles.c.id).where(roles.c.name.in_(['reader', 'editor']))
    ).all()
    for role_id, in role_rows:
        existing = bind.execute(
            sa.select(role_permissions.c.role_id).where(
                role_permissions.c.role_id == role_id,
                role_permissions.c.permission_id == permission_id,
            )
        ).first()
        if existing is None:
            bind.execute(
                role_permissions.insert().values(
                    role_id=role_id,
                    permission_id=permission_id,
                )
            )


def downgrade():
    bind = op.get_bind()
    permissions = sa.table(
        'permissions',
        sa.column('id', sa.BigInteger()),
        sa.column('name', sa.String()),
    )
    role_permissions = sa.table(
        'mcp_model_role_permissions',
        sa.column('role_id', sa.BigInteger()),
        sa.column('permission_id', sa.BigInteger()),
    )
    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.name == PERMISSION_NAME)
    ).scalar_one_or_none()
    if permission_id is not None:
        bind.execute(
            role_permissions.delete().where(
                role_permissions.c.permission_id == permission_id
            )
        )
        bind.execute(permissions.delete().where(permissions.c.id == permission_id))
