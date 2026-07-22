"""expand analytics skill fields

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-07-01 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = 'd2e3f4a5b6c7'
down_revision = 'c1d2e3f4a5b6'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "analytics_skills" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("analytics_skills")}
    with op.batch_alter_table("analytics_skills", schema=None) as batch_op:
        if "description" not in existing_columns:
            batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        if "priority" not in existing_columns:
            batch_op.add_column(sa.Column("priority", sa.String(length=20), nullable=False, server_default="normal"))
        if "enforcement_mode" not in existing_columns:
            batch_op.add_column(sa.Column("enforcement_mode", sa.String(length=30), nullable=False, server_default="soft"))
        if "confidence_label" not in existing_columns:
            batch_op.add_column(sa.Column("confidence_label", sa.String(length=30), nullable=True))
        if "routing_json" not in existing_columns:
            batch_op.add_column(sa.Column("routing_json", sa.JSON(), nullable=True))
        if "validation_json" not in existing_columns:
            batch_op.add_column(sa.Column("validation_json", sa.JSON(), nullable=True))

    inspector = inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("analytics_skills")}
    with op.batch_alter_table("analytics_skills", schema=None) as batch_op:
        if "ix_analytics_skills_priority" not in indexes:
            batch_op.create_index("ix_analytics_skills_priority", ["priority"], unique=False)
        if "ix_analytics_skills_enforcement_mode" not in indexes:
            batch_op.create_index("ix_analytics_skills_enforcement_mode", ["enforcement_mode"], unique=False)
        if "ix_analytics_skills_confidence_label" not in indexes:
            batch_op.create_index("ix_analytics_skills_confidence_label", ["confidence_label"], unique=False)

    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                """
                UPDATE analytics_skills
                SET
                    enforcement_mode = COALESCE(metadata_json ->> 'enforcement_mode', enforcement_mode, 'soft'),
                    confidence_label = COALESCE(metadata_json ->> 'confidence', confidence_label)
                """
            )
        )

    if bind.dialect.name != "sqlite":
        with op.batch_alter_table("analytics_skills", schema=None) as batch_op:
            batch_op.alter_column("priority", server_default=None)
            batch_op.alter_column("enforcement_mode", server_default=None)


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "analytics_skills" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("analytics_skills")}
    indexes = {index["name"] for index in inspector.get_indexes("analytics_skills")}
    with op.batch_alter_table("analytics_skills", schema=None) as batch_op:
        if "ix_analytics_skills_confidence_label" in indexes:
            batch_op.drop_index("ix_analytics_skills_confidence_label")
        if "ix_analytics_skills_enforcement_mode" in indexes:
            batch_op.drop_index("ix_analytics_skills_enforcement_mode")
        if "ix_analytics_skills_priority" in indexes:
            batch_op.drop_index("ix_analytics_skills_priority")
        if "validation_json" in columns:
            batch_op.drop_column("validation_json")
        if "routing_json" in columns:
            batch_op.drop_column("routing_json")
        if "confidence_label" in columns:
            batch_op.drop_column("confidence_label")
        if "enforcement_mode" in columns:
            batch_op.drop_column("enforcement_mode")
        if "priority" in columns:
            batch_op.drop_column("priority")
        if "description" in columns:
            batch_op.drop_column("description")
