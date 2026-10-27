"""add structured evidence to report_run_sections (V1.5)

Additive only: two nullable columns on ``report_run_sections``. ``evidence_json`` holds the facts / series /
tables of the DAX result behind a section's answer as ONE document; ``evidence_schema_version`` is the version
of that document's own schema. Existing rows keep NULL (no structured evidence) and keep working.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-04 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'b8c9d0e1f2a3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def _columns():
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns('report_run_sections')}


def upgrade():
    existing = _columns()
    if 'evidence_json' not in existing:
        op.add_column('report_run_sections', sa.Column('evidence_json', sa.JSON(), nullable=True))
    if 'evidence_schema_version' not in existing:
        op.add_column('report_run_sections',
                      sa.Column('evidence_schema_version', sa.String(length=10), nullable=True))


def downgrade():
    existing = _columns()
    if 'evidence_schema_version' in existing:
        op.drop_column('report_run_sections', 'evidence_schema_version')
    if 'evidence_json' in existing:
        op.drop_column('report_run_sections', 'evidence_json')
