"""Add reports.requires_login and the reports.read permission

Public links of a report with requires_login ask for a login with an application
user. Access needs the reports.read permission plus membership of an empresa
associated with the report (backoffice users are always allowed).

Seeds the reports.read permission and a "Lector de reportes" role that holds it,
so it can be assigned from the user access screen. Both are created only when
missing.

Downgrade drops the column and removes the seeded role and permission together
with their assignments.

Revision ID: b3d8f2a6c1e9
Revises: a7c3e91b5d24
Create Date: 2026-10-06

"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = 'b3d8f2a6c1e9'
down_revision = 'a7c3e91b5d24'
branch_labels = None
depends_on = None

PERMISSION_NAME = 'reports.read'
PERMISSION_DESCRIPTION = 'Leer reportes públicos que requieren login'
ROLE_NAME = 'Lector de reportes'
ROLE_DESCRIPTION = 'Puede abrir los reportes con login de las empresas a las que pertenece. No da acceso al backoffice.'


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tables():
    permissions = sa.table(
        'permissions', sa.column('id'), sa.column('name'),
        sa.column('description'), sa.column('created_at'),
    )
    roles = sa.table(
        'roles', sa.column('id'), sa.column('name'),
        sa.column('description'), sa.column('created_at'),
    )
    role_permission = sa.table('role_permission', sa.column('role_id'), sa.column('permission_id'))
    user_role = sa.table('user_role', sa.column('user_id'), sa.column('role_id'))
    return permissions, roles, role_permission, user_role


def upgrade():
    with op.batch_alter_table('reports') as batch_op:
        batch_op.add_column(
            sa.Column('requires_login', sa.Boolean(), nullable=False, server_default=sa.false())
        )

    bind = op.get_bind()
    permissions, roles, role_permission, _ = _tables()

    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.name == PERMISSION_NAME)
    ).scalar()
    if permission_id is None:
        bind.execute(permissions.insert().values(
            name=PERMISSION_NAME, description=PERMISSION_DESCRIPTION, created_at=_now()
        ))
        permission_id = bind.execute(
            sa.select(permissions.c.id).where(permissions.c.name == PERMISSION_NAME)
        ).scalar_one()

    role_id = bind.execute(sa.select(roles.c.id).where(roles.c.name == ROLE_NAME)).scalar()
    if role_id is None:
        bind.execute(roles.insert().values(
            name=ROLE_NAME, description=ROLE_DESCRIPTION, created_at=_now()
        ))
        role_id = bind.execute(sa.select(roles.c.id).where(roles.c.name == ROLE_NAME)).scalar_one()

    already_linked = bind.execute(
        sa.select(role_permission.c.role_id).where(
            role_permission.c.role_id == role_id,
            role_permission.c.permission_id == permission_id,
        )
    ).first()
    if not already_linked:
        bind.execute(role_permission.insert().values(role_id=role_id, permission_id=permission_id))


def downgrade():
    bind = op.get_bind()
    permissions, roles, role_permission, user_role = _tables()

    role_id = bind.execute(sa.select(roles.c.id).where(roles.c.name == ROLE_NAME)).scalar()
    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.name == PERMISSION_NAME)
    ).scalar()

    if role_id is not None:
        bind.execute(user_role.delete().where(user_role.c.role_id == role_id))
        bind.execute(role_permission.delete().where(role_permission.c.role_id == role_id))
        bind.execute(roles.delete().where(roles.c.id == role_id))
    if permission_id is not None:
        bind.execute(role_permission.delete().where(role_permission.c.permission_id == permission_id))
        bind.execute(permissions.delete().where(permissions.c.id == permission_id))

    with op.batch_alter_table('reports') as batch_op:
        batch_op.drop_column('requires_login')
