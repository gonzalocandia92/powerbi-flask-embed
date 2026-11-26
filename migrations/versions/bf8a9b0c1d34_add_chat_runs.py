"""add chat_runs (asynchronous KLARA chat queue: one row per queued question)

Additive: one table. No CHECK on ``status``, ``stage`` or ``error_code``
(``app/persistence/chat_run_states.py``): new stages and codes must not need a migration.

Idempotent: the table may already exist (db.create_all()).

Revision ID: bf8a9b0c1d34
Revises: ae7f8a9b0c23
Create Date: 2026-11-16 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'bf8a9b0c1d34'
down_revision = 'ae7f8a9b0c23'
branch_labels = None
depends_on = None

TABLE = 'chat_runs'
INDEXES = {
    'ix_chat_runs_status_created': ['status', 'created_at'],
    'ix_chat_runs_owner_status': ['owner_hash', 'status'],
    'ix_chat_runs_finished_at': ['finished_at'],
    'ix_chat_runs_report_id_fk': ['report_id_fk'],
}


def upgrade():
    if not sa.inspect(op.get_bind()).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column('id', sa.String(length=36), primary_key=True),
            sa.Column('slug', sa.String(length=120), nullable=False),
            sa.Column('report_id_fk', sa.BigInteger(), sa.ForeignKey('reports.id', ondelete='SET NULL'),
                      nullable=True),
            sa.Column('conversation_id_in', sa.String(length=64), nullable=True),
            sa.Column('model_key_in', sa.String(length=120), nullable=True),
            sa.Column('question', sa.Text(), nullable=False),
            sa.Column('reset_history', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('is_staff', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('owner_hash', sa.String(length=64), nullable=False),
            sa.Column('user_key', sa.String(length=200), nullable=True),
            sa.Column('source', sa.String(length=30), nullable=False, server_default='chat'),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='queued'),
            sa.Column('stage', sa.String(length=40), nullable=True),
            sa.Column('worker_id', sa.String(length=120), nullable=True),
            sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
            sa.Column('lease_expires_at', sa.DateTime(), nullable=True),
            sa.Column('cancel_requested_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('claimed_at', sa.DateTime(), nullable=True),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.Column('result_json', sa.JSON(), nullable=True),
            sa.Column('error_code', sa.String(length=80), nullable=True),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('diagnostic_id', sa.String(length=32), nullable=True),
        )
    existing = {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(TABLE)}
    for name, columns in INDEXES.items():
        if name not in existing:
            op.create_index(name, TABLE, columns)


def downgrade():
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
