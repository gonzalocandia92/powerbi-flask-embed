"""
Power BI Flask Embed Application

This Flask application provides an interface for embedding Power BI reports
using Azure AD authentication and the Power BI REST API.
"""
import os
import atexit
import logging
import asyncio
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, abort, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
from app.services.observability import init_langfuse

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'WARNING').upper(), logging.WARNING),
    format='%(asctime)s [%(levelname)s] %(message)s'
)

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=[])


def _is_production():
    return os.getenv('FLASK_ENV', 'development').strip().lower() in {'production', 'prod'}


def _validate_public_url(name, value, *, production):
    parsed = urlparse(value or '')
    if not parsed.scheme or not parsed.netloc or parsed.fragment:
        raise RuntimeError(f'{name} must be an absolute URL without a fragment')
    if production and parsed.scheme != 'https':
        raise RuntimeError(f'{name} must use HTTPS in production')


def _configured_oauth_private_key(app):
    configured = app.config.get('MCP_OAUTH_PRIVATE_KEY')
    if configured:
        return serialization.load_pem_private_key(
            configured.replace('\\n', '\n').encode(), password=None
        )

    key_path = app.config.get('MCP_OAUTH_PRIVATE_KEY_FILE')
    if key_path:
        path = Path(key_path)
        if not path.is_file():
            if _is_production():
                raise RuntimeError(f'MCP_OAUTH_PRIVATE_KEY_FILE does not exist: {path}')
            logging.warning(
                'Ignoring missing development MCP_OAUTH_PRIVATE_KEY_FILE: %s', path
            )
            return None
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    return None


def _validate_mcp_security_config(app):
    production = _is_production()
    _validate_public_url(
        'MCP_OAUTH_ISSUER', app.config['MCP_OAUTH_ISSUER'], production=production
    )
    _validate_public_url(
        'MCP_RESOURCE_URL', app.config['MCP_RESOURCE_URL'], production=production
    )

    key = _configured_oauth_private_key(app)
    if key is not None:
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise RuntimeError('MCP OAuth signing key must be an RSA private key of at least 2048 bits')
        app.extensions['mcp_oauth_private_key'] = key

    if not production:
        return

    secret_key = app.config.get('SECRET_KEY') or ''
    if secret_key == 'dev-secret' or len(secret_key.encode()) < 32:
        raise RuntimeError('SECRET_KEY must contain at least 32 bytes in production')

    internal_secret = app.config.get('MCP_INTERNAL_JWT_SECRET') or ''
    if len(internal_secret.encode()) < 32:
        raise RuntimeError('MCP_INTERNAL_JWT_SECRET must contain at least 32 bytes in production')
    if key is None:
        raise RuntimeError(
            'MCP_OAUTH_PRIVATE_KEY or MCP_OAUTH_PRIVATE_KEY_FILE is required in production'
        )

    access_ttl = app.config['MCP_OAUTH_ACCESS_TOKEN_TTL']
    refresh_ttl = app.config['MCP_OAUTH_REFRESH_TOKEN_TTL']
    if not 60 <= access_ttl <= 3600:
        raise RuntimeError('MCP_OAUTH_ACCESS_TOKEN_TTL must be between 60 and 3600 seconds')
    if not access_ttl < refresh_ttl <= 31536000:
        raise RuntimeError(
            'MCP_OAUTH_REFRESH_TOKEN_TTL must exceed the access TTL and be at most one year'
        )


