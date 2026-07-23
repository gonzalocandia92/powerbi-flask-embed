"""
Database models for Power BI Flask Embed application.

Domain hierarchy:
  Client (1) → (N) Tenant (1) → (N) Workspace (1) → (N) Report (1) → (N) PublicLink
  Report (M) ↔ (N) Empresa
"""
from datetime import datetime, timezone
import sqlalchemy as sa
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet, InvalidToken
import os
from sqlalchemy.ext.compiler import compiles

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - fallback for environments without pgvector installed
    class Vector(sa.types.UserDefinedType):
        cache_ok = True

        def __init__(self, dimensions):
            self.dimensions = dimensions

        def get_col_spec(self, **kw):
            return f"VECTOR({self.dimensions})"

from app import db

FERNET_KEY = os.getenv('FERNET_KEY')
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY not defined in .env. Generate with cryptography.Fernet.generate_key()")

fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)


def _utcnow():
    """Return current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


# Association table for many-to-many relationship between User and Role
user_role = db.Table(
    'user_role',
    db.Column('user_id', db.BigInteger, db.ForeignKey('users.id'), primary_key=True),
    db.Column('role_id', db.BigInteger, db.ForeignKey('roles.id'), primary_key=True),
)

# Association table for many-to-many relationship between Role and Permission
role_permission = db.Table(
    'role_permission',
    db.Column('role_id', db.BigInteger, db.ForeignKey('roles.id'), primary_key=True),
    db.Column('permission_id', db.BigInteger, db.ForeignKey('permissions.id'), primary_key=True),
)


class Permission(db.Model):
    """Permission model for role-based access control."""

    __tablename__ = 'permissions'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    description = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    # Relationships
    roles = db.relationship('Role', secondary='role_permission', back_populates='permissions')

    def __repr__(self):
        return f'<Permission {self.name}>'


class Role(db.Model):
    """Role model for grouping permissions."""

    __tablename__ = 'roles'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    description = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    # Relationships
    users = db.relationship('User', secondary='user_role', back_populates='roles')
    permissions = db.relationship('Permission', secondary='role_permission', back_populates='roles')

    def __repr__(self):
        return f'<Role {self.name}>'

    def has_permission(self, permission_name):
        """Check if role has a specific permission."""
        return any(p.name == permission_name for p in self.permissions)


class User(db.Model, UserMixin):
    """Application user model for authentication."""

    __tablename__ = 'users'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    email = db.Column(db.String(254), unique=True, nullable=True, index=True)
    password_hash = db.Column(db.String(256), nullable=True)
    is_admin = db.Column(db.Boolean, default=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    # Relationships
    roles = db.relationship('Role', secondary='user_role', back_populates='users')

    def set_password(self, password):
        """Hash and store the password."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Verify the password against the stored hash."""
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def has_permission(self, permission_name):
        """Check if user has a specific permission through their roles."""
        if self.is_admin:
            return True
        return any(role.has_permission(permission_name) for role in self.roles)

    def has_role(self, role_name):
        """Check if user has a specific role."""
        if self.is_admin:
            return True
        return any(role.name == role_name for role in self.roles)

    def __repr__(self):
        return f'<User {self.username}>'


class Client(db.Model):
    """Azure AD application client configuration."""

    __tablename__ = 'clients'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    client_id = db.Column(db.String(200), nullable=False)
    _client_secret = db.Column("client_secret", db.LargeBinary, nullable=True)

    # Relationships
    tenants = db.relationship('Tenant', back_populates='client', lazy='dynamic')

    def set_secret(self, plain):
        """Encrypt and store the client secret."""
        self._client_secret = fernet.encrypt(plain.encode())

    def get_secret(self):
        """Decrypt and return the client secret."""
        if not self._client_secret:
            return None
        try:
            return fernet.decrypt(self._client_secret).decode()
        except InvalidToken:
            return None


class Tenant(db.Model):
    """Azure AD tenant configuration. Belongs to a Client."""

    __tablename__ = 'tenants'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    tenant_id = db.Column(db.String(120), nullable=False)
    client_id_fk = db.Column(db.BigInteger, db.ForeignKey('clients.id'), nullable=False)

    # Relationships
    client = db.relationship('Client', back_populates='tenants')
    workspaces = db.relationship('Workspace', back_populates='tenant', lazy='dynamic')


