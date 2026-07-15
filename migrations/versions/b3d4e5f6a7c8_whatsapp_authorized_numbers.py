"""whatsapp_authorized_numbers

Revision ID: b3d4e5f6a7c8
Revises: e824431929ee
Create Date: 2026-07-11 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = 'b3d4e5f6a7c8'
down_revision = 'e824431929ee'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = inspect(bind).get_table_names()

    if 'whatsapp_authorized_numbers' not in existing:
        op.create_table(
            'whatsapp_authorized_numbers',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('phone_number', sa.String(length=30), nullable=False),
            sa.Column('empresa_id_fk', sa.BigInteger(), nullable=False),
            sa.Column('report_id_fk', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['empresa_id_fk'], ['clientes_privados.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['report_id_fk'], ['reports.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('phone_number', 'report_id_fk', name='uq_whatsapp_authorized_number_report'),
        )
        with op.batch_alter_table('whatsapp_authorized_numbers', schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f('ix_whatsapp_authorized_numbers_phone_number'), ['phone_number'], unique=False
            )

    columns = {c['name'] for c in inspect(bind).get_columns('clientes_privados')}
    if 'whatsapp_enabled' not in columns:
        with op.batch_alter_table('clientes_privados', schema=None) as batch_op:
            batch_op.add_column(
                sa.Column('whatsapp_enabled', sa.Boolean(), nullable=False, server_default=sa.false())
            )
        with op.batch_alter_table('clientes_privados', schema=None) as batch_op:
            batch_op.alter_column('whatsapp_enabled', server_default=None)

    contact_columns = {c['name'] for c in inspect(bind).get_columns('whatsapp_contacts')}
    with op.batch_alter_table('whatsapp_contacts', schema=None) as batch_op:
        if 'slug' in contact_columns:
            batch_op.drop_column('slug')
        if 'awaiting_report_selection' not in contact_columns:
            batch_op.add_column(
                sa.Column('awaiting_report_selection', sa.Boolean(), nullable=False, server_default=sa.false())
            )
        batch_op.alter_column('report_id_fk', existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table('whatsapp_contacts', schema=None) as batch_op:
        batch_op.alter_column('awaiting_report_selection', server_default=None)


def downgrade():
    bind = op.get_bind()

    contact_columns = {c['name'] for c in inspect(bind).get_columns('whatsapp_contacts')}
    with op.batch_alter_table('whatsapp_contacts', schema=None) as batch_op:
        batch_op.alter_column('report_id_fk', existing_type=sa.Integer(), nullable=False)
        if 'awaiting_report_selection' in contact_columns:
            batch_op.drop_column('awaiting_report_selection')
        if 'slug' not in contact_columns:
            batch_op.add_column(sa.Column('slug', sa.String(length=120), nullable=False, server_default=''))

    columns = {c['name'] for c in inspect(bind).get_columns('clientes_privados')}
    if 'whatsapp_enabled' in columns:
        with op.batch_alter_table('clientes_privados', schema=None) as batch_op:
            batch_op.drop_column('whatsapp_enabled')

    if 'whatsapp_authorized_numbers' in inspect(bind).get_table_names():
        with op.batch_alter_table('whatsapp_authorized_numbers', schema=None) as batch_op:
            batch_op.drop_index(batch_op.f('ix_whatsapp_authorized_numbers_phone_number'))
        op.drop_table('whatsapp_authorized_numbers')
