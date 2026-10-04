"""add report_run_artifacts (persistent, versioned outputs of report runs)

Additive only: no existing table or row is touched. ``report_runs.result_json`` keeps
working as the legacy/fallback copy of the FinalReport.

Revision ID: 819746ad8c3d
Revises: f2a3b4c5d6e7
Create Date: 2026-10-03 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '819746ad8c3d'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None


def upgrade():
    # Idempotent: the table may already exist (e.g. created via db.create_all()).
    if sa.inspect(op.get_bind()).has_table('report_run_artifacts'):
        return
    op.create_table(
        'report_run_artifacts',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer, 'sqlite'), primary_key=True, autoincrement=True),
        sa.Column('report_run_id', sa.String(length=36), sa.ForeignKey('report_runs.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('artifact_type', sa.String(length=40), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('schema_version', sa.String(length=20), nullable=True),
        sa.Column('renderer_version', sa.String(length=40), nullable=True),
        sa.Column('content_type', sa.String(length=100), nullable=False),
        sa.Column('content_text', sa.Text(), nullable=True),
        sa.Column('content_bytes', sa.LargeBinary(), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('report_run_id', 'artifact_type', 'revision', name='uq_report_run_artifact_revision'),
    )
    op.create_index('ix_report_run_artifacts_report_run_id', 'report_run_artifacts', ['report_run_id'])
    op.create_index('ix_report_run_artifacts_run_type', 'report_run_artifacts', ['report_run_id', 'artifact_type'])


def downgrade():
    op.drop_table('report_run_artifacts')
