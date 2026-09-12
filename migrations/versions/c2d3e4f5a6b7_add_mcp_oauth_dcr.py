"""add dynamic client registration metadata to MCP OAuth clients

Revision ID: c2d3e4f5a6b7
Revises: b0d5f8a3c7e2
Create Date: 2026-09-05 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'c2d3e4f5a6b7'
down_revision = 'b0d5f8a3c7e2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('mcp_oauth_clients') as batch_op:
        batch_op.add_column(
            sa.Column(
                'registration_method',
                sa.String(length=16),
                nullable=False,
                server_default='static',
            )
        )
        batch_op.alter_column(
            'client_secret_hash',
            existing_type=sa.String(length=256),
            nullable=True,
        )
        batch_op.create_check_constraint(
            'ck_mcp_oauth_clients_registration_method',
            "registration_method IN ('static', 'dynamic')",
        )

    op.execute(
        sa.text(
            "UPDATE mcp_oauth_clients SET client_secret_hash = NULL "
            "WHERE token_endpoint_auth_method = 'none'"
        )
    )


def downgrade():
    op.execute(
        sa.text(
            "UPDATE mcp_oauth_clients "
            "SET client_secret_hash = 'public-client-no-secret' "
            "WHERE client_secret_hash IS NULL"
        )
    )
    with op.batch_alter_table('mcp_oauth_clients') as batch_op:
        batch_op.drop_constraint(
            'ck_mcp_oauth_clients_registration_method', type_='check'
        )
        batch_op.alter_column(
            'client_secret_hash',
            existing_type=sa.String(length=256),
            nullable=False,
        )
        batch_op.drop_column('registration_method')
