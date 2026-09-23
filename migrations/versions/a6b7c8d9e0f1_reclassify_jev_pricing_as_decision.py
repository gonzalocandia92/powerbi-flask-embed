"""Classify Jev pricing as decision usage.

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
"""
from alembic import op
import sqlalchemy as sa

revision = 'a6b7c8d9e0f1'
down_revision = 'f5a6b7c8d9e0'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
        UPDATE ai_model_pricing
        SET event_type = 'decision'
        WHERE provider = 'typesafe' AND model = 'jev-latest' AND event_type = 'embedding'
          AND NOT EXISTS (
              SELECT 1 FROM ai_usage_events
              WHERE ai_usage_events.pricing_id = ai_model_pricing.id
          )
    """))


def downgrade():
    # The original event type is user data; changing all decision prices back
    # would also alter records created after this migration.
    pass
