"""add analytical report definitions, durable report runs and run sections

Revision ID: f2a3b4c5d6e7
Revises: dda0c4adf327
Create Date: 2026-10-03 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'f2a3b4c5d6e7'
down_revision = 'dda0c4adf327'
branch_labels = None
depends_on = None


def _bigint_pk():
    return sa.BigInteger().with_variant(sa.Integer, 'sqlite')


def upgrade():
    # Idempotent: the tables may already exist (e.g. created via db.create_all()).
    inspector = sa.inspect(op.get_bind())
    if all(inspector.has_table(t) for t in (
        'analytical_report_definitions', 'analytical_report_questions',
        'report_runs', 'report_run_sections',
    )):
        return
    if any(inspector.has_table(t) for t in (
        'analytical_report_definitions', 'analytical_report_questions',
        'report_runs', 'report_run_sections',
    )):
        # Partial state: drop leftovers (children first) so we can recreate cleanly.
        for t in ('report_run_sections', 'report_runs',
                  'analytical_report_questions', 'analytical_report_definitions'):
            if inspector.has_table(t):
                op.drop_table(t)
    op.create_table(
        'analytical_report_definitions',
        sa.Column('id', _bigint_pk(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('report_id_fk', sa.BigInteger(), sa.ForeignKey('reports.id', ondelete='CASCADE'), nullable=False),
        sa.Column('strategy', sa.String(length=30), nullable=False, server_default='fixed'),
        sa.Column('analysis_model_key', sa.String(length=120), nullable=True),
        sa.Column('analysis_service_tier', sa.String(length=50), nullable=True),
        sa.Column('structure_prompt', sa.Text(), nullable=True),
        sa.Column('config_json', sa.JSON(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_by_user_id', sa.BigInteger(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_analytical_report_definitions_report_id_fk', 'analytical_report_definitions', ['report_id_fk'])

    op.create_table(
        'analytical_report_questions',
        sa.Column('id', _bigint_pk(), primary_key=True, autoincrement=True),
        sa.Column('definition_id', sa.BigInteger(),
                  sa.ForeignKey('analytical_report_definitions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('key', sa.String(length=80), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('required_skill_keys_json', sa.JSON(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('definition_id', 'key', name='uq_analytical_report_question_key'),
    )
    op.create_index('ix_analytical_report_questions_definition_id', 'analytical_report_questions', ['definition_id'])

    op.create_table(
        'report_runs',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('definition_id', sa.BigInteger(),
                  sa.ForeignKey('analytical_report_definitions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('report_id_fk', sa.BigInteger(), sa.ForeignKey('reports.id', ondelete='CASCADE'), nullable=False),
        sa.Column('requested_by_user_id', sa.BigInteger(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False, server_default='queued'),
        sa.Column('current_stage', sa.String(length=30), nullable=True),
        sa.Column('progress_current', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('progress_total', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('snapshot_schema_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('definition_snapshot_json', sa.JSON(), nullable=False),
        sa.Column('result_json', sa.JSON(), nullable=True),
        sa.Column('error_code', sa.String(length=80), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('worker_id', sa.String(length=120), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(), nullable=True),
        sa.Column('cancel_requested_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_report_runs_definition_id', 'report_runs', ['definition_id'])
    op.create_index('ix_report_runs_report_id_fk', 'report_runs', ['report_id_fk'])
    op.create_index('ix_report_runs_requested_by_user_id', 'report_runs', ['requested_by_user_id'])
    op.create_index('ix_report_runs_status', 'report_runs', ['status'])
    op.create_index('ix_report_runs_worker_id', 'report_runs', ['worker_id'])
    op.create_index('ix_report_runs_lease_expires_at', 'report_runs', ['lease_expires_at'])
    op.create_index('ix_report_runs_status_created', 'report_runs', ['status', 'created_at'])

    op.create_table(
        'report_run_sections',
        sa.Column('id', _bigint_pk(), primary_key=True, autoincrement=True),
        sa.Column('run_id', sa.String(length=36), sa.ForeignKey('report_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('question_key', sa.String(length=80), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('origin', sa.String(length=20), nullable=False, server_default='definition'),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('answer', sa.Text(), nullable=True),
        sa.Column('failure_reason', sa.String(length=120), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('recovered_errors_json', sa.JSON(), nullable=True),
        sa.Column('dax_query', sa.Text(), nullable=True),
        sa.Column('tools_called_json', sa.JSON(), nullable=True),
        sa.Column('model_key', sa.String(length=120), nullable=True),
        sa.Column('model', sa.String(length=200), nullable=True),
        sa.Column('provider', sa.String(length=50), nullable=True),
        sa.Column('service_tier', sa.String(length=50), nullable=True),
        sa.Column('actual_service_tier', sa.String(length=50), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('latency_by_component_ms', sa.JSON(), nullable=True),
        sa.Column('trace_id', sa.String(length=120), nullable=True),
        sa.Column('semantic_notes_json', sa.JSON(), nullable=True),
        sa.Column('skill_routing_json', sa.JSON(), nullable=True),
        sa.Column('purpose', sa.Text(), nullable=True),
        sa.Column('related_section_keys_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('run_id', 'question_key', name='uq_report_run_section_key'),
    )
    op.create_index('ix_report_run_sections_run_id', 'report_run_sections', ['run_id'])


def downgrade():
    op.drop_table('report_run_sections')
    op.drop_table('report_runs')
    op.drop_table('analytical_report_questions')
    op.drop_table('analytical_report_definitions')
