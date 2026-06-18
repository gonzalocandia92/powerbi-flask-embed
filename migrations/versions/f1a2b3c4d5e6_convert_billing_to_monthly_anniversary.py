"""convert billing to monthly anniversary

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-06-11 10:00:00.000000

"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = 'f1a2b3c4d5e6'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("billing_limits")}

    with op.batch_alter_table("billing_limits", schema=None) as batch_op:
        if "cycle_anchor_day" not in columns:
            batch_op.add_column(sa.Column("cycle_anchor_day", sa.Integer(), nullable=True, server_default="1"))

    billing_limits = sa.table(
        "billing_limits",
        sa.column("id", sa.BigInteger()),
        sa.column("period_type", sa.String()),
        sa.column("cycle_anchor_day", sa.Integer()),
        sa.column("starts_at", sa.DateTime()),
        sa.column("created_at", sa.DateTime()),
    )

    rows = bind.execute(
        sa.select(
            billing_limits.c.id,
            billing_limits.c.period_type,
            billing_limits.c.cycle_anchor_day,
            billing_limits.c.starts_at,
            billing_limits.c.created_at,
        )
    ).fetchall()

    for row in rows:
        anchor_source = row.starts_at or row.created_at or datetime.now(timezone.utc)
        anchor_day = int(anchor_source.day)
        bind.execute(
            billing_limits.update()
            .where(billing_limits.c.id == row.id)
            .values(
                period_type="monthly_anniversary",
                cycle_anchor_day=anchor_day,
            )
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("billing_limits")}

    billing_limits = sa.table(
        "billing_limits",
        sa.column("period_type", sa.String()),
    )
    bind.execute(
        billing_limits.update().values(period_type="rolling_30d")
    )

    with op.batch_alter_table("billing_limits", schema=None) as batch_op:
        if "cycle_anchor_day" in columns:
            batch_op.drop_column("cycle_anchor_day")
