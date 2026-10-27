"""Bind chat sessions to a verifiable owner

chat_sessions.owner_hash stores an HMAC of the authorized context that created
the session (signed visitor cookie, user id or verified WhatsApp number). A
conversation_id is only honoured when the requester presents the same owner, so
guessing or leaking a conversation id no longer grants access to the history.
Existing rows keep NULL and are never reused.

Revision ID: 5f1a2b3c4d6e
Revises: 5e2d9a7c4b18
Create Date: 2026-10-15

"""
from alembic import op
import sqlalchemy as sa


revision = '5f1a2b3c4d6e'
down_revision = '5e2d9a7c4b18'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('chat_sessions') as batch_op:
        batch_op.add_column(sa.Column('owner_hash', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_chat_sessions_owner_hash', ['owner_hash'])


def downgrade():
    with op.batch_alter_table('chat_sessions') as batch_op:
        batch_op.drop_index('ix_chat_sessions_owner_hash')
        batch_op.drop_column('owner_hash')
