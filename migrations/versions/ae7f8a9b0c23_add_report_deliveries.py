"""add report_deliveries (distribution of approved scheduled reports, e.g. WhatsApp)

Additive: one outbox table, one row per approved review x channel x recipient. No CHECK on ``channel`` or
``status``: new channels and statuses must not need a migration.

Idempotent: the table may already exist (db.create_all()).

Revision ID: ae7f8a9b0c23
Revises: 9d6e7f8a0b12
Create Date: 2026-10-30 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'ae7f8a9b0c23'
down_revision = '9d6e7f8a0b12'
branch_labels = None
depends_on = None

TABLE = 'report_deliveries'
INDEXES = {
    'ix_report_deliveries_due': ['status', 'next_attempt_at'],
    'ix_report_deliveries_report_run_id': ['report_run_id'],
    'ix_report_deliveries_schedule_id': ['schedule_id'],
}

_ID = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')


def upgrade():
    if not sa.inspect(op.get_bind()).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column('id', _ID, primary_key=True, autoincrement=True),
            sa.Column('review_id', _ID, sa.ForeignKey('report_reviews.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('report_run_id', sa.String(length=36), nullable=False),
            sa.Column('schedule_id', sa.BigInteger(), nullable=True),
            sa.Column('channel', sa.String(length=30), nullable=False),
            sa.Column('recipient', sa.String(length=64), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
            sa.Column('lease_until', sa.DateTime(), nullable=True),
            sa.Column('provider_message_id', sa.String(length=200), nullable=True),
            sa.Column('last_error_code', sa.String(length=80), nullable=True),
            sa.Column('last_error_message', sa.Text(), nullable=True),
            sa.Column('sent_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('review_id', 'channel', 'recipient',
                                name='uq_report_delivery_review_channel_recipient'),
        )
    existing = {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(TABLE)}
    for name, columns in INDEXES.items():
        if name not in existing:
            op.create_index(name, TABLE, columns)


def downgrade():
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
