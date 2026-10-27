"""add KLARA catalog and evaluation baseline tables

Revision ID: ebcd339309df
Revises: c2d3e4f5a6b7
Create Date: 2026-09-20 03:30:00.000000

This revision restores the migration identifier already applied to existing
development databases.  The following revision completes the integration,
adds legacy-table columns and seeds safe defaults.
"""

from alembic import op
import sqlalchemy as sa


revision = 'ebcd339309df'
down_revision = 'c2d3e4f5a6b7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ai_model_configs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('model_key', sa.String(length=120), nullable=False),
        sa.Column('display_name', sa.String(length=160), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('physical_model', sa.String(length=200), nullable=False),
        sa.Column('gateway', sa.String(length=50), nullable=False, server_default='direct'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('client_selectable', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('validation_status', sa.String(length=30), nullable=False, server_default='pending'),
        sa.Column('validated_at', sa.DateTime(), nullable=True),
        sa.Column('supports_tools', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('supports_reasoning', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('supports_cache_key', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('supports_flex', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('context_window', sa.Integer(), nullable=False, server_default='200000'),
        sa.Column('max_output_tokens', sa.Integer(), nullable=False, server_default='4096'),
        sa.Column('default_reasoning_effort', sa.String(length=30), nullable=True),
        sa.Column('default_service_tier', sa.String(length=50), nullable=True),
        sa.Column('pricing_tier', sa.String(length=50), nullable=True),
        sa.Column('provider_options_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('model_key'),
    )
    for name, columns in (
        ('ix_ai_model_configs_model_key', ['model_key']),
        ('ix_ai_model_configs_provider', ['provider']),
        ('ix_ai_model_configs_enabled', ['enabled']),
        ('ix_ai_model_configs_client_selectable', ['client_selectable']),
        ('ix_ai_model_configs_validation_status', ['validation_status']),
    ):
        op.create_index(name, 'ai_model_configs', columns, unique=name.endswith('model_key'))

    op.create_table(
        'ai_model_role_assignments',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('model_id', sa.BigInteger(), nullable=False),
        sa.Column('role', sa.String(length=50), nullable=False),
        sa.Column('scope_type', sa.String(length=20), nullable=False),
        sa.Column('scope_id', sa.String(length=120), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('max_output_tokens', sa.Integer(), nullable=True),
        sa.Column('reasoning_effort', sa.String(length=30), nullable=True),
        sa.Column('service_tier', sa.String(length=50), nullable=True),
        sa.Column('provider_options_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['model_id'], ['ai_model_configs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_model_role_assignments_model_id', 'ai_model_role_assignments', ['model_id'])
    op.create_index('ix_ai_model_role_assignments_role', 'ai_model_role_assignments', ['role'])
    op.create_index('ix_ai_model_role_assignments_scope_type', 'ai_model_role_assignments', ['scope_type'])
    op.create_index('ix_ai_model_role_assignments_scope_id', 'ai_model_role_assignments', ['scope_id'])
    op.create_index('ix_ai_model_role_scope', 'ai_model_role_assignments', ['role', 'scope_type', 'scope_id', 'is_active'])

    op.create_table(
        'ai_model_grants',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('model_id', sa.BigInteger(), nullable=False),
        sa.Column('scope_type', sa.String(length=20), nullable=False),
        sa.Column('scope_id', sa.String(length=120), nullable=True),
        sa.Column('allowed', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('client_selectable', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['model_id'], ['ai_model_configs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_model_grants_model_id', 'ai_model_grants', ['model_id'])
    op.create_index('ix_ai_model_grants_scope_type', 'ai_model_grants', ['scope_type'])
    op.create_index('ix_ai_model_grants_scope_id', 'ai_model_grants', ['scope_id'])
    op.create_index('ix_ai_model_grant_scope', 'ai_model_grants', ['scope_type', 'scope_id', 'allowed'])

    op.create_table(
        'model_evaluation_runs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('report_id_fk', sa.BigInteger(), nullable=True),
        sa.Column('mode', sa.String(length=30), nullable=False),
        sa.Column('cache_mode', sa.String(length=30), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('configuration_json', sa.JSON(), nullable=True),
        sa.Column('summary_json', sa.JSON(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['report_id_fk'], ['reports.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_model_evaluation_runs_report_id_fk', 'model_evaluation_runs', ['report_id_fk'])
    op.create_index('ix_model_evaluation_runs_status', 'model_evaluation_runs', ['status'])

    op.create_table(
        'model_evaluation_cases',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.BigInteger(), nullable=False),
        sa.Column('sequence_index', sa.Integer(), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('expected_answer', sa.Text(), nullable=True),
        sa.Column('answer', sa.Text(), nullable=True),
        sa.Column('model_key', sa.String(length=120), nullable=False),
        sa.Column('cache_mode', sa.String(length=30), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=True),
        sa.Column('physical_model', sa.String(length=200), nullable=True),
        sa.Column('actual_model', sa.String(length=200), nullable=True),
        sa.Column('gateway', sa.String(length=50), nullable=True),
        sa.Column('service_tier', sa.String(length=50), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_read_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_write_tokens', sa.Integer(), nullable=True),
        sa.Column('reasoning_tokens', sa.Integer(), nullable=True),
        sa.Column('main_model_cost', sa.Float(), nullable=True),
        sa.Column('decision_layer_cost', sa.Float(), nullable=True),
        sa.Column('pipeline_total_cost', sa.Float(), nullable=True),
        sa.Column('selected_skills_json', sa.JSON(), nullable=True),
        sa.Column('candidate_skills_json', sa.JSON(), nullable=True),
        sa.Column('complexity_assessment_json', sa.JSON(), nullable=True),
        sa.Column('execution_policy_json', sa.JSON(), nullable=True),
        sa.Column('tools_called_json', sa.JSON(), nullable=True),
        sa.Column('dax_query', sa.Text(), nullable=True),
        sa.Column('tool_rounds', sa.Integer(), nullable=True),
        sa.Column('failure_reason', sa.String(length=120), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('trace_id', sa.String(length=120), nullable=True),
        sa.Column('metrics_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['run_id'], ['model_evaluation_runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_model_evaluation_cases_run_id', 'model_evaluation_cases', ['run_id'])
    op.create_index('ix_model_evaluation_cases_model_key', 'model_evaluation_cases', ['model_key'])
    op.create_index('ix_model_evaluation_cases_status', 'model_evaluation_cases', ['status'])
    op.create_index('ix_model_evaluation_case_run_model', 'model_evaluation_cases', ['run_id', 'model_key'])


def downgrade():
    op.drop_index('ix_model_evaluation_case_run_model', table_name='model_evaluation_cases')
    op.drop_index('ix_model_evaluation_cases_status', table_name='model_evaluation_cases')
    op.drop_index('ix_model_evaluation_cases_model_key', table_name='model_evaluation_cases')
    op.drop_index('ix_model_evaluation_cases_run_id', table_name='model_evaluation_cases')
    op.drop_table('model_evaluation_cases')
    op.drop_index('ix_model_evaluation_runs_status', table_name='model_evaluation_runs')
    op.drop_index('ix_model_evaluation_runs_report_id_fk', table_name='model_evaluation_runs')
    op.drop_table('model_evaluation_runs')
    op.drop_index('ix_ai_model_grant_scope', table_name='ai_model_grants')
    op.drop_index('ix_ai_model_grants_scope_id', table_name='ai_model_grants')
    op.drop_index('ix_ai_model_grants_scope_type', table_name='ai_model_grants')
    op.drop_index('ix_ai_model_grants_model_id', table_name='ai_model_grants')
    op.drop_table('ai_model_grants')
    op.drop_index('ix_ai_model_role_scope', table_name='ai_model_role_assignments')
    op.drop_index('ix_ai_model_role_assignments_scope_id', table_name='ai_model_role_assignments')
    op.drop_index('ix_ai_model_role_assignments_scope_type', table_name='ai_model_role_assignments')
    op.drop_index('ix_ai_model_role_assignments_role', table_name='ai_model_role_assignments')
    op.drop_index('ix_ai_model_role_assignments_model_id', table_name='ai_model_role_assignments')
    op.drop_table('ai_model_role_assignments')
    for name in (
        'ix_ai_model_configs_validation_status', 'ix_ai_model_configs_client_selectable',
        'ix_ai_model_configs_enabled', 'ix_ai_model_configs_provider',
        'ix_ai_model_configs_model_key',
    ):
        op.drop_index(name, table_name='ai_model_configs')
    op.drop_table('ai_model_configs')
