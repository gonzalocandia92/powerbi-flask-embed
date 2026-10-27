"""Record the effective identity of every chat turn

chat_messages gets the report, dataset/workspace fingerprints and credential
reference actually used for the turn, plus an opaque diagnostic_id that is the
only handle shared with the public client. Existing rows keep NULL.

Revision ID: 6a2b3c4d5e7f
Revises: 5f1a2b3c4d6e
Create Date: 2026-10-15

"""
from alembic import op
import sqlalchemy as sa


revision = '6a2b3c4d5e7f'
down_revision = '5f1a2b3c4d6e'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('chat_messages') as batch_op:
        batch_op.add_column(sa.Column('report_id_fk', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('dataset_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('workspace_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('credential_ref', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('dataset_source', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('trace_id', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('diagnostic_id', sa.String(length=32), nullable=True))
        batch_op.create_foreign_key(
            'fk_chat_messages_report_id_fk_reports', 'reports', ['report_id_fk'], ['id']
        )
        batch_op.create_index('ix_chat_messages_report_id_fk', ['report_id_fk'])
        batch_op.create_index('ix_chat_messages_diagnostic_id', ['diagnostic_id'], unique=True)


def downgrade():
    with op.batch_alter_table('chat_messages') as batch_op:
        batch_op.drop_index('ix_chat_messages_diagnostic_id')
        batch_op.drop_index('ix_chat_messages_report_id_fk')
        batch_op.drop_constraint('fk_chat_messages_report_id_fk_reports', type_='foreignkey')
        for column in ('diagnostic_id', 'trace_id', 'dataset_source', 'credential_ref',
                       'workspace_hash', 'dataset_hash', 'report_id_fk'):
            batch_op.drop_column(column)
