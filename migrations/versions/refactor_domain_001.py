"""Refactor domain: remove ReportConfig, add hierarchical FKs

Client (1) -> (N) Tenant (1) -> (N) Workspace (1) -> (N) Report (1) -> (N) PublicLink
Report (M) <-> (N) Empresa

Revision ID: refactor_domain_001
Revises: 533ac3eb829f
Create Date: 2026-04-10

NOTE: The schema changes originally intended by this migration are already
fully applied by the preceding migration (533ac3eb829f), which creates the
complete refactored schema from scratch. This migration is therefore a
no-op bridge that preserves the revision chain for databases that were
initialized via 533ac3eb829f.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = 'refactor_domain_001'
down_revision = '533ac3eb829f'
branch_labels = None
depends_on = None


def upgrade():
    # All schema changes were already applied by migration 533ac3eb829f,
    # which creates the full refactored schema from scratch. No-op here.
    pass


def downgrade():
    # Corresponding no-op: 533ac3eb829f handles the full schema creation.
    pass
