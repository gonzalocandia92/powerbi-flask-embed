"""
Access rules for public reports that require an application login.

A report with requires_login can be opened through its public links only by an
authenticated, active user who either has backoffice access or has the
reports.read permission and belongs to an active empresa associated with the report.
Reports without requires_login stay open to everyone.
"""
from flask import jsonify, redirect, request, url_for

from app import db
from app.models import PublicLink, Report

READ_PERMISSION = 'reports.read'
BACKOFFICE_PERMISSION = 'backoffice.access'


def is_logged_in(user):
    """True for an authenticated user whose account is still active."""
    return bool(user is not None and user.is_authenticated and user.is_active)


def has_backoffice_access(user):
    return is_logged_in(user) and user.has_permission(BACKOFFICE_PERMISSION)


def user_can_view_report(user, report):
    """Whether `user` (possibly anonymous) may open `report` through a public link."""
    if not report.requires_login:
        return True
    if not is_logged_in(user):
        return False
    if user.has_permission(BACKOFFICE_PERMISSION):
        return True
    if not user.has_permission(READ_PERMISSION):
        return False
    member_of = {membership.empresa_id for membership in user.empresa_memberships}
    return any(empresa.estado_activo and empresa.id in member_of for empresa in report.empresas)


def login_redirect():
    """Send an anonymous visitor to the login page and bring them back afterwards."""
    target = request.full_path if request.query_string else request.path
    return redirect(url_for('auth.login', next=target))


def access_denied_json(user):
    """JSON error for API-style endpoints: 401 when logged out, 403 when not allowed."""
    if not is_logged_in(user):
        return jsonify({
            'error': 'Debe iniciar sesión para acceder a este reporte',
            'login_required': True,
        }), 401
    return jsonify({'error': 'No tiene permiso para acceder a este reporte'}), 403


def active_link_report(slug):
    """Report behind an active public link slug, or None."""
    if not slug:
        return None
    link = PublicLink.query.filter_by(custom_slug=slug, is_active=True).first()
    return link.report if link else None


def login_required_slugs():
    """Slugs (active or not) of every public link whose report requires a login."""
    rows = (
        db.session.query(PublicLink.custom_slug)
        .join(Report, Report.id == PublicLink.report_id_fk)
        .filter(Report.requires_login.is_(True))
        .all()
    )
    return {slug for (slug,) in rows if slug}
