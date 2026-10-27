"""
Utility decorators for the Power BI Flask Embed application.
"""
import time
import logging
from functools import wraps
from flask import jsonify, redirect, url_for, flash
from flask_login import current_user
from sqlalchemy.exc import OperationalError, DBAPIError

from app import db
from app.services.report_access import has_backoffice_access, is_logged_in, is_staff_actor


def retry_on_db_error(max_retries=3, delay=1):
    """
    Decorator to retry database operations on connection errors.
    
    This decorator handles transient database connection failures by automatically
    retrying the operation with exponential backoff.
    
    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        delay: Base delay in seconds between retries (default: 1)
    
    Returns:
        Decorated function that retries on database errors
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (OperationalError, DBAPIError) as e:
                    last_exception = e
                    logging.warning(
                        f"Database connection error (attempt {attempt + 1}/{max_retries}): {e}"
                    )
                    
                    db.session.rollback()
                    db.session.remove()
                    
                    if attempt < max_retries - 1:
                        time.sleep(delay * (attempt + 1))
                    else:
                        logging.error(
                            f"Database connection error after {max_retries} attempts: {e}"
                        )
                        raise last_exception
            
            return None
        
        return wrapper
    
    return decorator


def admin_required(f):
    """
    Decorator to require admin privileges.
    
    Redirects non-admin users to login page with a warning message.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Acceso denegado. Se requieren permisos de administrador.', 'danger')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function


ACCESS_MARK = "__access__"
ACCESS_STAFF = "staff"


def staff_required(f=None, *, json: bool = False):
    """Require an authenticated, active Sudata staff user with backoffice access.

    ``is_admin`` does not stand in for ``is_staff``. The predicate is the one in ``report_access`` (single
    source of truth); this decorator only decides how a refusal looks:

    * ``json=True`` (API endpoints, called with ``fetch``): ``401`` when logged out, ``403`` otherwise,
      both as ``{"error", "code"}``. Never a redirect, which a ``fetch`` would silently follow.
    * ``json=False`` (HTML pages): flash + redirect, like ``admin_required``.

    The wrapped view carries ``__access__ == "staff"`` so a test can prove no endpoint of a blueprint was
    left unguarded. Usable bare (``@staff_required``) or as ``@staff_required(json=True)``.
    """
    def decorator(view):
        @wraps(view)
        def decorated_function(*args, **kwargs):
            if is_staff_actor(current_user) and has_backoffice_access(current_user):
                return view(*args, **kwargs)
            if json:
                if not is_logged_in(current_user):
                    return jsonify({"error": "Debe iniciar sesión.", "code": "login_required"}), 401
                return jsonify({"error": "Se requiere un usuario del equipo interno.",
                                "code": "staff_required"}), 403
            if not is_logged_in(current_user):
                flash('Debe iniciar sesión.', 'danger')
            else:
                flash('Acceso denegado. Se requiere un usuario del equipo interno.', 'danger')
            return redirect(url_for('auth.login'))
        setattr(decorated_function, ACCESS_MARK, ACCESS_STAFF)
        return decorated_function

    return decorator(f) if f is not None else decorator


def backoffice_required(f):
    """Require the dedicated backoffice permission, independent of OAuth login."""
    return permission_required('backoffice.access')(f)


def permission_required(permission_name):
    """
    Decorator to require a specific permission.
    
    Args:
        permission_name: Name of the permission required
    
    Returns:
        Decorated function that checks for permission
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                flash('Debe iniciar sesión.', 'danger')
                return redirect(url_for('auth.login'))
            
            if not current_user.has_permission(permission_name):
                flash('No tiene permiso para realizar esta acción.', 'danger')
                return redirect(url_for('main.index'))
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator
