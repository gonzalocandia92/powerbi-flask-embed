"""add process_heartbeats (Phase 3: report-scheduler liveness in the admin UI)

Additive: one small table, one row per background process instance, updated on every poll. No CHECK on
``process`` or ``last_outcome``: new process kinds and outcomes must not need a migration.

Idempotent: the table may already exist (db.create_all()).

Revision ID: 9d6e7f8a0b12
Revises: 8c4d5e6f7a91
Create Date: 2026-10-22 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '9d6e7f8a0b12'
down_revision = '8c4d5e6f7a91'
branch_labels = None
depends_on = None

TABLE = 'process_heartbeats'
INDEX = 'ix_process_heartbeats_process_seen'


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column('instance_id', sa.String(length=200), primary_key=True),
            sa.Column('process', sa.String(length=60), nullable=False),
            sa.Column('host', sa.String(length=255), nullable=True),
            sa.Column('pid', sa.Integer(), nullable=True),
            sa.Column('started_at', sa.DateTime(), nullable=False),
            sa.Column('last_seen_at', sa.DateTime(), nullable=False),
            sa.Column('interval_seconds', sa.Integer(), nullable=True),
            sa.Column('last_outcome', sa.String(length=20), nullable=True),
            sa.Column('details_json', sa.JSON(), nullable=True),
        )
    if INDEX not in {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(TABLE)}:
        op.create_index(INDEX, TABLE, ['process', 'last_seen_at'])


def downgrade():
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
