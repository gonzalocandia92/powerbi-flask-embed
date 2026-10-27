"""add_chat_channel_and_mcp_interactions

Revision ID: dda0c4adf327
Revises: d8e9f0a1b2c3
Create Date: 2026-09-28 00:00:00.000000

Schema-only migration for the new admin "Interacciones" screen:

- ``chat_sessions.channel``: plain VARCHAR (never a Postgres ENUM, so new
  channels can be added later without a migration), nullable and indexed.
  It is NOT backfilled here on purpose — a blanket ``NULL -> 'klara_chat'``
  would misclassify historical WhatsApp conversations recorded before the
  code explicitly tagged them (WhatsApp shipped 2026-07-10, but the
  chatbot_service call only started passing ``source="whatsapp"``, and
  therefore ``channel``, on 2026-09-23 — see git history). Historical rows
  are classified by ``backfill_chat_channels.py`` at the repo root, which
  documents its evidence-based criteria and leaves anything it cannot
  determine as NULL ("Sin identificar" in the UI).
- ``chat_messages.reply_to_message_id``: explicit, nullable self-FK from an
  assistant reply to the user message it answers. Populated going forward
  by chatbot_service for every new turn; NULL on historical rows, where the
  admin read layer falls back to positional pairing (see
  app/services/interactions/chat_provider.py).
- ``mcp_interactions``: small, MCP-specific table that stores only the
  question captured from the MCP broker (never an answer/cost/tokens/model,
  which KLARA never computes for MCP — see the McpInteraction docstring).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = 'dda0c4adf327'
down_revision = 'd8e9f0a1b2c3'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing_tables = inspect(bind).get_table_names()

    with op.batch_alter_table('chat_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('channel', sa.String(length=30), nullable=True))
        batch_op.create_index(batch_op.f('ix_chat_sessions_channel'), ['channel'], unique=False)

    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reply_to_message_id', sa.BigInteger(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_chat_messages_reply_to_message_id'), ['reply_to_message_id'], unique=False
        )
        batch_op.create_foreign_key(
            'fk_chat_messages_reply_to_message_id',
            'chat_messages', ['reply_to_message_id'], ['id'], ondelete='SET NULL'
        )

    if 'mcp_interactions' not in existing_tables:
        op.create_table(
            'mcp_interactions',
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('question', sa.Text(), nullable=False),
            sa.Column('empresa_id', sa.BigInteger(), nullable=True),
            sa.Column('report_id_fk', sa.BigInteger(), nullable=True),
            sa.Column('user_id', sa.BigInteger(), nullable=True),
            sa.Column('mcp_session_public_id', sa.String(length=36), nullable=True),
            sa.Column('grant_public_id', sa.String(length=36), nullable=True),
            sa.Column('source_tool', sa.String(length=50), nullable=False),
            sa.ForeignKeyConstraint(['empresa_id'], ['clientes_privados.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['report_id_fk'], ['reports.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
        with op.batch_alter_table('mcp_interactions', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_mcp_interactions_created_at'), ['created_at'], unique=False)
            batch_op.create_index(batch_op.f('ix_mcp_interactions_empresa_id'), ['empresa_id'], unique=False)
            batch_op.create_index(batch_op.f('ix_mcp_interactions_report_id_fk'), ['report_id_fk'], unique=False)
            batch_op.create_index(batch_op.f('ix_mcp_interactions_user_id'), ['user_id'], unique=False)
            batch_op.create_index(
                batch_op.f('ix_mcp_interactions_mcp_session_public_id'), ['mcp_session_public_id'], unique=False
            )
            batch_op.create_index(
                batch_op.f('ix_mcp_interactions_grant_public_id'), ['grant_public_id'], unique=False
            )
            batch_op.create_index(
                'ix_mcp_interactions_session_created', ['mcp_session_public_id', 'created_at'], unique=False
            )


def downgrade():
    if 'mcp_interactions' in inspect(op.get_bind()).get_table_names():
        op.drop_table('mcp_interactions')

    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.drop_constraint('fk_chat_messages_reply_to_message_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_chat_messages_reply_to_message_id'))
        batch_op.drop_column('reply_to_message_id')

    with op.batch_alter_table('chat_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chat_sessions_channel'))
        batch_op.drop_column('channel')