class Workspace(db.Model):
    """Power BI workspace configuration. Belongs to a Tenant."""

    __tablename__ = 'workspaces'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    workspace_id = db.Column(db.String(200), nullable=False)
    tenant_id_fk = db.Column(db.BigInteger, db.ForeignKey('tenants.id'), nullable=False)

    # Relationships
    tenant = db.relationship('Tenant', back_populates='workspaces')
    reports = db.relationship('Report', back_populates='workspace', lazy='dynamic')


class UsuarioPBI(db.Model):
    """Power BI user credentials for authentication."""

    __tablename__ = 'usuarios_pbi'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(200), nullable=False, unique=True)
    username = db.Column(db.String(200), nullable=False)
    _password = db.Column("password", db.LargeBinary, nullable=False)

    def set_password(self, plain):
        """Encrypt and store the password."""
        self._password = fernet.encrypt(plain.encode())

    def get_password(self):
        """Decrypt and return the password."""
        try:
            return fernet.decrypt(self._password).decode()
        except InvalidToken:
            return None


# Association table for many-to-many relationship between Empresa and Report
empresa_report = db.Table(
    'empresa_report',
    db.Column('empresa_id', db.BigInteger, db.ForeignKey('clientes_privados.id'), primary_key=True),
    db.Column('report_id', db.BigInteger, db.ForeignKey('reports.id'), primary_key=True),
    db.Column('created_at', db.DateTime, default=_utcnow),
)


class Report(db.Model):
    """Power BI report. Belongs to a Workspace. Contains privacy settings."""

    __tablename__ = 'reports'

    id = db.Column(db.BigInteger().with_variant(db.Integer, 'sqlite'), primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    report_id = db.Column(db.String(200), nullable=False)
    embed_url = db.Column(db.String(1000), nullable=True)

    # Foreign keys
    workspace_id_fk = db.Column(db.BigInteger, db.ForeignKey('workspaces.id'), nullable=False)
    usuario_pbi_id = db.Column(db.BigInteger, db.ForeignKey('usuarios_pbi.id'), nullable=False)
    empresa_facturadora_id = db.Column(db.BigInteger, db.ForeignKey('clientes_privados.id'), nullable=True)

    # Privacy fields (moved from former ReportConfig)
    es_publico = db.Column(db.Boolean, default=True, nullable=False)
    es_privado = db.Column(db.Boolean, default=False, nullable=False)

    # Chatbot visibility — disable to hide KLARA for this report's public links
    chatbot_enabled = db.Column(db.Boolean, default=False, nullable=False)

    # Whether to show DAX queries in the chat (off by default for clients)
    show_dax_query = db.Column(db.Boolean, default=False, nullable=False)

    # Fixed schema retrieval limits for KLARA. Null values use runtime defaults.
    schema_retrieval_prompt = db.Column(db.Text, nullable=True)
    schema_table_context_limit = db.Column(db.Integer, nullable=True)
    schema_measure_context_limit = db.Column(db.Integer, nullable=True)

    # Filter configuration for private API access
    filter_enabled = db.Column(db.Boolean, default=False, nullable=False)
    filter_table = db.Column(db.String(200), nullable=True)
    filter_column = db.Column(db.String(200), nullable=True)

    created_at = db.Column(db.DateTime, default=_utcnow)

    # Relationships
    workspace = db.relationship('Workspace', back_populates='reports')
    usuario_pbi = db.relationship('UsuarioPBI')
    public_links = db.relationship('PublicLink', back_populates='report', lazy='dynamic', cascade='all, delete-orphan')
    empresas = db.relationship('Empresa', secondary='empresa_report', back_populates='reports')
    empresa_facturadora = db.relationship('Empresa', foreign_keys=[empresa_facturadora_id], back_populates='reports_facturados')
    schema_embeddings = db.relationship(
        'SchemaEmbedding',
        back_populates='report',
        lazy='dynamic',
        cascade='all, delete-orphan',
    )


class Empresa(db.Model):
    """Company configuration for API access (formerly ClientePrivado)."""

    __tablename__ = 'clientes_privados'  # Keep table name for backward compatibility

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(200), nullable=False, unique=True)
    cuit = db.Column(db.String(20), nullable=True)
    client_id = db.Column(db.String(200), nullable=False, unique=True)
    client_secret_hash = db.Column(db.String(256), nullable=False)
    estado_activo = db.Column(db.Boolean, default=True, nullable=False)
    whatsapp_enabled = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    # Relationship to reports (many-to-many)
    reports = db.relationship('Report', secondary='empresa_report', back_populates='empresas')
    reports_facturados = db.relationship('Report', foreign_keys='Report.empresa_facturadora_id', back_populates='empresa_facturadora')
    whatsapp_authorized_numbers = db.relationship(
        'WhatsAppAuthorizedNumber', back_populates='empresa', cascade='all, delete-orphan'
    )


