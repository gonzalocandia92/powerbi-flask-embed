"""add_schema_embeddings

Revision ID: 8e2b7c6d4f10
Revises: f5c653ddb450
Create Date: 2026-06-04 23:40:00.000000

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
revision = '8e2b7c6d4f10'
down_revision = 'f5c653ddb450'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = inspect(bind).get_table_names()

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    if 'schema_embeddings' not in existing:
        op.create_table(
            'schema_embeddings',
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('report_id_fk', sa.BigInteger(), nullable=False),
            sa.Column('dataset_id', sa.String(length=200), nullable=False),
            sa.Column('item_type', sa.String(length=50), nullable=False),
            sa.Column('item_name', sa.String(length=255), nullable=False),
            sa.Column('content_text', sa.Text(), nullable=False),
            sa.Column('embedding', Vector(1024), nullable=False),
            sa.Column('last_updated', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['report_id_fk'], ['reports.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        with op.batch_alter_table('schema_embeddings', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_schema_embeddings_dataset_id'), ['dataset_id'], unique=False)
            batch_op.create_index(batch_op.f('ix_schema_embeddings_item_type'), ['item_type'], unique=False)
            batch_op.create_index(batch_op.f('ix_schema_embeddings_report_id_fk'), ['report_id_fk'], unique=False)


def downgrade():
    if 'schema_embeddings' in inspect(op.get_bind()).get_table_names():
        with op.batch_alter_table('schema_embeddings', schema=None) as batch_op:
            batch_op.drop_index(batch_op.f('ix_schema_embeddings_report_id_fk'))
            batch_op.drop_index(batch_op.f('ix_schema_embeddings_item_type'))
            batch_op.drop_index(batch_op.f('ix_schema_embeddings_dataset_id'))

        op.drop_table('schema_embeddings')
