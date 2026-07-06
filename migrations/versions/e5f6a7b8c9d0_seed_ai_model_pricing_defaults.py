"""seed ai model pricing defaults

Revision ID: e5f6a7b8c9d0
Revises: d4f1e2a3b4c5
Create Date: 2026-06-10 13:00:00.000000

"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = 'e5f6a7b8c9d0'
down_revision = 'd4f1e2a3b4c5'
branch_labels = None
depends_on = None


def _table():
    return sa.table(
        'ai_model_pricing',
        sa.column('provider', sa.String()),
        sa.column('model', sa.String()),
        sa.column('event_type', sa.String()),
        sa.column('currency', sa.String()),
        sa.column('input_cost_per_million_usd', sa.Float()),
        sa.column('output_cost_per_million_usd', sa.Float()),
        sa.column('cache_write_cost_per_million_usd', sa.Float()),
        sa.column('cache_read_cost_per_million_usd', sa.Float()),
        sa.column('is_active', sa.Boolean()),
        sa.column('effective_from', sa.DateTime()),
        sa.column('effective_to', sa.DateTime()),
        sa.column('created_at', sa.DateTime()),
        sa.column('updated_at', sa.DateTime()),
    )


def _exists(bind, provider: str, model: str, event_type: str) -> bool:
    pricing = _table()
    stmt = (
        sa.select(sa.func.count())
        .select_from(pricing)
        .where(
            pricing.c.provider == provider,
            pricing.c.model == model,
            pricing.c.event_type == event_type,
            pricing.c.is_active.is_(True),
        )
    )
    return bool(bind.execute(stmt).scalar())


def upgrade():
    bind = op.get_bind()
    pricing = _table()
    now = datetime.now(timezone.utc)
    rows = []

    if not _exists(bind, 'anthropic', 'claude-haiku-4-5-20251001', 'generation'):
        rows.append(
            {
                'provider': 'anthropic',
                'model': 'claude-haiku-4-5-20251001',
                'event_type': 'generation',
                'currency': 'USD',
                'input_cost_per_million_usd': 1.0,
                'output_cost_per_million_usd': 5.0,
                'cache_write_cost_per_million_usd': 1.25,
                'cache_read_cost_per_million_usd': 0.10,
                'is_active': True,
                'effective_from': now,
                'effective_to': None,
                'created_at': now,
                'updated_at': now,
            }
        )

    if not _exists(bind, 'voyageai', 'voyage-4', 'embedding'):
        rows.append(
            {
                'provider': 'voyageai',
                'model': 'voyage-4',
                'event_type': 'embedding',
                'currency': 'USD',
                'input_cost_per_million_usd': 0.06,
                'output_cost_per_million_usd': 0.0,
                'cache_write_cost_per_million_usd': 0.0,
                'cache_read_cost_per_million_usd': 0.0,
                'is_active': True,
                'effective_from': now,
                'effective_to': None,
                'created_at': now,
                'updated_at': now,
            }
        )

    if rows:
        op.bulk_insert(pricing, rows)


def downgrade():
    pricing = _table()
    bind = op.get_bind()
    bind.execute(
        pricing.delete().where(
            sa.tuple_(pricing.c.provider, pricing.c.model, pricing.c.event_type).in_(
                [
                    ('anthropic', 'claude-haiku-4-5-20251001', 'generation'),
                    ('voyageai', 'voyage-4', 'embedding'),
                ]
            )
        )
    )
