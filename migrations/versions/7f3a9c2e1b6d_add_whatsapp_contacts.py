"""add_whatsapp_contacts

Revision ID: 7f3a9c2e1b6d
Revises: 6c7d8e9f0a1b
Create Date: 2026-06-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = '7f3a9c2e1b6d'
down_revision = '6c7d8e9f0a1b'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = inspect(bind).get_table_names()

    if 'whatsapp_contacts' not in existing:
        op.create_table(
            'whatsapp_contacts',
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('phone_number', sa.String(length=32), nullable=False),
            sa.Column('report_id_fk', sa.BigInteger(), nullable=False),
            sa.Column('slug', sa.String(length=120), nullable=False),
            sa.Column('conversation_id', sa.BigInteger(), nullable=True),
            sa.Column('is_processing', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('last_message_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['report_id_fk'], ['reports.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['conversation_id'], ['chat_sessions.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
        with op.batch_alter_table('whatsapp_contacts', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_whatsapp_contacts_phone_number'), ['phone_number'], unique=True)


def downgrade():
    if 'whatsapp_contacts' in inspect(op.get_bind()).get_table_names():
        with op.batch_alter_table('whatsapp_contacts', schema=None) as batch_op:
            batch_op.drop_index(batch_op.f('ix_whatsapp_contacts_phone_number'))

        op.drop_table('whatsapp_contacts')
