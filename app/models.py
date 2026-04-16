"""
Database models for Power BI Flask Embed application.

Domain hierarchy:
  Client (1) → (N) Tenant (1) → (N) Workspace (1) → (N) Report (1) → (N) PublicLink
  Report (M) ↔ (N) Empresa
"""
from datetime import datetime, timezone
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet, InvalidToken
import os

from app import db

FERNET_KEY = os.getenv('FERNET_KEY')
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY not defined in .env. Generate with cryptography.Fernet.generate_key()")

fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)


def _utcnow():
    """Return current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


class User(db.Model, UserMixin):
    """Application user model for authentication."""

    __tablename__ = 'users'

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=True)

    def set_password(self, password):
        """Hash and store the password."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Verify the password against the stored hash."""
        return check_password_hash(self.password_hash, password)


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

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    report_id = db.Column(db.String(200), nullable=False)
    embed_url = db.Column(db.String(1000), nullable=True)

    # Foreign keys
    workspace_id_fk = db.Column(db.BigInteger, db.ForeignKey('workspaces.id'), nullable=False)
    usuario_pbi_id = db.Column(db.BigInteger, db.ForeignKey('usuarios_pbi.id'), nullable=False)

    # Privacy fields (moved from former ReportConfig)
    es_publico = db.Column(db.Boolean, default=True, nullable=False)
    es_privado = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, default=_utcnow)

    # Relationships
    workspace = db.relationship('Workspace', back_populates='reports')
    usuario_pbi = db.relationship('UsuarioPBI')
    public_links = db.relationship('PublicLink', back_populates='report', lazy='dynamic', cascade='all, delete-orphan')
    empresas = db.relationship('Empresa', secondary='empresa_report', back_populates='reports')


class Empresa(db.Model):
    """Company configuration for API access (formerly ClientePrivado)."""

    __tablename__ = 'clientes_privados'  # Keep table name for backward compatibility

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(200), nullable=False, unique=True)
    cuit = db.Column(db.String(20), nullable=True)
    client_id = db.Column(db.String(200), nullable=False, unique=True)
    client_secret_hash = db.Column(db.String(256), nullable=False)
    estado_activo = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    # Relationship to reports (many-to-many)
    reports = db.relationship('Report', secondary='empresa_report', back_populates='empresas')


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

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
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
