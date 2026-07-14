"""add analytics skills

Revision ID: c1d2e3f4a5b6
Revises: 7f3a9c2e1b6d, b7c8d9e0f1a2
Create Date: 2026-07-01 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - fallback for environments without pgvector installed
    class Vector(sa.types.UserDefinedType):
        cache_ok = True

        def __init__(self, dimensions):
            self.dimensions = dimensions

        def get_col_spec(self, **kw):
            return f"VECTOR({self.dimensions})"


# revision identifiers, used by Alembic.
revision = 'c1d2e3f4a5b6'
down_revision = ('7f3a9c2e1b6d', 'b7c8d9e0f1a2')
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing_tables = set(inspect(bind).get_table_names())
    dialect_name = bind.dialect.name

    if dialect_name != "sqlite":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    if "analytics_skills" not in existing_tables:
        op.create_table(
            "analytics_skills",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
            sa.Column("skill_key", sa.String(length=120), nullable=False),
            sa.Column("domain_key", sa.String(length=120), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("report_id_fk", sa.BigInteger(), nullable=True),
            sa.Column("empresa_id_fk", sa.BigInteger(), nullable=True),
            sa.Column("dataset_id", sa.String(length=200), nullable=True),
            sa.Column("routing_text", sa.Text(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("embedding", Vector(1024), nullable=True),
            sa.Column("embedding_model", sa.String(length=120), nullable=True),
            sa.Column("embedded_at", sa.DateTime(), nullable=True),
            sa.Column("routing_document_hash", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                """
                (
                    report_id_fk IS NULL AND empresa_id_fk IS NULL AND dataset_id IS NULL
                ) OR (
                    report_id_fk IS NULL AND empresa_id_fk IS NOT NULL AND dataset_id IS NULL
                ) OR (
                    report_id_fk IS NULL AND empresa_id_fk IS NULL AND dataset_id IS NOT NULL
                ) OR (
                    report_id_fk IS NOT NULL AND empresa_id_fk IS NULL AND dataset_id IS NULL
                )
                """,
                name="ck_analytics_skills_single_scope",
            ),
            sa.ForeignKeyConstraint(["empresa_id_fk"], ["clientes_privados.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["report_id_fk"], ["reports.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("analytics_skills", schema=None) as batch_op:
            batch_op.create_index("ix_analytics_skills_skill_key", ["skill_key"], unique=False)
            batch_op.create_index("ix_analytics_skills_domain_key", ["domain_key"], unique=False)
            batch_op.create_index("ix_analytics_skills_report_id_fk", ["report_id_fk"], unique=False)
            batch_op.create_index("ix_analytics_skills_empresa_id_fk", ["empresa_id_fk"], unique=False)
            batch_op.create_index("ix_analytics_skills_dataset_id", ["dataset_id"], unique=False)
            batch_op.create_index("ix_analytics_skills_is_active", ["is_active"], unique=False)
            batch_op.create_index(
                "ix_analytics_skills_scope",
                ["report_id_fk", "dataset_id", "empresa_id_fk"],
                unique=False,
            )


def downgrade():
    bind = op.get_bind()
    existing_tables = set(inspect(bind).get_table_names())

    if "analytics_skills" in existing_tables:
        with op.batch_alter_table("analytics_skills", schema=None) as batch_op:
            batch_op.drop_index("ix_analytics_skills_scope")
            batch_op.drop_index("ix_analytics_skills_is_active")
            batch_op.drop_index("ix_analytics_skills_dataset_id")
            batch_op.drop_index("ix_analytics_skills_empresa_id_fk")
            batch_op.drop_index("ix_analytics_skills_report_id_fk")
            batch_op.drop_index("ix_analytics_skills_domain_key")
            batch_op.drop_index("ix_analytics_skills_skill_key")
        op.drop_table("analytics_skills")
