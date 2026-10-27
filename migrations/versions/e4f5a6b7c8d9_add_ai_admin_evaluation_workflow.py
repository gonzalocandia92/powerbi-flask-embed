"""add AI administration and recoverable evaluation workflow

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-09-20 14:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'e4f5a6b7c8d9'
down_revision = 'd3e4f5a6b7c8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('ai_model_role_assignments') as batch_op:
        batch_op.alter_column('model_id', existing_type=sa.BigInteger(), nullable=True)
        batch_op.add_column(sa.Column('strategy', sa.String(length=30), nullable=False,
                                      server_default='model'))

    with op.batch_alter_table('model_evaluation_runs') as batch_op:
        batch_op.add_column(sa.Column('requested_by_user_id', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('total_cases', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('completed_cases', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('worker_id', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('heartbeat_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('lease_expires_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('cancel_requested_at', sa.DateTime(), nullable=True))
        batch_op.create_index('ix_model_evaluation_runs_requested_by_user_id', ['requested_by_user_id'])
        batch_op.create_index('ix_model_evaluation_runs_worker_id', ['worker_id'])
        batch_op.create_index('ix_model_evaluation_runs_lease_expires_at', ['lease_expires_at'])
        batch_op.create_foreign_key(
            'fk_model_evaluation_runs_requested_by_user_id_users', 'users',
            ['requested_by_user_id'], ['id'], ondelete='SET NULL',
        )

    with op.batch_alter_table('model_evaluation_cases') as batch_op:
        batch_op.add_column(sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('review_status', sa.String(length=30), nullable=False,
                                      server_default='unreviewed'))
        batch_op.add_column(sa.Column('review_notes', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('reviewed_by_user_id', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('reviewed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('started_at', sa.DateTime(), nullable=True))
        batch_op.create_index('ix_model_evaluation_cases_review_status', ['review_status'])
        batch_op.create_unique_constraint(
            'uq_model_evaluation_case_run_sequence', ['run_id', 'sequence_index']
        )
        batch_op.create_foreign_key(
            'fk_model_evaluation_cases_reviewed_by_user_id_users', 'users',
            ['reviewed_by_user_id'], ['id'], ondelete='SET NULL',
        )


def downgrade():
    with op.batch_alter_table('model_evaluation_cases') as batch_op:
        batch_op.drop_constraint(
            'fk_model_evaluation_cases_reviewed_by_user_id_users', type_='foreignkey'
        )
        batch_op.drop_constraint('uq_model_evaluation_case_run_sequence', type_='unique')
        batch_op.drop_index('ix_model_evaluation_cases_review_status')
        for column in (
            'started_at', 'reviewed_at', 'reviewed_by_user_id', 'review_notes',
            'review_status', 'attempt_count',
        ):
            batch_op.drop_column(column)

    with op.batch_alter_table('model_evaluation_runs') as batch_op:
        batch_op.drop_constraint(
            'fk_model_evaluation_runs_requested_by_user_id_users', type_='foreignkey'
        )
        batch_op.drop_index('ix_model_evaluation_runs_lease_expires_at')
        batch_op.drop_index('ix_model_evaluation_runs_worker_id')
        batch_op.drop_index('ix_model_evaluation_runs_requested_by_user_id')
        for column in (
            'cancel_requested_at', 'lease_expires_at', 'heartbeat_at', 'worker_id',
            'completed_cases', 'total_cases', 'requested_by_user_id',
        ):
            batch_op.drop_column(column)

    # Legacy schema cannot represent disabled/embeddings strategies because it
    # requires a model_id. Remove only those non-model assignments on downgrade.
    op.execute(sa.text("DELETE FROM ai_model_role_assignments WHERE model_id IS NULL"))
    with op.batch_alter_table('ai_model_role_assignments') as batch_op:
        batch_op.drop_column('strategy')
        batch_op.alter_column('model_id', existing_type=sa.BigInteger(), nullable=False)
