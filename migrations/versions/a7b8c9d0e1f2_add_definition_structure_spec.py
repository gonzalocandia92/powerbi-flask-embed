"""add compiled structure spec columns to analytical_report_definitions (V1.3)

Additive only: nullable columns, no row is touched. Old definitions keep working (no spec ->
the default structure is used at run time).

Revision ID: a7b8c9d0e1f2
Revises: 819746ad8c3d
Create Date: 2026-10-03 21:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'a7b8c9d0e1f2'
down_revision = '819746ad8c3d'
branch_labels = None
depends_on = None

TABLE = 'analytical_report_definitions'
COLUMNS = (
    ('structure_spec_json', sa.JSON()),
    ('structure_schema_version', sa.String(length=20)),
    ('structure_input_hash', sa.String(length=64)),
    ('structure_compiled_at', sa.DateTime()),
    ('structure_compile_json', sa.JSON()),
)


def upgrade():
    # Idempotent: the columns may already exist (e.g. created via db.create_all()).
    existing = {column['name'] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}
    for name, column_type in COLUMNS:
        if name not in existing:
            op.add_column(TABLE, sa.Column(name, column_type, nullable=True))


def downgrade():
    existing = {column['name'] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for name, _ in reversed(COLUMNS):
            if name in existing:
                batch.drop_column(name)
