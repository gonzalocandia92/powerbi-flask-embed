"""add report_schedules and report_schedule_occurrences (Phase 3: scheduled reports)

Additive. Creates the two schedule tables and adds to report_runs the columns that link a run to the
occurrence that created it (``trigger_source``, ``schedule_occurrence_id``) plus a PARTIAL unique index:
at most one open (queued/running/cancel_requested) run per occurrence. The index condition below is a
FROZEN copy of ``app.models.OPEN_RUN_PER_OCCURRENCE_WHERE``: do not edit it when the run statuses change,
add a new migration (``tests/test_report_schedule_migration.py`` fails until you do).

No CHECK constraints on statuses or policies on purpose: new values must not need a migration.

Idempotent: tables/columns may already exist (db.create_all()).

Revision ID: 8c4d5e6f7a91
Revises: 7b3c4d5e6f80
Create Date: 2026-10-21 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '8c4d5e6f7a91'
down_revision = '7b3c4d5e6f80'
branch_labels = None
depends_on = None

OPEN_RUN_INDEX = 'uq_report_runs_open_occurrence'
OPEN_RUN_WHERE = ("schedule_occurrence_id IS NOT NULL AND "
                  "status IN ('queued', 'running', 'cancel_requested')")


def _big_id():
    return sa.BigInteger().with_variant(sa.Integer, 'sqlite')


def _columns(inspector, table):
    return {column['name'] for column in inspector.get_columns(table)}


def _indexes(inspector, table):
    return {index['name'] for index in inspector.get_indexes(table)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table('report_schedules'):
        op.create_table(
            'report_schedules',
            sa.Column('id', _big_id(), primary_key=True, autoincrement=True),
            sa.Column('definition_id', sa.BigInteger(), nullable=False),
            sa.Column('name', sa.String(length=200), nullable=False),
            sa.Column('frequency', sa.String(length=20), nullable=False),
            sa.Column('weekday', sa.Integer(), nullable=True),
            sa.Column('day_of_month', sa.Integer(), nullable=True),
            sa.Column('local_time', sa.Time(), nullable=False),
            sa.Column('timezone', sa.String(length=64), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('next_run_at', sa.DateTime(), nullable=True),
            sa.Column('freshness_policy', sa.String(length=60), nullable=False,
                      server_default='import_refresh_after_period_end'),
            sa.Column('refresh_deadline_hours', sa.Integer(), nullable=True),
            sa.Column('misfire_policy', sa.String(length=40), nullable=False, server_default='latest_within_grace'),
            sa.Column('misfire_grace_hours', sa.Integer(), nullable=True),
            sa.Column('config_json', sa.JSON(), nullable=True),
            sa.Column('created_by_user_id', sa.BigInteger(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['definition_id'], ['analytical_report_definitions.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='SET NULL'),
        )
        op.create_index('ix_report_schedules_definition_id', 'report_schedules', ['definition_id'])
        op.create_index('ix_report_schedules_due', 'report_schedules', ['is_active', 'next_run_at'])

    if not inspector.has_table('report_schedule_occurrences'):
        op.create_table(
            'report_schedule_occurrences',
            sa.Column('id', _big_id(), primary_key=True, autoincrement=True),
            sa.Column('schedule_id', _big_id(), nullable=False),
            sa.Column('scheduled_for', sa.DateTime(), nullable=False),
            sa.Column('trigger_source', sa.String(length=20), nullable=False, server_default='scheduled'),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='waiting_data'),
            sa.Column('period_start', sa.DateTime(), nullable=False),
            sa.Column('period_end_exclusive', sa.DateTime(), nullable=False),
            sa.Column('definition_snapshot_json', sa.JSON(), nullable=True),
            sa.Column('refresh_deadline_at', sa.DateTime(), nullable=True),
            sa.Column('refresh_checked_at', sa.DateTime(), nullable=True),
            sa.Column('refresh_completed_at', sa.DateTime(), nullable=True),
            sa.Column('next_check_at', sa.DateTime(), nullable=True),
            sa.Column('check_attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_error_code', sa.String(length=80), nullable=True),
            sa.Column('last_error_message', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['schedule_id'], ['report_schedules.id'], ondelete='CASCADE'),
            sa.UniqueConstraint('schedule_id', 'scheduled_for', name='uq_report_schedule_occurrence_slot'),
        )
        op.create_index('ix_report_schedule_occurrences_status_check', 'report_schedule_occurrences',
                        ['status', 'next_check_at'])

    columns = _columns(inspector, 'report_runs')
    if not {'trigger_source', 'schedule_occurrence_id'} <= columns:
        # batch mode: SQLite can only add a foreign key by rebuilding the table (PostgreSQL ALTERs in place).
        with op.batch_alter_table('report_runs') as batch:
            if 'trigger_source' not in columns:
                batch.add_column(sa.Column(
                    'trigger_source', sa.String(length=20), nullable=False, server_default='manual'))
            if 'schedule_occurrence_id' not in columns:
                batch.add_column(sa.Column('schedule_occurrence_id', _big_id(), nullable=True))
                batch.create_foreign_key('fk_report_runs_schedule_occurrence', 'report_schedule_occurrences',
                                         ['schedule_occurrence_id'], ['id'], ondelete='SET NULL')
    indexes = _indexes(sa.inspect(bind), 'report_runs')
    if 'ix_report_runs_schedule_occurrence_id' not in indexes:
        op.create_index('ix_report_runs_schedule_occurrence_id', 'report_runs', ['schedule_occurrence_id'])
    if OPEN_RUN_INDEX not in indexes:
        op.create_index(OPEN_RUN_INDEX, 'report_runs', ['schedule_occurrence_id'], unique=True,
                        postgresql_where=sa.text(OPEN_RUN_WHERE), sqlite_where=sa.text(OPEN_RUN_WHERE))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = _indexes(inspector, 'report_runs')
    for name in (OPEN_RUN_INDEX, 'ix_report_runs_schedule_occurrence_id'):
        if name in indexes:
            op.drop_index(name, table_name='report_runs')
    columns = _columns(inspector, 'report_runs')
    # batch mode: SQLite can only drop a column (with its FK) by rebuilding the table.
    with op.batch_alter_table('report_runs') as batch:
        if 'schedule_occurrence_id' in columns:
            if bind.dialect.name != 'sqlite':
                batch.drop_constraint('fk_report_runs_schedule_occurrence', type_='foreignkey')
            batch.drop_column('schedule_occurrence_id')
        if 'trigger_source' in columns:
            batch.drop_column('trigger_source')
    if inspector.has_table('report_schedule_occurrences'):
        op.drop_table('report_schedule_occurrences')
    if inspector.has_table('report_schedules'):
        op.drop_table('report_schedules')
