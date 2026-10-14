"""Seed Claude Haiku 5.5 (disabled) and its two prompt-length rate cards.

Revision ID: 5e2d9a7c4b18
Revises: 91af0b7c2d13
"""
from datetime import datetime, timezone
from decimal import Decimal

from alembic import op
import sqlalchemy as sa

revision = '5e2d9a7c4b18'
down_revision = '91af0b7c2d13'
branch_labels = None
depends_on = None

SOURCE = 'https://platform.claude.com/docs/en/about-claude/pricing'
MODEL = 'claude-haiku-5-5'

# Prompts up to 100K tokens (short) vs over 100K tokens (long); the whole call is
# billed at one card. Order: input, output, 5m cache write, cache read (USD per MTok).
RATES = {
    'short': ('0.10', '0.50', '0.125', '0.01'),
    'long': ('0.50', '2.50', '0.625', '0.05'),
}


def upgrade():
    bind = op.get_bind()
    now = datetime.now(timezone.utc)

    pricing = sa.table('ai_model_pricing',
        sa.column('provider', sa.String()), sa.column('model', sa.String()),
        sa.column('event_type', sa.String()), sa.column('service_tier', sa.String()),
        sa.column('pricing_tier', sa.String()), sa.column('gateway', sa.String()),
        sa.column('context_band', sa.String()), sa.column('source_url', sa.String()),
        sa.column('currency', sa.String()),
        sa.column('input_cost_per_million_usd', sa.Numeric(20, 9)),
        sa.column('output_cost_per_million_usd', sa.Numeric(20, 9)),
        sa.column('cache_write_cost_per_million_usd', sa.Numeric(20, 9)),
        sa.column('cache_read_cost_per_million_usd', sa.Numeric(20, 9)),
        sa.column('is_active', sa.Boolean()), sa.column('effective_from', sa.DateTime()),
        sa.column('created_at', sa.DateTime()), sa.column('updated_at', sa.DateTime()))
    rows = []
    for band, (input_rate, output_rate, write_rate, read_rate) in RATES.items():
        exists = bind.execute(sa.select(sa.func.count()).select_from(pricing).where(
            pricing.c.provider == 'anthropic', pricing.c.model == MODEL,
            pricing.c.event_type == 'generation', pricing.c.gateway == 'direct',
            pricing.c.service_tier.is_(None), pricing.c.context_band == band,
            pricing.c.is_active.is_(True),
        )).scalar()
        if exists:
            continue
        rows.append(dict(provider='anthropic', model=MODEL, event_type='generation',
            service_tier=None, pricing_tier=None, gateway='direct', context_band=band,
            source_url=SOURCE, currency='USD',
            input_cost_per_million_usd=Decimal(input_rate),
            output_cost_per_million_usd=Decimal(output_rate),
            cache_write_cost_per_million_usd=Decimal(write_rate),
            cache_read_cost_per_million_usd=Decimal(read_rate),
            is_active=True, effective_from=now, created_at=now, updated_at=now))
    if rows:
        op.bulk_insert(pricing, rows)

    models = sa.table('ai_model_configs',
        sa.column('model_key', sa.String()), sa.column('display_name', sa.String()),
        sa.column('provider', sa.String()), sa.column('physical_model', sa.String()),
        sa.column('gateway', sa.String()), sa.column('enabled', sa.Boolean()),
        sa.column('client_selectable', sa.Boolean()), sa.column('validation_status', sa.String()),
        sa.column('supports_tools', sa.Boolean()), sa.column('supports_reasoning', sa.Boolean()),
        sa.column('supports_cache_key', sa.Boolean()), sa.column('supports_flex', sa.Boolean()),
        sa.column('context_window', sa.Integer()), sa.column('max_output_tokens', sa.Integer()),
        sa.column('family_key', sa.String()), sa.column('thinking_mode', sa.String()),
        sa.column('created_at', sa.DateTime()), sa.column('updated_at', sa.DateTime()))
    if not bind.execute(sa.select(sa.func.count()).select_from(models).where(
            models.c.model_key == MODEL)).scalar():
        # Disabled and unvalidated on purpose: it must pass the evaluation workflow
        # before any role, grant or client selection is added.
        op.bulk_insert(models, [dict(
            model_key=MODEL, display_name='Claude Haiku 5.5', provider='anthropic',
            physical_model=MODEL, gateway='direct', enabled=False, client_selectable=False,
            validation_status='pending', supports_tools=True, supports_reasoning=True,
            supports_cache_key=False, supports_flex=False, context_window=1_000_000,
            max_output_tokens=4096, family_key='claude-haiku-5.5', thinking_mode='off',
            created_at=now, updated_at=now)])


def downgrade():
    bind = op.get_bind()
    models = sa.table('ai_model_configs', sa.column('model_key', sa.String()),
        sa.column('id', sa.BigInteger()))
    model_id = bind.execute(sa.select(models.c.id).where(models.c.model_key == MODEL)).scalar()
    if model_id is not None:
        for table in ('ai_model_role_assignments', 'ai_model_grants'):
            bind.execute(sa.text(f'DELETE FROM {table} WHERE model_id = :id'), {'id': model_id})
        bind.execute(models.delete().where(models.c.model_key == MODEL))
    pricing = sa.table('ai_model_pricing', sa.column('provider', sa.String()),
        sa.column('model', sa.String()), sa.column('source_url', sa.String()))
    bind.execute(pricing.delete().where(
        pricing.c.provider == 'anthropic', pricing.c.model == MODEL,
        pricing.c.source_url == SOURCE))
