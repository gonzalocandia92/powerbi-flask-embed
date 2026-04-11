"""Refactor domain: remove ReportConfig, add hierarchical FKs

Client (1) -> (N) Tenant (1) -> (N) Workspace (1) -> (N) Report (1) -> (N) PublicLink
Report (M) <-> (N) Empresa

Revision ID: refactor_domain_001
Revises: 117b07c24e37
Create Date: 2026-04-10
"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime

# revision identifiers
revision = 'refactor_domain_001'
down_revision = '117b07c24e37'
branch_labels = None
depends_on = None


def upgrade():
    # --- Step 1: Add new FK columns (nullable initially for data migration) ---

    # Tenant -> Client
    op.add_column('tenants', sa.Column('client_id_fk', sa.BigInteger(), nullable=True))

    # Workspace -> Tenant
    op.add_column('workspaces', sa.Column('tenant_id_fk', sa.BigInteger(), nullable=True))

    # Report -> Workspace, UsuarioPBI, privacy fields
    op.add_column('reports', sa.Column('workspace_id_fk', sa.BigInteger(), nullable=True))
    op.add_column('reports', sa.Column('usuario_pbi_id', sa.BigInteger(), nullable=True))
    op.add_column('reports', sa.Column('es_publico', sa.Boolean(), nullable=True, server_default='true'))
    op.add_column('reports', sa.Column('es_privado', sa.Boolean(), nullable=True, server_default='false'))
    op.add_column('reports', sa.Column('created_at', sa.DateTime(), nullable=True))

    # PublicLink: add report_id_fk (will migrate from report_config_id)
    op.add_column('public_links', sa.Column('report_id_fk', sa.BigInteger(), nullable=True))

    # --- Step 2: Create empresa_report M2M table ---
    op.create_table(
        'empresa_report',
        sa.Column('empresa_id', sa.BigInteger(), sa.ForeignKey('clientes_privados.id'), primary_key=True),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('reports.id'), primary_key=True),
        sa.Column('created_at', sa.DateTime(), default=datetime.utcnow),
    )

    # --- Step 3: Data migration from report_configs ---
    conn = op.get_bind()

    # Check if report_configs table exists
    inspector = sa.inspect(conn)
    if 'report_configs' in inspector.get_table_names():
        # Migrate Tenant -> Client relationships
        conn.execute(sa.text("""
            UPDATE tenants SET client_id_fk = (
                SELECT DISTINCT rc.client_id FROM report_configs rc
                WHERE rc.tenant_id = tenants.id
                LIMIT 1
            )
            WHERE EXISTS (
                SELECT 1 FROM report_configs rc WHERE rc.tenant_id = tenants.id
            )
        """))

        # Migrate Workspace -> Tenant relationships
        conn.execute(sa.text("""
            UPDATE workspaces SET tenant_id_fk = (
                SELECT DISTINCT rc.tenant_id FROM report_configs rc
                WHERE rc.workspace_id = workspaces.id
                LIMIT 1
            )
            WHERE EXISTS (
                SELECT 1 FROM report_configs rc WHERE rc.workspace_id = workspaces.id
            )
        """))

        # Migrate Report -> Workspace, UsuarioPBI, privacy
        conn.execute(sa.text("""
            UPDATE reports SET
                workspace_id_fk = (
                    SELECT rc.workspace_id FROM report_configs rc
                    WHERE rc.report_id_fk = reports.id
                    LIMIT 1
                ),
                usuario_pbi_id = (
                    SELECT rc.usuario_pbi_id FROM report_configs rc
                    WHERE rc.report_id_fk = reports.id
                    LIMIT 1
                ),
                es_publico = COALESCE((
                    SELECT rc.es_publico FROM report_configs rc
                    WHERE rc.report_id_fk = reports.id
                    LIMIT 1
                ), true),
                es_privado = COALESCE((
                    SELECT rc.es_privado FROM report_configs rc
                    WHERE rc.report_id_fk = reports.id
                    LIMIT 1
                ), false),
                created_at = (
                    SELECT rc.created_at FROM report_configs rc
                    WHERE rc.report_id_fk = reports.id
                    LIMIT 1
                )
            WHERE EXISTS (
                SELECT 1 FROM report_configs rc WHERE rc.report_id_fk = reports.id
            )
        """))

        # Migrate PublicLink: report_config_id -> report_id_fk
        conn.execute(sa.text("""
            UPDATE public_links SET report_id_fk = (
                SELECT rc.report_id_fk FROM report_configs rc
                WHERE rc.id = public_links.report_config_id
            )
            WHERE report_config_id IS NOT NULL
        """))

        # Migrate empresa_report_config -> empresa_report
        if 'empresa_report_config' in inspector.get_table_names():
            conn.execute(sa.text("""
                INSERT INTO empresa_report (empresa_id, report_id, created_at)
                SELECT erc.empresa_id, rc.report_id_fk, erc.created_at
                FROM empresa_report_config erc
                JOIN report_configs rc ON rc.id = erc.report_config_id
            """))

    # --- Step 4: For unassigned records, set defaults ---
    # If there are tenants without a client, assign the first client
    result = conn.execute(sa.text("SELECT id FROM clients LIMIT 1"))
    first_client = result.fetchone()
    if first_client:
        conn.execute(sa.text(
            "UPDATE tenants SET client_id_fk = :cid WHERE client_id_fk IS NULL"
        ), {"cid": first_client[0]})

    # If there are workspaces without a tenant, assign the first tenant
    result = conn.execute(sa.text("SELECT id FROM tenants LIMIT 1"))
    first_tenant = result.fetchone()
    if first_tenant:
        conn.execute(sa.text(
            "UPDATE workspaces SET tenant_id_fk = :tid WHERE tenant_id_fk IS NULL"
        ), {"tid": first_tenant[0]})

    # If there are reports without a workspace, assign the first workspace
    result = conn.execute(sa.text("SELECT id FROM workspaces LIMIT 1"))
    first_workspace = result.fetchone()
    if first_workspace:
        conn.execute(sa.text(
            "UPDATE reports SET workspace_id_fk = :wid WHERE workspace_id_fk IS NULL"
        ), {"wid": first_workspace[0]})

    # If there are reports without a usuario_pbi, assign the first one
    result = conn.execute(sa.text("SELECT id FROM usuarios_pbi LIMIT 1"))
    first_upbi = result.fetchone()
    if first_upbi:
        conn.execute(sa.text(
            "UPDATE reports SET usuario_pbi_id = :uid WHERE usuario_pbi_id IS NULL"
        ), {"uid": first_upbi[0]})

    # Set defaults for privacy fields
    conn.execute(sa.text(
        "UPDATE reports SET es_publico = true WHERE es_publico IS NULL"
    ))
    conn.execute(sa.text(
        "UPDATE reports SET es_privado = false WHERE es_privado IS NULL"
    ))

    # --- Step 5: Make columns NOT NULL and add FKs ---
    # Note: For SQLite, we can't alter columns to add NOT NULL or FKs
    # We handle this by creating the FKs only for non-SQLite databases
    bind = op.get_bind()
    is_sqlite = 'sqlite' in str(bind.engine.url)

    if not is_sqlite:
        op.alter_column('tenants', 'client_id_fk', nullable=False)
        op.create_foreign_key('fk_tenants_client', 'tenants', 'clients', ['client_id_fk'], ['id'])

        op.alter_column('workspaces', 'tenant_id_fk', nullable=False)
        op.create_foreign_key('fk_workspaces_tenant', 'workspaces', 'tenants', ['tenant_id_fk'], ['id'])

        op.alter_column('reports', 'workspace_id_fk', nullable=False)
        op.alter_column('reports', 'usuario_pbi_id', nullable=False)
        op.alter_column('reports', 'es_publico', nullable=False)
        op.alter_column('reports', 'es_privado', nullable=False)
        op.create_foreign_key('fk_reports_workspace', 'reports', 'workspaces', ['workspace_id_fk'], ['id'])
        op.create_foreign_key('fk_reports_usuario_pbi', 'reports', 'usuarios_pbi', ['usuario_pbi_id'], ['id'])

        op.alter_column('public_links', 'report_id_fk', nullable=False)
        op.create_foreign_key('fk_public_links_report', 'public_links', 'reports', ['report_id_fk'], ['id'])

    # --- Step 6: Drop old tables and columns ---
    # Drop old report_config_id from public_links
    if not is_sqlite:
        # Drop FK constraint first
        try:
            op.drop_constraint('public_links_report_config_id_fkey', 'public_links', type_='foreignkey')
        except Exception:
            pass
        op.drop_column('public_links', 'report_config_id')

    # Drop empresa_report_config table
    if 'empresa_report_config' in inspector.get_table_names():
        op.drop_table('empresa_report_config')

    # Drop report_configs table
    if 'report_configs' in inspector.get_table_names():
        op.drop_table('report_configs')


def downgrade():
    # --- Reverse: recreate report_configs and restore old structure ---
    conn = op.get_bind()
    is_sqlite = 'sqlite' in str(conn.engine.url)

    # Recreate report_configs table
    op.create_table(
        'report_configs',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('tenant_id', sa.BigInteger(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('client_id', sa.BigInteger(), sa.ForeignKey('clients.id'), nullable=False),
        sa.Column('workspace_id', sa.BigInteger(), sa.ForeignKey('workspaces.id'), nullable=False),
        sa.Column('report_id_fk', sa.BigInteger(), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('usuario_pbi_id', sa.BigInteger(), sa.ForeignKey('usuarios_pbi.id'), nullable=False),
        sa.Column('es_publico', sa.Boolean(), default=True, nullable=False),
        sa.Column('es_privado', sa.Boolean(), default=False, nullable=False),
        sa.Column('tipo_privacidad', sa.String(20), nullable=True),
        sa.Column('cliente_privado_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime()),
    )

    # Recreate empresa_report_config M2M
    op.create_table(
        'empresa_report_config',
        sa.Column('empresa_id', sa.BigInteger(), sa.ForeignKey('clientes_privados.id'), primary_key=True),
        sa.Column('report_config_id', sa.BigInteger(), sa.ForeignKey('report_configs.id'), primary_key=True),
        sa.Column('created_at', sa.DateTime()),
    )

    # Add back report_config_id to public_links
    if not is_sqlite:
        op.add_column('public_links', sa.Column('report_config_id', sa.BigInteger(), nullable=True))

    # Drop new tables and columns
    op.drop_table('empresa_report')

    if not is_sqlite:
        try:
            op.drop_constraint('fk_public_links_report', 'public_links', type_='foreignkey')
        except Exception:
            pass
        op.drop_column('public_links', 'report_id_fk')

        try:
            op.drop_constraint('fk_reports_workspace', 'reports', type_='foreignkey')
        except Exception:
            pass
        try:
            op.drop_constraint('fk_reports_usuario_pbi', 'reports', type_='foreignkey')
        except Exception:
            pass

    op.drop_column('reports', 'workspace_id_fk')
    op.drop_column('reports', 'usuario_pbi_id')
    op.drop_column('reports', 'es_publico')
    op.drop_column('reports', 'es_privado')
    op.drop_column('reports', 'created_at')

    if not is_sqlite:
        try:
            op.drop_constraint('fk_workspaces_tenant', 'workspaces', type_='foreignkey')
        except Exception:
            pass
    op.drop_column('workspaces', 'tenant_id_fk')

    if not is_sqlite:
        try:
            op.drop_constraint('fk_tenants_client', 'tenants', type_='foreignkey')
        except Exception:
            pass
    op.drop_column('tenants', 'client_id_fk')
