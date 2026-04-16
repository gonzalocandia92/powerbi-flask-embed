"""
Power BI Flask Embed Application

This Flask application provides an interface for embedding Power BI reports
using Azure AD authentication and the Power BI REST API.
"""
import os
import atexit
import logging
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'WARNING').upper(), logging.WARNING),
    format='%(asctime)s [%(levelname)s] %(message)s'
)

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()


def create_app():
    """
    Application factory function to create and configure the Flask app.
    
    Returns:
        Flask: Configured Flask application instance
    """
    app = Flask(__name__)
    
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('SQLALCHEMY_DATABASE_URI')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
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
    
    login_manager.login_view = 'auth.login'
    login_manager.init_app(app)
    
    @app.teardown_appcontext
    def shutdown_session(exception=None):
        """Close database session after each request."""
        db.session.remove()
    
    from app.routes import auth, main, tenants, clients, workspaces, reports, usuarios_pbi, public, analytics, private, empresas, futuras_empresas, api_docs, monitor
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
    
    return app
