"""add_ai_billing_ledger

Revision ID: c9d8e7f6a5b4
Revises: b1c2d3e4f5a6
Create Date: 2026-06-10 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from datetime import datetime, timezone


# revision identifiers, used by Alembic.
revision = 'c9d8e7f6a5b4'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def _table_names(bind):
    return set(inspect(bind).get_table_names())


def _column_names(bind, table_name):
    return {col["name"] for col in inspect(bind).get_columns(table_name)}


def upgrade():
    bind = op.get_bind()
    existing_tables = _table_names(bind)

    report_columns = _column_names(bind, "reports")
    with op.batch_alter_table("reports", schema=None) as batch_op:
        if "empresa_facturadora_id" not in report_columns:
            batch_op.add_column(sa.Column("empresa_facturadora_id", sa.BigInteger(), nullable=True))
            batch_op.create_foreign_key(
                "fk_reports_empresa_facturadora_id_clientes_privados",
                "clientes_privados",
                ["empresa_facturadora_id"],
                ["id"],
            )
            batch_op.create_index("ix_reports_empresa_facturadora_id", ["empresa_facturadora_id"], unique=False)

    chat_session_columns = _column_names(bind, "chat_sessions")
    with op.batch_alter_table("chat_sessions", schema=None) as batch_op:
        if "workspace_id_fk" not in chat_session_columns:
            batch_op.add_column(sa.Column("workspace_id_fk", sa.BigInteger(), nullable=True))
            batch_op.create_foreign_key(
                "fk_chat_sessions_workspace_id_fk_workspaces",
                "workspaces",
                ["workspace_id_fk"],
                ["id"],
            )
            batch_op.create_index("ix_chat_sessions_workspace_id_fk", ["workspace_id_fk"], unique=False)
        if "report_id_fk" not in chat_session_columns:
            batch_op.add_column(sa.Column("report_id_fk", sa.BigInteger(), nullable=True))
            batch_op.create_foreign_key(
                "fk_chat_sessions_report_id_fk_reports",
                "reports",
                ["report_id_fk"],
                ["id"],
            )
            batch_op.create_index("ix_chat_sessions_report_id_fk", ["report_id_fk"], unique=False)
        if "empresa_id" not in chat_session_columns:
            batch_op.add_column(sa.Column("empresa_id", sa.BigInteger(), nullable=True))
            batch_op.create_foreign_key(
                "fk_chat_sessions_empresa_id_clientes_privados",
                "clientes_privados",
                ["empresa_id"],
                ["id"],
            )
            batch_op.create_index("ix_chat_sessions_empresa_id", ["empresa_id"], unique=False)

    chat_message_columns = _column_names(bind, "chat_messages")
    with op.batch_alter_table("chat_messages", schema=None) as batch_op:
        if "total_cost_usd" not in chat_message_columns:
            batch_op.add_column(sa.Column("total_cost_usd", sa.Float(), nullable=True, server_default="0"))
        if "total_input_tokens" not in chat_message_columns:
            batch_op.add_column(sa.Column("total_input_tokens", sa.Integer(), nullable=True, server_default="0"))
        if "total_output_tokens" not in chat_message_columns:
            batch_op.add_column(sa.Column("total_output_tokens", sa.Integer(), nullable=True, server_default="0"))

    if "billing_limits" not in existing_tables:
        op.create_table(
            "billing_limits",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("scope_type", sa.String(length=20), nullable=False),
            sa.Column("scope_id", sa.String(length=120), nullable=True),
            sa.Column("period_type", sa.String(length=30), nullable=False),
            sa.Column("limit_usd", sa.Float(), nullable=False),
            sa.Column("currency", sa.String(length=10), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("starts_at", sa.DateTime(), nullable=True),
            sa.Column("ends_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("billing_limits", schema=None) as batch_op:
            batch_op.create_index("ix_billing_limits_scope_type", ["scope_type"], unique=False)
            batch_op.create_index("ix_billing_limits_scope_id", ["scope_id"], unique=False)

    if "ai_model_pricing" not in existing_tables:
        op.create_table(
            "ai_model_pricing",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("provider", sa.String(length=50), nullable=False),
            sa.Column("model", sa.String(length=120), nullable=False),
            sa.Column("event_type", sa.String(length=30), nullable=False),
            sa.Column("currency", sa.String(length=10), nullable=False),
            sa.Column("input_cost_per_million_usd", sa.Float(), nullable=True),
            sa.Column("output_cost_per_million_usd", sa.Float(), nullable=True),
            sa.Column("cache_write_cost_per_million_usd", sa.Float(), nullable=True),
            sa.Column("cache_read_cost_per_million_usd", sa.Float(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("effective_from", sa.DateTime(), nullable=False),
            sa.Column("effective_to", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("ai_model_pricing", schema=None) as batch_op:
            batch_op.create_index("ix_ai_model_pricing_provider", ["provider"], unique=False)
            batch_op.create_index("ix_ai_model_pricing_model", ["model"], unique=False)
            batch_op.create_index("ix_ai_model_pricing_event_type", ["event_type"], unique=False)
            batch_op.create_index("ix_ai_model_pricing_effective_from", ["effective_from"], unique=False)

    if "ai_usage_events" not in existing_tables:
        op.create_table(
            "ai_usage_events",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("session_id", sa.BigInteger(), nullable=True),
            sa.Column("message_id", sa.BigInteger(), nullable=True),
            sa.Column("workspace_id_fk", sa.BigInteger(), nullable=True),
            sa.Column("report_id_fk", sa.BigInteger(), nullable=True),
            sa.Column("empresa_id", sa.BigInteger(), nullable=True),
            sa.Column("billing_scope_type", sa.String(length=20), nullable=False),
            sa.Column("billing_scope_id", sa.String(length=120), nullable=True),
            sa.Column("source_type", sa.String(length=30), nullable=False),
            sa.Column("trigger_type", sa.String(length=30), nullable=False),
            sa.Column("provider", sa.String(length=50), nullable=False),
            sa.Column("model", sa.String(length=120), nullable=False),
            sa.Column("event_type", sa.String(length=30), nullable=False),
            sa.Column("operation_name", sa.String(length=120), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("input_tokens", sa.Integer(), nullable=True),
            sa.Column("output_tokens", sa.Integer(), nullable=True),
            sa.Column("total_tokens", sa.Integer(), nullable=True),
            sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
            sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
            sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
            sa.Column("input_cost_usd", sa.Float(), nullable=True),
            sa.Column("output_cost_usd", sa.Float(), nullable=True),
            sa.Column("cache_write_cost_usd", sa.Float(), nullable=True),
            sa.Column("cache_read_cost_usd", sa.Float(), nullable=True),
            sa.Column("total_cost_usd", sa.Float(), nullable=True),
            sa.Column("currency", sa.String(length=10), nullable=False),
            sa.Column("pricing_id", sa.BigInteger(), nullable=True),
            sa.Column("trace_id", sa.String(length=120), nullable=True),
            sa.Column("observation_id", sa.String(length=120), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.ForeignKeyConstraint(["empresa_id"], ["clientes_privados.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["message_id"], ["chat_messages.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["pricing_id"], ["ai_model_pricing.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["report_id_fk"], ["reports.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["workspace_id_fk"], ["workspaces.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("ai_usage_events", schema=None) as batch_op:
            batch_op.create_index("ix_ai_usage_events_created_at", ["created_at"], unique=False)
            batch_op.create_index("ix_ai_usage_events_session_id", ["session_id"], unique=False)
            batch_op.create_index("ix_ai_usage_events_message_id", ["message_id"], unique=False)
            batch_op.create_index("ix_ai_usage_events_workspace_id_fk", ["workspace_id_fk"], unique=False)
            batch_op.create_index("ix_ai_usage_events_report_id_fk", ["report_id_fk"], unique=False)
            batch_op.create_index("ix_ai_usage_events_empresa_id", ["empresa_id"], unique=False)
            batch_op.create_index("ix_ai_usage_events_billing_scope_type", ["billing_scope_type"], unique=False)
            batch_op.create_index("ix_ai_usage_events_billing_scope_id", ["billing_scope_id"], unique=False)
            batch_op.create_index("ix_ai_usage_events_source_type", ["source_type"], unique=False)
            batch_op.create_index("ix_ai_usage_events_provider", ["provider"], unique=False)
            batch_op.create_index("ix_ai_usage_events_model", ["model"], unique=False)
            batch_op.create_index("ix_ai_usage_events_event_type", ["event_type"], unique=False)
            batch_op.create_index("ix_ai_usage_events_pricing_id", ["pricing_id"], unique=False)
            batch_op.create_index("ix_ai_usage_events_empresa_created", ["empresa_id", "created_at"], unique=False)
            batch_op.create_index("ix_ai_usage_events_report_created", ["report_id_fk", "created_at"], unique=False)
            batch_op.create_index("ix_ai_usage_events_workspace_created", ["workspace_id_fk", "created_at"], unique=False)
            batch_op.create_index("ix_ai_usage_events_provider_model", ["provider", "model"], unique=False)

    billing_limits = sa.table(
        "billing_limits",
        sa.column("scope_type", sa.String()),
        sa.column("scope_id", sa.String()),
        sa.column("period_type", sa.String()),
        sa.column("limit_usd", sa.Float()),
        sa.column("currency", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("starts_at", sa.DateTime()),
        sa.column("ends_at", sa.DateTime()),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )
    has_global_limit = bind.execute(
        sa.select(sa.func.count()).select_from(billing_limits).where(
            billing_limits.c.scope_type == "global",
            billing_limits.c.scope_id.is_(None),
            billing_limits.c.period_type == "rolling_30d",
        )
    ).scalar()
    if not has_global_limit:
        now = datetime.now(timezone.utc)
        op.bulk_insert(
            billing_limits,
            [
                {
                    "scope_type": "global",
                    "scope_id": None,
                    "period_type": "rolling_30d",
                    "limit_usd": 5.0,
                    "currency": "USD",
                    "is_active": True,
                    "starts_at": None,
                    "ends_at": None,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )


def downgrade():
    bind = op.get_bind()
    existing_tables = _table_names(bind)

    if "ai_usage_events" in existing_tables:
        with op.batch_alter_table("ai_usage_events", schema=None) as batch_op:
            batch_op.drop_index("ix_ai_usage_events_provider_model")
            batch_op.drop_index("ix_ai_usage_events_workspace_created")
            batch_op.drop_index("ix_ai_usage_events_report_created")
            batch_op.drop_index("ix_ai_usage_events_empresa_created")
            batch_op.drop_index("ix_ai_usage_events_pricing_id")
            batch_op.drop_index("ix_ai_usage_events_event_type")
            batch_op.drop_index("ix_ai_usage_events_model")
            batch_op.drop_index("ix_ai_usage_events_provider")
            batch_op.drop_index("ix_ai_usage_events_source_type")
            batch_op.drop_index("ix_ai_usage_events_billing_scope_id")
            batch_op.drop_index("ix_ai_usage_events_billing_scope_type")
            batch_op.drop_index("ix_ai_usage_events_empresa_id")
            batch_op.drop_index("ix_ai_usage_events_report_id_fk")
            batch_op.drop_index("ix_ai_usage_events_workspace_id_fk")
            batch_op.drop_index("ix_ai_usage_events_message_id")
            batch_op.drop_index("ix_ai_usage_events_session_id")
            batch_op.drop_index("ix_ai_usage_events_created_at")
        op.drop_table("ai_usage_events")

    if "ai_model_pricing" in existing_tables:
        with op.batch_alter_table("ai_model_pricing", schema=None) as batch_op:
            batch_op.drop_index("ix_ai_model_pricing_effective_from")
            batch_op.drop_index("ix_ai_model_pricing_event_type")
            batch_op.drop_index("ix_ai_model_pricing_model")
            batch_op.drop_index("ix_ai_model_pricing_provider")
        op.drop_table("ai_model_pricing")

    if "billing_limits" in existing_tables:
        with op.batch_alter_table("billing_limits", schema=None) as batch_op:
            batch_op.drop_index("ix_billing_limits_scope_id")
            batch_op.drop_index("ix_billing_limits_scope_type")
        op.drop_table("billing_limits")

    chat_message_columns = _column_names(bind, "chat_messages")
    with op.batch_alter_table("chat_messages", schema=None) as batch_op:
        if "total_output_tokens" in chat_message_columns:
            batch_op.drop_column("total_output_tokens")
        if "total_input_tokens" in chat_message_columns:
            batch_op.drop_column("total_input_tokens")
        if "total_cost_usd" in chat_message_columns:
            batch_op.drop_column("total_cost_usd")

    chat_session_columns = _column_names(bind, "chat_sessions")
    with op.batch_alter_table("chat_sessions", schema=None) as batch_op:
        if "empresa_id" in chat_session_columns:
            batch_op.drop_index("ix_chat_sessions_empresa_id")
            batch_op.drop_column("empresa_id")
        if "report_id_fk" in chat_session_columns:
            batch_op.drop_index("ix_chat_sessions_report_id_fk")
            batch_op.drop_column("report_id_fk")
        if "workspace_id_fk" in chat_session_columns:
            batch_op.drop_index("ix_chat_sessions_workspace_id_fk")
            batch_op.drop_column("workspace_id_fk")

    report_columns = _column_names(bind, "reports")
    with op.batch_alter_table("reports", schema=None) as batch_op:
        if "empresa_facturadora_id" in report_columns:
            batch_op.drop_index("ix_reports_empresa_facturadora_id")
            batch_op.drop_column("empresa_facturadora_id")