# Keep ClientePrivado as an alias for backward compatibility
ClientePrivado = Empresa


class PublicLink(db.Model):
    """Public link for accessing reports without authentication."""

    __tablename__ = 'public_links'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    token = db.Column(db.String(120), unique=True, nullable=False)
    custom_slug = db.Column(db.String(120), unique=True, nullable=True)
    report_id_fk = db.Column(db.BigInteger, db.ForeignKey('reports.id'), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    allow_refresh = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    report = db.relationship('Report', back_populates='public_links')


class Visit(db.Model):
    """Analytics tracking for public link visits."""

    __tablename__ = 'visits'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    link_slug = db.Column(db.String(120), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, default=_utcnow, nullable=False, index=True)
    visitor_id = db.Column(db.String(36), nullable=True, index=True)
    ip_hash = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(500), nullable=True)
    referrer = db.Column(db.String(1000), nullable=True)
    utm_source = db.Column(db.String(100), nullable=True)
    utm_medium = db.Column(db.String(100), nullable=True)
    utm_campaign = db.Column(db.String(100), nullable=True)
    device_type = db.Column(db.String(50), nullable=True)
    browser = db.Column(db.String(100), nullable=True)
    os = db.Column(db.String(100), nullable=True)
    country = db.Column(db.String(2), nullable=True)
    is_bot = db.Column(db.Boolean, default=False, nullable=False)
    session_duration = db.Column(db.Integer, nullable=True)


class DatasetRefreshLog(db.Model):
    """Tracks the refresh status of Power BI semantic models for each report."""

    __tablename__ = 'dataset_refresh_logs'

    id = db.Column(db.BigInteger().with_variant(db.Integer, 'sqlite'), primary_key=True, autoincrement=True)
    report_id_fk = db.Column(db.BigInteger, db.ForeignKey('reports.id', ondelete='CASCADE'), nullable=False)
    dataset_id = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(50), nullable=False, default='Unknown')
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    error_json = db.Column(db.Text, nullable=True)
    refresh_type = db.Column(db.String(50), nullable=True)
    polled_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    retry_attempted = db.Column(db.Boolean, default=False, nullable=False)
    retry_triggered_at = db.Column(db.DateTime, nullable=True)

    report = db.relationship('Report', backref=db.backref('refresh_logs', lazy='dynamic', cascade='all, delete-orphan'))

    __table_args__ = (
        db.Index('ix_refresh_log_report_polled', 'report_id_fk', 'polled_at'),
    )


