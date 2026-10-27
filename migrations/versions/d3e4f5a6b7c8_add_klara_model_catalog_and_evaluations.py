"""add KLARA model catalog, allowlists and evaluation records

Revision ID: d3e4f5a6b7c8
Revises: ebcd339309df
Create Date: 2026-09-20 04:00:00.000000
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = 'd3e4f5a6b7c8'
down_revision = 'ebcd339309df'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('chat_sessions') as batch_op:
        batch_op.add_column(sa.Column('last_model_key', sa.String(length=120), nullable=True))
    with op.batch_alter_table('chat_messages') as batch_op:
        batch_op.add_column(sa.Column('requested_model_key', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('model_key', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('model_provider', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('physical_model', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('model_gateway', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('service_tier', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('actual_model', sa.String(length=200), nullable=True))
    with op.batch_alter_table('ai_model_pricing') as batch_op:
        batch_op.add_column(sa.Column('service_tier', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('pricing_tier', sa.String(length=50), nullable=True))
        batch_op.create_index('ix_ai_model_pricing_service_tier', ['service_tier'])
        batch_op.create_index('ix_ai_model_pricing_pricing_tier', ['pricing_tier'])
    with op.batch_alter_table('ai_usage_events') as batch_op:
        batch_op.add_column(sa.Column('model_key', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('gateway', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('service_tier', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('pricing_tier', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('actual_model', sa.String(length=200), nullable=True))
        batch_op.create_index('ix_ai_usage_events_model_key', ['model_key'])

    now = datetime.now(timezone.utc)
    models = sa.table(
        'ai_model_configs',
        sa.column('id', sa.BigInteger()), sa.column('model_key', sa.String()),
        sa.column('display_name', sa.String()), sa.column('provider', sa.String()),
        sa.column('physical_model', sa.String()), sa.column('gateway', sa.String()),
        sa.column('enabled', sa.Boolean()), sa.column('client_selectable', sa.Boolean()),
        sa.column('validation_status', sa.String()), sa.column('validated_at', sa.DateTime()),
        sa.column('supports_tools', sa.Boolean()), sa.column('supports_reasoning', sa.Boolean()),
        sa.column('supports_cache_key', sa.Boolean()), sa.column('supports_flex', sa.Boolean()),
        sa.column('context_window', sa.Integer()), sa.column('max_output_tokens', sa.Integer()),
        sa.column('created_at', sa.DateTime()), sa.column('updated_at', sa.DateTime()),
    )
    model_rows = [
        {'id': 1, 'model_key': 'claude-haiku-4-5', 'display_name': 'Claude Haiku 4.5', 'provider': 'anthropic', 'physical_model': 'claude-haiku-4-5-20251001', 'gateway': 'direct', 'enabled': True, 'client_selectable': True, 'validation_status': 'passed', 'validated_at': now, 'supports_tools': True, 'supports_reasoning': False, 'supports_cache_key': False, 'supports_flex': False, 'context_window': 200000, 'max_output_tokens': 4096, 'created_at': now, 'updated_at': now},
        {'id': 2, 'model_key': 'openai-gpt-4-1-mini', 'display_name': 'OpenAI GPT-4.1 mini', 'provider': 'openai', 'physical_model': 'gpt-4.1-mini', 'gateway': 'direct', 'enabled': False, 'client_selectable': False, 'validation_status': 'pending', 'validated_at': None, 'supports_tools': True, 'supports_reasoning': False, 'supports_cache_key': True, 'supports_flex': True, 'context_window': 1047576, 'max_output_tokens': 32768, 'created_at': now, 'updated_at': now},
        {'id': 3, 'model_key': 'deepseek-chat', 'display_name': 'DeepSeek Chat', 'provider': 'deepseek', 'physical_model': 'deepseek-chat', 'gateway': 'direct', 'enabled': False, 'client_selectable': False, 'validation_status': 'pending', 'validated_at': None, 'supports_tools': True, 'supports_reasoning': False, 'supports_cache_key': False, 'supports_flex': False, 'context_window': 128000, 'max_output_tokens': 8192, 'created_at': now, 'updated_at': now},
        {'id': 4, 'model_key': 'deepseek-openrouter', 'display_name': 'DeepSeek via OpenRouter', 'provider': 'deepseek', 'physical_model': 'deepseek/deepseek-chat', 'gateway': 'openrouter', 'enabled': False, 'client_selectable': False, 'validation_status': 'pending', 'validated_at': None, 'supports_tools': True, 'supports_reasoning': False, 'supports_cache_key': False, 'supports_flex': False, 'context_window': 128000, 'max_output_tokens': 8192, 'created_at': now, 'updated_at': now},
    ]
    if op.get_bind().dialect.name != 'sqlite':
        for row in model_rows:
            row.pop('id', None)
    op.bulk_insert(models, model_rows)
    claude_model_id = op.get_bind().execute(
        sa.text("SELECT id FROM ai_model_configs WHERE model_key = 'claude-haiku-4-5'")
    ).scalar_one()
    roles = sa.table(
        'ai_model_role_assignments',
        sa.column('id', sa.BigInteger()), sa.column('model_id', sa.BigInteger()),
        sa.column('role', sa.String()), sa.column('scope_type', sa.String()),
        sa.column('scope_id', sa.String()), sa.column('is_active', sa.Boolean()),
        sa.column('max_output_tokens', sa.Integer()), sa.column('created_at', sa.DateTime()),
        sa.column('updated_at', sa.DateTime()),
    )
    role_rows = [
        {'id': 1, 'model_id': claude_model_id, 'role': 'main_agent', 'scope_type': 'global', 'scope_id': None, 'is_active': True, 'max_output_tokens': 4096, 'created_at': now, 'updated_at': now},
        {'id': 2, 'model_id': claude_model_id, 'role': 'query_rewriter', 'scope_type': 'global', 'scope_id': None, 'is_active': True, 'max_output_tokens': 100, 'created_at': now, 'updated_at': now},
        {'id': 3, 'model_id': claude_model_id, 'role': 'skill_selector', 'scope_type': 'global', 'scope_id': None, 'is_active': True, 'max_output_tokens': 500, 'created_at': now, 'updated_at': now},
    ]
    if op.get_bind().dialect.name != 'sqlite':
        for row in role_rows:
            row.pop('id', None)
    op.bulk_insert(roles, role_rows)
    grants = sa.table(
        'ai_model_grants',
        sa.column('id', sa.BigInteger()), sa.column('model_id', sa.BigInteger()),
        sa.column('scope_type', sa.String()), sa.column('scope_id', sa.String()),
        sa.column('allowed', sa.Boolean()), sa.column('is_default', sa.Boolean()),
        sa.column('client_selectable', sa.Boolean()), sa.column('created_at', sa.DateTime()),
        sa.column('updated_at', sa.DateTime()),
    )
    grant_rows = [{'id': 1, 'model_id': claude_model_id, 'scope_type': 'global', 'scope_id': None, 'allowed': True, 'is_default': True, 'client_selectable': True, 'created_at': now, 'updated_at': now}]
    if op.get_bind().dialect.name != 'sqlite':
        grant_rows[0].pop('id', None)
    op.bulk_insert(grants, grant_rows)


def downgrade():
    seed_keys = "('claude-haiku-4-5', 'openai-gpt-4-1-mini', 'deepseek-chat', 'deepseek-openrouter')"
    op.execute(sa.text(
        f"DELETE FROM ai_model_grants WHERE model_id IN "
        f"(SELECT id FROM ai_model_configs WHERE model_key IN {seed_keys})"
    ))
    op.execute(sa.text(
        f"DELETE FROM ai_model_role_assignments WHERE model_id IN "
        f"(SELECT id FROM ai_model_configs WHERE model_key IN {seed_keys})"
    ))
    op.execute(sa.text(f"DELETE FROM ai_model_configs WHERE model_key IN {seed_keys}"))
    with op.batch_alter_table('chat_messages') as batch_op:
        for column in ('actual_model', 'service_tier', 'model_gateway', 'physical_model', 'model_provider', 'model_key', 'requested_model_key'):
            batch_op.drop_column(column)
    with op.batch_alter_table('ai_usage_events') as batch_op:
        batch_op.drop_index('ix_ai_usage_events_model_key')
        for column in ('actual_model', 'pricing_tier', 'service_tier', 'gateway', 'model_key'):
            batch_op.drop_column(column)
    with op.batch_alter_table('ai_model_pricing') as batch_op:
        batch_op.drop_index('ix_ai_model_pricing_pricing_tier')
        batch_op.drop_index('ix_ai_model_pricing_service_tier')
        batch_op.drop_column('pricing_tier')
        batch_op.drop_column('service_tier')
    with op.batch_alter_table('chat_sessions') as batch_op:
        batch_op.drop_column('last_model_key')
