"""Move public link action flags to reports

allow_refresh and allow_reset_to_default move from public_links to reports, so the
same setting applies to every public link of a report and to the private API.
Also adds reports.allow_refresh_visuals (Power BI client SDK report.refresh()),
used by the private API.

Existing values are preserved: a report enables an action when any of its active
public links had it enabled. Reports whose active links disagreed are logged as a
warning, because their other links change behaviour.

Downgrade copies each report's value back onto all of its links.
reports.allow_refresh_visuals has no previous home and is dropped.

Revision ID: a7c3e91b5d24
Revises: ebcd339309df
Create Date: 2026-10-06

"""
import logging

from alembic import op
import sqlalchemy as sa


revision = 'a7c3e91b5d24'
down_revision = 'ebcd339309df'
branch_labels = None
depends_on = None

logger = logging.getLogger('alembic.runtime.migration')

# Flags that already exist on public_links and move to reports.
_MOVED_FLAGS = ('allow_refresh', 'allow_reset_to_default')
# Flag that only exists on reports.
_NEW_FLAG = 'allow_refresh_visuals'


def _links_table():
    return sa.table(
        'public_links',
        sa.column('report_id_fk'),
        sa.column('is_active', sa.Boolean),
        *[sa.column(flag, sa.Boolean) for flag in _MOVED_FLAGS],
    )


def _reports_table():
    return sa.table(
        'reports',
        sa.column('id'),
        *[sa.column(flag, sa.Boolean) for flag in _MOVED_FLAGS],
    )


def upgrade():
    with op.batch_alter_table('reports') as batch_op:
        for flag in _MOVED_FLAGS + (_NEW_FLAG,):
            batch_op.add_column(
                sa.Column(flag, sa.Boolean(), nullable=False, server_default=sa.false())
            )

    bind = op.get_bind()
    links = _links_table()
    reports = _reports_table()

    # flag -> report id -> set of values found on its active links
    seen = {flag: {} for flag in _MOVED_FLAGS}
    rows = bind.execute(
        sa.select(links.c.report_id_fk, links.c.is_active, *[links.c[f] for f in _MOVED_FLAGS])
    ).mappings()
    for row in rows:
        if row['is_active'] is False:
            continue
        for flag in _MOVED_FLAGS:
            seen[flag].setdefault(row['report_id_fk'], set()).add(bool(row[flag]))

    for flag in _MOVED_FLAGS:
        enabled = [rid for rid, values in seen[flag].items() if True in values]
        mixed = sorted(rid for rid, values in seen[flag].items() if len(values) > 1)
        if enabled:
            bind.execute(reports.update().where(reports.c.id.in_(enabled)).values({flag: True}))
        if mixed:
            logger.warning(
                "%s differed between active public links of reports %s; "
                "now enabled for all their links.",
                flag, mixed,
            )

    with op.batch_alter_table('public_links') as batch_op:
        for flag in _MOVED_FLAGS:
            batch_op.drop_column(flag)


def downgrade():
    with op.batch_alter_table('public_links') as batch_op:
        for flag in _MOVED_FLAGS:
            batch_op.add_column(
                sa.Column(flag, sa.Boolean(), nullable=False, server_default=sa.false())
            )

    links = _links_table()
    reports = _reports_table()
    for flag in _MOVED_FLAGS:
        op.execute(
            links.update().values({
                flag: sa.select(reports.c[flag])
                .where(reports.c.id == links.c.report_id_fk)
                .scalar_subquery()
            })
        )

    with op.batch_alter_table('reports') as batch_op:
        for flag in _MOVED_FLAGS + (_NEW_FLAG,):
            batch_op.drop_column(flag)
