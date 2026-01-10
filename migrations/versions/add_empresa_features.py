"""Add empresa features and many-to-many relationships

Revision ID: add_empresa_features
Revises: d40cd067fdb3
Create Date: 2026-01-10 14:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime


# revision identifiers, used by Alembic.
revision = 'add_empresa_features'
down_revision = 'd40cd067fdb3'
branch_labels = None
depends_on = None


def upgrade():
    # Add cuit column to clientes_privados (empresas)
    with op.batch_alter_table('clientes_privados', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cuit', sa.String(length=20), nullable=True))
    
    # Add new privacy fields to report_configs
    with op.batch_alter_table('report_configs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('es_publico', sa.Boolean(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('es_privado', sa.Boolean(), nullable=False, server_default='0'))
    
    # Create association table for many-to-many relationship
    op.create_table('empresa_report_config',
        sa.Column('empresa_id', sa.BigInteger(), nullable=False),
        sa.Column('report_config_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True, default=datetime.utcnow),
        sa.ForeignKeyConstraint(['empresa_id'], ['clientes_privados.id'], ),
        sa.ForeignKeyConstraint(['report_config_id'], ['report_configs.id'], ),
        sa.PrimaryKeyConstraint('empresa_id', 'report_config_id')
    )
    
    # Create futuras_empresas table
    op.create_table('futuras_empresas',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('external_id', sa.String(length=200), nullable=False),
        sa.Column('nombre', sa.String(length=200), nullable=False),
        sa.Column('cuit', sa.String(length=20), nullable=True),
        sa.Column('email', sa.String(length=200), nullable=True),
        sa.Column('telefono', sa.String(length=50), nullable=True),
        sa.Column('direccion', sa.String(length=500), nullable=True),
        sa.Column('datos_adicionales', sa.Text(), nullable=True),
        sa.Column('estado', sa.String(length=20), nullable=False, server_default='pendiente'),
        sa.Column('fecha_recepcion', sa.DateTime(), nullable=False, default=datetime.utcnow),
        sa.Column('fecha_procesamiento', sa.DateTime(), nullable=True),
        sa.Column('procesado_por_user_id', sa.BigInteger(), nullable=True),
        sa.Column('empresa_id', sa.BigInteger(), nullable=True),
        sa.Column('notas', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['empresa_id'], ['clientes_privados.id'], ),
        sa.ForeignKeyConstraint(['procesado_por_user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('external_id')
    )
    
    # Migrate existing data from tipo_privacidad to new fields
    # Reports with tipo_privacidad='publico' -> es_publico=True, es_privado=False
    # Reports with tipo_privacidad='privado' -> es_publico=False, es_privado=True
    op.execute("""
        UPDATE report_configs 
        SET es_publico = CASE WHEN tipo_privacidad = 'publico' OR tipo_privacidad IS NULL THEN 1 ELSE 0 END,
            es_privado = CASE WHEN tipo_privacidad = 'privado' THEN 1 ELSE 0 END
    """)
    
    # Migrate existing cliente_privado_id relationships to many-to-many table
    op.execute("""
        INSERT INTO empresa_report_config (empresa_id, report_config_id, created_at)
        SELECT cliente_privado_id, id, CURRENT_TIMESTAMP
        FROM report_configs
        WHERE cliente_privado_id IS NOT NULL
    """)


def downgrade():
    # Drop futuras_empresas table
    op.drop_table('futuras_empresas')
    
    # Drop association table
    op.drop_table('empresa_report_config')
    
    # Remove new columns from report_configs
    with op.batch_alter_table('report_configs', schema=None) as batch_op:
        batch_op.drop_column('es_privado')
        batch_op.drop_column('es_publico')
    
    # Remove cuit column from clientes_privados
    with op.batch_alter_table('clientes_privados', schema=None) as batch_op:
        batch_op.drop_column('cuit')