def create_app():
    """
    Application factory function to create and configure the Flask app.
    
    Returns:
        Flask: Configured Flask application instance
    """
    app = Flask(__name__)
    trusted_proxy_hops = int(os.getenv('TRUSTED_PROXY_HOPS', '0'))
    if trusted_proxy_hops:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=trusted_proxy_hops,
            x_proto=trusted_proxy_hops,
            x_host=trusted_proxy_hops,
        )
    
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('SQLALCHEMY_DATABASE_URI')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['MCP_OAUTH_ISSUER'] = os.getenv('MCP_OAUTH_ISSUER', 'http://localhost:5000')
    app.config['MCP_RESOURCE_URL'] = os.getenv(
        'MCP_RESOURCE_URL', 'http://localhost:8000/mcp'
    ).rstrip('/')
    app.config['MCP_OAUTH_DCR_ENABLED'] = os.getenv(
        'MCP_OAUTH_DCR_ENABLED', 'false'
    ).strip().lower() == 'true'
    app.config['MCP_OAUTH_PRIVATE_KEY'] = os.getenv('MCP_OAUTH_PRIVATE_KEY')
    app.config['MCP_OAUTH_PRIVATE_KEY_FILE'] = os.getenv('MCP_OAUTH_PRIVATE_KEY_FILE')
    app.config['MCP_OAUTH_ACCESS_TOKEN_TTL'] = int(os.getenv('MCP_OAUTH_ACCESS_TOKEN_TTL', '900'))
    app.config['MCP_OAUTH_REFRESH_TOKEN_TTL'] = int(os.getenv('MCP_OAUTH_REFRESH_TOKEN_TTL', '2592000'))
    app.config['MCP_INTERNAL_JWT_SECRET'] = os.getenv('MCP_INTERNAL_JWT_SECRET')
    app.config['MCP_INTERNAL_REQUIRE_MTLS'] = os.getenv('MCP_INTERNAL_REQUIRE_MTLS', 'true').lower() == 'true'
    app.config['RATELIMIT_STORAGE_URI'] = os.getenv('RATELIMIT_STORAGE_URI', 'memory://')
    _validate_mcp_security_config(app)
    init_langfuse()
    
    db_uri = app.config['SQLALCHEMY_DATABASE_URI']
    if db_uri and 'sqlite' not in db_uri.lower():
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            'pool_size': 10,
            'pool_recycle': 3600,
            'pool_pre_ping': True,
            'max_overflow': 20,
            'pool_timeout': 30,
            'connect_args': {
                'connect_timeout': 10
            }
        }
    else:
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            'pool_pre_ping': True
        }
    
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)

    # Flask async views require the 'async' extra. In environments where that
    # dependency is unavailable, provide a lightweight compatibility shim so
    # async routes can still run under WSGI.
    try:
        from asgiref.sync import async_to_sync as asgiref_async_to_sync
    except ImportError:
        def _async_to_sync(func):
            def wrapper(*args, **kwargs):
                return asyncio.run(func(*args, **kwargs))
            return wrapper

        app.async_to_sync = _async_to_sync
    else:
        app.async_to_sync = asgiref_async_to_sync
    
    login_manager.login_view = 'auth.login'
    login_manager.init_app(app)
    
    @app.teardown_appcontext
    def shutdown_session(exception=None):
        """Close database session after each request."""
        db.session.remove()
    
    from app.routes import ai_config, auth, main, tenants, clients, workspaces, reports, usuarios_pbi, public, analytics, private, empresas, futuras_empresas, api_docs, monitor, chatbot, whatsapp, users, mcp_config, mcp_oauth, mcp_internal, mcp_oauth_admin
    app.register_blueprint(auth.bp)
    app.register_blueprint(main.bp)
    app.register_blueprint(tenants.bp)
    app.register_blueprint(clients.bp)
    app.register_blueprint(workspaces.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(usuarios_pbi.bp)
    app.register_blueprint(public.bp)
    app.register_blueprint(analytics.bp)
    app.register_blueprint(private.bp)
    app.register_blueprint(empresas.bp)
    app.register_blueprint(futuras_empresas.bp)
    app.register_blueprint(api_docs.bp)
    app.register_blueprint(monitor.bp)
    app.register_blueprint(chatbot.bp)
    app.register_blueprint(ai_config.bp)
    app.register_blueprint(whatsapp.bp)
    app.register_blueprint(users.bp)
    app.register_blueprint(mcp_config.bp)
    app.register_blueprint(mcp_oauth.bp)
    app.register_blueprint(mcp_internal.bp)
    app.register_blueprint(mcp_oauth_admin.bp)

    # Non-browser APIs authenticate independently and must not be subjected to
    # cookie-session CSRF validation.
    csrf.exempt(private.bp)
    csrf.exempt(chatbot.bp)
    csrf.exempt(whatsapp.bp)

    from app.services.mcp_oauth import init_oauth_server
    init_oauth_server(app)

    backoffice_blueprints = {
        'main', 'tenants', 'clients', 'workspaces', 'reports', 'usuarios_pbi',
        'analytics', 'empresas', 'futuras_empresas', 'api_docs', 'monitor',
        'ai_config', 'users', 'mcp_config', 'mcp_oauth_admin',
    }

    @app.before_request
    def enforce_backoffice_boundary():
        """OAuth-only users may authenticate but never enter the backoffice."""
        from flask_login import current_user
        if (
            request.blueprint in backoffice_blueprints
            and current_user.is_authenticated
            and not current_user.has_permission('backoffice.access')
        ):
            abort(403)

    # ── Background scheduler for dataset refresh monitoring ──────────────────
    # Avoid double-start in Flask debug/reloader mode
    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from app.services.refresh_monitor import poll_all_reports

            interval_hours = int(os.getenv('REFRESH_POLL_INTERVAL_HOURS', 12))
            scheduler = BackgroundScheduler(daemon=True)
            scheduler.add_job(
                func=poll_all_reports,
                args=[app],
                trigger='interval',
                hours=interval_hours,
                id='refresh_monitor_poll',
                replace_existing=True,
            )
            scheduler.start()
            atexit.register(lambda: scheduler.shutdown(wait=False))
            logging.info(
                f"[RefreshMonitor] Scheduler started — poll interval: {interval_hours}h"
            )
        except Exception as _sched_err:
            logging.error(f"[RefreshMonitor] Failed to start scheduler: {_sched_err}")
    # ─────────────────────────────────────────────────────────────────────────

    from app.models import User
    
    @login_manager.user_loader
    def load_user(user_id):
        """Load user from database by ID."""
        from app.utils.decorators import retry_on_db_error
        
        @retry_on_db_error(max_retries=3, delay=1)
        def _load_user():
            return User.query.get(int(user_id))
        
        return _load_user()
    
    @app.cli.command("create-admin")
    def create_admin():
        """CLI command to create an admin user."""
        username = input("Username admin: ")
        password = input("Password: ")
        
        if User.query.filter_by(username=username).first():
            print("User already exists")
            return
        
        user = User(username=username, is_admin=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print("Admin user created successfully")

    @app.cli.command('create-mcp-oauth-client')
    def create_mcp_oauth_client():
        """Create or update the static Claude OAuth client."""
        import secrets
        from app.models import McpOAuthClient

        client_id = os.getenv('MCP_CLAUDE_CLIENT_ID', 'claude-mcp')
        raw_secret = os.getenv('MCP_CLAUDE_CLIENT_SECRET')
        client = McpOAuthClient.query.filter_by(client_id=client_id).first()
        if client is None:
            client = McpOAuthClient(
                client_id=client_id, name='Claude MCP', registration_method='static'
            )
            raw_secret = raw_secret or secrets.token_urlsafe(48)
            db.session.add(client)
        elif client.registration_method != 'static':
            raise RuntimeError('The requested client ID belongs to a dynamic client')
        client.redirect_uris = [
            'https://claude.ai/api/mcp/auth_callback',
            'https://claude.com/api/mcp/auth_callback',
        ]
        client.allowed_scopes = [
            'mcp:models:list', 'mcp:models:read', 'mcp:models:query', 'mcp:models:write'
        ]
        client.grant_types = ['authorization_code', 'refresh_token']
        client.response_types = ['code']
        client.token_endpoint_auth_method = 'client_secret_post'
        client.is_active = True
        if raw_secret:
            client.set_client_secret(raw_secret)
        db.session.commit()
        print(f'Client ID: {client_id}')
        print('Token endpoint authentication: client_secret_post (PKCE required)')
        if raw_secret:
            print(f'Client secret (shown once): {raw_secret}')
        else:
            print('Existing client secret retained')
    
    return app
