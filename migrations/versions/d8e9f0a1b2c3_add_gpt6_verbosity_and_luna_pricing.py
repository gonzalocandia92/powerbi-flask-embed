"""Add nullable model verbosity and auditable GPT-6 Luna rates.

Revision ID: d8e9f0a1b2c3
Revises: a6b7c8d9e0f1
"""
from datetime import datetime, timezone
from decimal import Decimal

from alembic import op
import sqlalchemy as sa

revision = 'd8e9f0a1b2c3'
down_revision = 'a6b7c8d9e0f1'
branch_labels = None
depends_on = None

SOURCE = 'https://developers.openai.com/api/docs/pricing'


def upgrade():
    with op.batch_alter_table('ai_model_configs') as batch:
        batch.add_column(sa.Column('default_verbosity', sa.String(10), nullable=True))

    pricing = sa.table('ai_model_pricing',
        sa.column('provider', sa.String()), sa.column('model', sa.String()),
        sa.column('event_type', sa.String()), sa.column('service_tier', sa.String()),
        sa.column('pricing_tier', sa.String()), sa.column('gateway', sa.String()),
        sa.column('context_band', sa.String()), sa.column('source_url', sa.String()),
        sa.column('currency', sa.String()),
        sa.column('input_cost_per_million_usd', sa.Numeric(20, 9)),
        sa.column('cache_read_cost_per_million_usd', sa.Numeric(20, 9)),
        sa.column('cache_write_cost_per_million_usd', sa.Numeric(20, 9)),
        sa.column('output_cost_per_million_usd', sa.Numeric(20, 9)),
        sa.column('is_active', sa.Boolean()), sa.column('effective_from', sa.DateTime()),
        sa.column('created_at', sa.DateTime()), sa.column('updated_at', sa.DateTime()))
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    standard = {
        'short': ('0.10', '0.01', '0.125', '0.50'),
        'long': ('0.20', '0.02', '0.25', '0.75'),
    }
    rows = []
    for tier in (None, 'flex'):
        for band, amounts in standard.items():
            exists = bind.execute(sa.select(sa.func.count()).select_from(pricing).where(
                pricing.c.provider == 'openai', pricing.c.model == 'gpt-6-luna',
                pricing.c.event_type == 'generation', pricing.c.gateway == 'direct',
                pricing.c.service_tier.is_(None) if tier is None else pricing.c.service_tier == tier,
                pricing.c.context_band == band, pricing.c.is_active.is_(True),
            )).scalar()
            if exists:
                continue
            factor = Decimal('0.5') if tier == 'flex' else Decimal('1')
            input_rate, read_rate, write_rate, output_rate = [Decimal(v) * factor for v in amounts]
            rows.append(dict(provider='openai', model='gpt-6-luna', event_type='generation',
                service_tier=tier, pricing_tier=None, gateway='direct', context_band=band,
                source_url=SOURCE, currency='USD', input_cost_per_million_usd=input_rate,
                cache_read_cost_per_million_usd=read_rate,
                cache_write_cost_per_million_usd=write_rate,
                output_cost_per_million_usd=output_rate, is_active=True,
                effective_from=now, created_at=now, updated_at=now))
    if rows:
        op.bulk_insert(pricing, rows)


def downgrade():
    pricing = sa.table('ai_model_pricing', sa.column('model', sa.String()),
        sa.column('source_url', sa.String()), sa.column('provider', sa.String()))
    op.get_bind().execute(pricing.delete().where(
        pricing.c.provider == 'openai', pricing.c.model == 'gpt-6-luna',
        pricing.c.source_url == SOURCE))
    with op.batch_alter_table('ai_model_configs') as batch:
        batch.drop_column('default_verbosity')
