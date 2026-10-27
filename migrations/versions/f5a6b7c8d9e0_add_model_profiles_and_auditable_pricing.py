"""Add model-family controls and auditable pricing dimensions.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
"""
from alembic import op
import sqlalchemy as sa

revision = 'f5a6b7c8d9e0'
down_revision = 'e4f5a6b7c8d9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('ai_model_configs') as batch:
        batch.add_column(sa.Column('family_key', sa.String(80)))
        batch.add_column(sa.Column('family_options_json', sa.JSON()))
        batch.add_column(sa.Column('thinking_mode', sa.String(10)))
        batch.create_index('ix_ai_model_configs_family_key', ['family_key'])
    with op.batch_alter_table('ai_model_role_assignments') as batch:
        batch.add_column(sa.Column('thinking_mode', sa.String(10)))
    with op.batch_alter_table('ai_model_pricing') as batch:
        batch.add_column(sa.Column('gateway', sa.String(50)))
        batch.add_column(sa.Column('context_band', sa.String(20)))
        batch.add_column(sa.Column('source_url', sa.String(500)))
        batch.create_index('ix_ai_model_pricing_gateway', ['gateway'])
        batch.create_index('ix_ai_model_pricing_context_band', ['context_band'])
        for name in ('input', 'output', 'cache_write', 'cache_read'):
            batch.alter_column(f'{name}_cost_per_million_usd', existing_type=sa.Float(), type_=sa.Numeric(20, 9))
    with op.batch_alter_table('ai_usage_events') as batch:
        batch.add_column(sa.Column('context_band', sa.String(20)))
        batch.add_column(sa.Column('billing_status', sa.String(30), nullable=False, server_default='verified'))
        batch.add_column(sa.Column('reserved_cost_usd', sa.Numeric(20, 12)))
        batch.add_column(sa.Column('effective_thinking_mode', sa.String(10)))
        batch.create_index('ix_ai_usage_events_billing_status', ['billing_status'])
        for name in ('input', 'output', 'cache_write', 'cache_read', 'total'):
            batch.alter_column(f'{name}_cost_usd', existing_type=sa.Float(), type_=sa.Numeric(20, 12))
    # Only unambiguous legacy records are backfilled. Unknown/custom entries
    # remain unverified until an administrator selects a profile.
    op.execute(sa.text("UPDATE ai_model_configs SET family_key='claude-haiku-4.5', thinking_mode='off' WHERE provider='anthropic' AND gateway='direct' AND physical_model IN ('claude-haiku-4-5-20251001','claude-haiku-4-5') AND default_reasoning_effort IS NULL"))
    op.execute(sa.text("UPDATE ai_model_configs SET family_key='openai-gpt-4.1', thinking_mode='off' WHERE provider='openai' AND gateway='direct' AND physical_model IN ('gpt-4.1-mini','gpt-4.1','gpt-4.1-nano')"))
    op.execute(sa.text("UPDATE ai_model_configs SET family_key='deepseek-v4', thinking_mode='on', default_reasoning_effort='high' WHERE provider='deepseek' AND gateway='direct' AND physical_model IN ('deepseek-flash','deepseek-v4-pro') AND default_reasoning_effort IS NULL"))


def downgrade():
    with op.batch_alter_table('ai_usage_events') as batch:
        for name in ('input', 'output', 'cache_write', 'cache_read', 'total'):
            batch.alter_column(f'{name}_cost_usd', existing_type=sa.Numeric(20, 12), type_=sa.Float())
        batch.drop_index('ix_ai_usage_events_billing_status')
        for name in ('context_band', 'billing_status', 'reserved_cost_usd', 'effective_thinking_mode'):
            batch.drop_column(name)
    with op.batch_alter_table('ai_model_pricing') as batch:
        for name in ('input', 'output', 'cache_write', 'cache_read'):
            batch.alter_column(f'{name}_cost_per_million_usd', existing_type=sa.Numeric(20, 9), type_=sa.Float())
        batch.drop_index('ix_ai_model_pricing_context_band')
        batch.drop_index('ix_ai_model_pricing_gateway')
        for name in ('gateway', 'context_band', 'source_url'):
            batch.drop_column(name)
    with op.batch_alter_table('ai_model_role_assignments') as batch:
        batch.drop_column('thinking_mode')
    with op.batch_alter_table('ai_model_configs') as batch:
        batch.drop_index('ix_ai_model_configs_family_key')
        for name in ('family_key', 'family_options_json', 'thinking_mode'):
            batch.drop_column(name)