class SchemaEmbedding(db.Model):
    """Persisted semantic-model embeddings for vector retrieval."""

    __tablename__ = 'schema_embeddings'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    report_id_fk = db.Column(
        db.BigInteger,
        db.ForeignKey('reports.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    dataset_id = db.Column(db.String(200), nullable=False, index=True)
    item_type = db.Column(db.String(50), nullable=False, index=True)
    item_name = db.Column(db.String(255), nullable=False)
    content_text = db.Column(db.Text, nullable=False)
    embedding = db.Column(Vector(1024), nullable=False)
    last_updated = db.Column(db.DateTime, default=_utcnow, nullable=False)

    report = db.relationship('Report', back_populates='schema_embeddings')


class AnalyticsSkill(db.Model):
    """Operational analytics skill routed by semantic embeddings."""

    __tablename__ = 'analytics_skills'

    id = db.Column(db.BigInteger().with_variant(db.Integer, 'sqlite'), primary_key=True, autoincrement=True)
    skill_key = db.Column(db.String(120), nullable=False, index=True)
    domain_key = db.Column(db.String(120), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    priority = db.Column(db.String(20), nullable=False, default='normal', index=True)
    enforcement_mode = db.Column(db.String(30), nullable=False, default='soft', index=True)
    confidence_label = db.Column(db.String(30), nullable=True, index=True)

    report_id_fk = db.Column(db.BigInteger, db.ForeignKey('reports.id', ondelete='CASCADE'), nullable=True, index=True)
    empresa_id_fk = db.Column(db.BigInteger, db.ForeignKey('clientes_privados.id', ondelete='CASCADE'), nullable=True, index=True)
    dataset_id = db.Column(db.String(200), nullable=True, index=True)

    routing_text = db.Column(db.Text, nullable=False)
    content = db.Column(db.Text, nullable=False)
    metadata_json = db.Column(db.JSON, nullable=True)
    routing_json = db.Column(db.JSON, nullable=True)
    validation_json = db.Column(db.JSON, nullable=True)

    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    version = db.Column(db.Integer, nullable=False, default=1)

    embedding = db.Column(Vector(1024), nullable=True)
    embedding_model = db.Column(db.String(120), nullable=True)
    embedded_at = db.Column(db.DateTime, nullable=True)
    routing_document_hash = db.Column(db.String(64), nullable=True)

    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    report = db.relationship('Report')
    empresa = db.relationship('Empresa')

    __table_args__ = (
        db.CheckConstraint(
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
            name='ck_analytics_skills_single_scope',
        ),
        db.Index('ix_analytics_skills_scope', 'report_id_fk', 'dataset_id', 'empresa_id_fk'),
    )

    @property
    def scope(self) -> str:
        if self.report_id_fk is not None:
            return 'report'
        if self.dataset_id:
            return 'dataset'
        if self.empresa_id_fk is not None:
            return 'empresa'
        return 'global'


@compiles(Vector, 'sqlite')
def _compile_vector_sqlite(type_, compiler, **kw):
    """Allow metadata creation in SQLite-based tests."""
    return 'BLOB'


class ChatSession(db.Model):
    """Chat conversation session — cabecera del log de KLARA."""

    __tablename__ = 'chat_sessions'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    slug = db.Column(db.String(120), nullable=True, index=True)
    title = db.Column(db.String(200), nullable=True)
    workspace_id_fk = db.Column(db.BigInteger, db.ForeignKey('workspaces.id'), nullable=True, index=True)
    report_id_fk = db.Column(db.BigInteger, db.ForeignKey('reports.id'), nullable=True, index=True)
    empresa_id = db.Column(db.BigInteger, db.ForeignKey('clientes_privados.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    last_message_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    total_messages = db.Column(db.Integer, default=0, nullable=False)
    had_errors = db.Column(db.Boolean, default=False, nullable=False)

    workspace = db.relationship('Workspace')
    report = db.relationship('Report')
    empresa = db.relationship('Empresa')
    messages = db.relationship(
        'ChatMessage', back_populates='session', lazy='dynamic',
        cascade='all, delete-orphan'
    )
    usage_events = db.relationship('AIUsageEvent', back_populates='session', lazy='dynamic')


class ChatMessage(db.Model):
    """Individual message in a KLARA chat session — detalle del log."""

    __tablename__ = 'chat_messages'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    session_id = db.Column(
        db.BigInteger, db.ForeignKey('chat_sessions.id', ondelete='CASCADE'),
        nullable=False, index=True
    )
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    latency_ms = db.Column(db.Integer, nullable=True)
    model_used = db.Column(db.String(100), nullable=True)
    input_tokens = db.Column(db.Integer, nullable=True)
    output_tokens = db.Column(db.Integer, nullable=True)
    total_cost_usd = db.Column(db.Float, nullable=True, default=0)
    total_input_tokens = db.Column(db.Integer, nullable=True, default=0)
    total_output_tokens = db.Column(db.Integer, nullable=True, default=0)
    mcp_used = db.Column(db.Boolean, nullable=True)
    tools_called = db.Column(db.JSON, nullable=True)
    dax_query = db.Column(db.Text, nullable=True)
    had_error = db.Column(db.Boolean, default=False, nullable=False)
    error_message = db.Column(db.Text, nullable=True)

    session = db.relationship('ChatSession', back_populates='messages')
    usage_events = db.relationship('AIUsageEvent', back_populates='message', lazy='dynamic')

    __table_args__ = (
        db.Index('ix_chat_message_session_created', 'session_id', 'created_at'),
    )


class FuturaEmpresa(db.Model):
    """Pending company approval from external system."""

    __tablename__ = 'futuras_empresas'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    external_id = db.Column(db.String(200), nullable=False, unique=True)
    nombre = db.Column(db.String(200), nullable=False)
    cuit = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(200), nullable=True)
    telefono = db.Column(db.String(50), nullable=True)
    direccion = db.Column(db.String(500), nullable=True)
    datos_adicionales = db.Column(db.Text, nullable=True)
    estado = db.Column(db.String(20), default='pendiente', nullable=False)
    fecha_recepcion = db.Column(db.DateTime, default=_utcnow, nullable=False)
    fecha_procesamiento = db.Column(db.DateTime, nullable=True)
    procesado_por_user_id = db.Column(db.BigInteger, db.ForeignKey('users.id'), nullable=True)
    empresa_id = db.Column(db.BigInteger, db.ForeignKey('clientes_privados.id'), nullable=True)
    notas = db.Column(db.Text, nullable=True)

    procesado_por = db.relationship('User', foreign_keys=[procesado_por_user_id])
    empresa = db.relationship('Empresa', foreign_keys=[empresa_id])


class BillingLimit(db.Model):
    """Persisted billing limits for empresa/global scopes."""

    __tablename__ = 'billing_limits'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    scope_type = db.Column(db.String(20), nullable=False, index=True)
    scope_id = db.Column(db.String(120), nullable=True, index=True)
    period_type = db.Column(db.String(30), nullable=False, default='monthly_anniversary')
    limit_usd = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), nullable=False, default='USD')
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    cycle_anchor_day = db.Column(db.Integer, nullable=True, default=1)
    starts_at = db.Column(db.DateTime, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class AgentPromptConfig(db.Model):
    """Persisted additional agent instructions for global/empresa/report scopes."""

    __tablename__ = 'agent_prompt_configs'

    id = db.Column(db.BigInteger().with_variant(db.Integer, 'sqlite'), primary_key=True, autoincrement=True)
    scope_type = db.Column(db.String(20), nullable=False, index=True)
    scope_id = db.Column(db.String(120), nullable=True, index=True)
    title = db.Column(db.String(200), nullable=False)
    instructions = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    starts_at = db.Column(db.DateTime, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    __table_args__ = (
        db.Index('ix_agent_prompt_configs_scope', 'scope_type', 'scope_id'),
    )


class AIModelPricing(db.Model):
    """Persisted pricing by provider/model/event type."""

    __tablename__ = 'ai_model_pricing'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    provider = db.Column(db.String(50), nullable=False, index=True)
    model = db.Column(db.String(120), nullable=False, index=True)
    event_type = db.Column(db.String(30), nullable=False, index=True)
    currency = db.Column(db.String(10), nullable=False, default='USD')
    input_cost_per_million_usd = db.Column(db.Float, nullable=True)
    output_cost_per_million_usd = db.Column(db.Float, nullable=True)
    cache_write_cost_per_million_usd = db.Column(db.Float, nullable=True)
    cache_read_cost_per_million_usd = db.Column(db.Float, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    effective_from = db.Column(db.DateTime, default=_utcnow, nullable=False, index=True)
    effective_to = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    usage_events = db.relationship('AIUsageEvent', back_populates='pricing', lazy='dynamic')


class AIUsageEvent(db.Model):
    """Granular persisted AI usage/cost event for billing enforcement."""

    __tablename__ = 'ai_usage_events'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False, index=True)

    session_id = db.Column(db.BigInteger, db.ForeignKey('chat_sessions.id', ondelete='SET NULL'), nullable=True, index=True)
    message_id = db.Column(db.BigInteger, db.ForeignKey('chat_messages.id', ondelete='SET NULL'), nullable=True, index=True)
    workspace_id_fk = db.Column(db.BigInteger, db.ForeignKey('workspaces.id', ondelete='SET NULL'), nullable=True, index=True)
    report_id_fk = db.Column(db.BigInteger, db.ForeignKey('reports.id', ondelete='SET NULL'), nullable=True, index=True)
    empresa_id = db.Column(db.BigInteger, db.ForeignKey('clientes_privados.id', ondelete='SET NULL'), nullable=True, index=True)

    billing_scope_type = db.Column(db.String(20), nullable=False, index=True)
    billing_scope_id = db.Column(db.String(120), nullable=True, index=True)

    source_type = db.Column(db.String(30), nullable=False, index=True)
    trigger_type = db.Column(db.String(30), nullable=False)
    provider = db.Column(db.String(50), nullable=False, index=True)
    model = db.Column(db.String(120), nullable=False, index=True)
    event_type = db.Column(db.String(30), nullable=False, index=True)
    operation_name = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='success')

    input_tokens = db.Column(db.Integer, nullable=True)
    output_tokens = db.Column(db.Integer, nullable=True)
    total_tokens = db.Column(db.Integer, nullable=True)
    cached_input_tokens = db.Column(db.Integer, nullable=True)
    cache_write_tokens = db.Column(db.Integer, nullable=True)
    cache_read_tokens = db.Column(db.Integer, nullable=True)

    input_cost_usd = db.Column(db.Float, nullable=True)
    output_cost_usd = db.Column(db.Float, nullable=True)
    cache_write_cost_usd = db.Column(db.Float, nullable=True)
    cache_read_cost_usd = db.Column(db.Float, nullable=True)
    total_cost_usd = db.Column(db.Float, nullable=True)
    currency = db.Column(db.String(10), nullable=False, default='USD')

    pricing_id = db.Column(db.BigInteger, db.ForeignKey('ai_model_pricing.id', ondelete='SET NULL'), nullable=True, index=True)
    trace_id = db.Column(db.String(120), nullable=True)
    observation_id = db.Column(db.String(120), nullable=True)
    metadata_json = db.Column(db.JSON, nullable=True)

    session = db.relationship('ChatSession', back_populates='usage_events')
    message = db.relationship('ChatMessage', back_populates='usage_events')
    workspace = db.relationship('Workspace')
    report = db.relationship('Report')
    empresa = db.relationship('Empresa')
    pricing = db.relationship('AIModelPricing', back_populates='usage_events')

    __table_args__ = (
        db.Index('ix_ai_usage_events_empresa_created', 'empresa_id', 'created_at'),
        db.Index('ix_ai_usage_events_report_created', 'report_id_fk', 'created_at'),
        db.Index('ix_ai_usage_events_workspace_created', 'workspace_id_fk', 'created_at'),
        db.Index('ix_ai_usage_events_provider_model', 'provider', 'model'),
    )


class WhatsAppAuthorizedNumber(db.Model):
    """Admin-granted access: a phone number authorized to query a given report."""

    __tablename__ = 'whatsapp_authorized_numbers'

    id = db.Column(db.Integer, primary_key=True)
    phone_number = db.Column(db.String(30), nullable=False, index=True)
    empresa_id_fk = db.Column(db.BigInteger, db.ForeignKey('clientes_privados.id', ondelete='CASCADE'), nullable=False)
    report_id_fk = db.Column(db.Integer, db.ForeignKey('reports.id', ondelete='CASCADE'), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    empresa = db.relationship('Empresa', back_populates='whatsapp_authorized_numbers')
    report = db.relationship('Report')

    __table_args__ = (
        db.UniqueConstraint('phone_number', 'report_id_fk', name='uq_whatsapp_authorized_number_report'),
    )


class WhatsAppContact(db.Model):
    """Live phone↔report binding. report_id_fk is the currently active report;
    it is null while the user is choosing from a multi-report menu."""

    __tablename__ = 'whatsapp_contacts'

    id = db.Column(db.Integer, primary_key=True)
    phone_number = db.Column(db.String(30), unique=True, nullable=False, index=True)
    report_id_fk = db.Column(db.Integer, db.ForeignKey('reports.id', ondelete='CASCADE'), nullable=True)
    awaiting_report_selection = db.Column(db.Boolean, nullable=False, default=False)
    conversation_id = db.Column(db.Integer, db.ForeignKey('chat_sessions.id', ondelete='SET NULL'), nullable=True)
    is_processing = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    last_message_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    report = db.relationship('Report')
    session = db.relationship('ChatSession')
