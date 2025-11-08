"""
Database models for Power BI Flask Embed application.
"""
from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet, InvalidToken
import os

from app import db

FERNET_KEY = os.getenv('FERNET_KEY')
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY not defined in .env. Generate with cryptography.Fernet.generate_key()")

fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)


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


class Tenant(db.Model):
    """Azure AD tenant configuration."""
    
    __tablename__ = 'tenants'
    
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    tenant_id = db.Column(db.String(120), nullable=False)


class Client(db.Model):
    """Azure AD application client configuration."""
    
    __tablename__ = 'clients'
    
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    client_id = db.Column(db.String(200), nullable=False)
    _client_secret = db.Column("client_secret", db.LargeBinary, nullable=True)
    
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


class Workspace(db.Model):
    """Power BI workspace configuration."""
    
    __tablename__ = 'workspaces'
    
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    workspace_id = db.Column(db.String(200), nullable=False)


class Report(db.Model):
    """Power BI report configuration."""
    
    __tablename__ = 'reports'
    
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    report_id = db.Column(db.String(200), nullable=False)
    embed_url = db.Column(db.String(1000), nullable=True)


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


class ReportConfig(db.Model):
    """Complete configuration for embedding a Power BI report."""
    
    __tablename__ = 'report_configs'
    
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    
    tenant_id = db.Column(db.BigInteger, db.ForeignKey('tenants.id'), nullable=False)
    client_id = db.Column(db.BigInteger, db.ForeignKey('clients.id'), nullable=False)
    workspace_id = db.Column(db.BigInteger, db.ForeignKey('workspaces.id'), nullable=False)
    report_id_fk = db.Column(db.BigInteger, db.ForeignKey('reports.id'), nullable=False)
    usuario_pbi_id = db.Column(db.BigInteger, db.ForeignKey('usuarios_pbi.id'), nullable=False)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    tenant = db.relationship('Tenant')
    client = db.relationship('Client')
    workspace = db.relationship('Workspace')
    report = db.relationship('Report')
    usuario_pbi = db.relationship('UsuarioPBI')


class PublicLink(db.Model):
    """Public link for accessing reports without authentication."""
    
    __tablename__ = 'public_links'
    
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    token = db.Column(db.String(120), unique=True, nullable=False)
    custom_slug = db.Column(db.String(120), unique=True, nullable=True)
    report_config_id = db.Column(db.BigInteger, db.ForeignKey('report_configs.id'), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    report_config = db.relationship('ReportConfig')
