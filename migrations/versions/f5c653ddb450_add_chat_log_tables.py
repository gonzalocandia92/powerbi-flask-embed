"""add_chat_log_tables

Revision ID: f5c653ddb450
Revises: 4a61a9be78e8
Create Date: 2026-05-28 11:48:36.572321

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = 'f5c653ddb450'
down_revision = '4a61a9be78e8'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = inspect(bind).get_table_names()

    if 'chat_sessions' not in existing:
        op.create_table('chat_sessions',
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('slug', sa.String(length=120), nullable=True),
            sa.Column('title', sa.String(length=200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('last_message_at', sa.DateTime(), nullable=False),
            sa.Column('total_messages', sa.Integer(), nullable=False),
            sa.Column('had_errors', sa.Boolean(), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )
        with op.batch_alter_table('chat_sessions', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_chat_sessions_slug'), ['slug'], unique=False)

    if 'chat_messages' not in existing:
        op.create_table('chat_messages',
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('session_id', sa.BigInteger(), nullable=False),
            sa.Column('role', sa.String(length=20), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('latency_ms', sa.Integer(), nullable=True),
            sa.Column('model_used', sa.String(length=100), nullable=True),
            sa.Column('input_tokens', sa.Integer(), nullable=True),
            sa.Column('output_tokens', sa.Integer(), nullable=True),
            sa.Column('mcp_used', sa.Boolean(), nullable=True),
            sa.Column('tools_called', sa.JSON(), nullable=True),
            sa.Column('dax_query', sa.Text(), nullable=True),
            sa.Column('had_error', sa.Boolean(), nullable=False),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id')
        )
        with op.batch_alter_table('chat_messages', schema=None) as batch_op:
            batch_op.create_index('ix_chat_message_session_created', ['session_id', 'created_at'], unique=False)
            batch_op.create_index(batch_op.f('ix_chat_messages_session_id'), ['session_id'], unique=False)

    # chatbot_enabled — requires sudata_owner to run:
    # ALTER TABLE reports ADD COLUMN IF NOT EXISTS chatbot_enabled BOOLEAN NOT NULL DEFAULT false;


def downgrade():
    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chat_messages_session_id'))
        batch_op.drop_index('ix_chat_message_session_created')

    op.drop_table('chat_messages')

    with op.batch_alter_table('chat_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chat_sessions_slug'))

    op.drop_table('chat_sessions')
