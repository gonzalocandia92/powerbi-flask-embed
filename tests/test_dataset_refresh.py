"""
Unit tests for dataset refresh functionality.
Covers public link refresh endpoint and admin refresh endpoint.
"""
import os
import unittest
import json
import uuid

os.environ.setdefault('FERNET_KEY', 'o9eBKpiFgJRzgZNyBbFaQ8YeHImGZ5QpFnLn4EP9nj0=')
os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('PRIVATE_JWT_SECRET', 'test-jwt-secret')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')

from unittest.mock import patch, MagicMock
import requests as requests_lib

from app import create_app, db
from app.models import Client, Tenant, Workspace, Report, PublicLink, UsuarioPBI, User

_next_id = 0


def _id():
    """Generate a unique ID for SQLite BigInteger columns."""
    global _next_id
    _next_id += 1
    return _next_id


def _create_test_hierarchy(session):
    """Create a full Client→Tenant→Workspace→UsuarioPBI hierarchy for testing."""
    client = Client(id=_id(), name='Test Client', client_id='test-client-id')
    client.set_secret('test-secret')
    session.add(client)
    session.flush()

    tenant = Tenant(id=_id(), name='Test Tenant', tenant_id='test-tenant-id', client_id_fk=client.id)
    session.add(tenant)
    session.flush()

    workspace = Workspace(
        id=_id(), name='Test Workspace', workspace_id='test-workspace-id', tenant_id_fk=tenant.id
    )
    session.add(workspace)
    session.flush()

    usuario = UsuarioPBI(id=_id(), nombre='Test User PBI', username='test@pbi.com')
    usuario.set_password('test-pass')
    session.add(usuario)
    session.flush()

    return client, tenant, workspace, usuario


def _create_report(session, workspace, usuario, **kwargs):
    """Create a test Report with optional keyword overrides."""
    defaults = dict(
        id=_id(),
        name='Test Report',
        report_id=str(uuid.uuid4()),
        workspace_id_fk=workspace.id,
        usuario_pbi_id=usuario.id,
        es_publico=True,
        es_privado=False,
    )
    defaults.update(kwargs)
    report = Report(**defaults)
    session.add(report)
    session.flush()
    return report


def _create_public_link(session, report, allow_refresh=False, is_active=True, slug=None):
    """Create a test PublicLink."""
    if slug is None:
        slug = f"test-slug-{_id()}"
    link = PublicLink(
        id=_id(),
        token=uuid.uuid4().hex[:16],
        custom_slug=slug,
        report_id_fk=report.id,
        is_active=is_active,
        allow_refresh=allow_refresh,
    )
    session.add(link)
    session.flush()
    return link


class _BaseTestCase(unittest.TestCase):
    """Base test case that sets up an in-memory SQLite database."""

    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.http = self.app.test_client()
        with self.app.app_context():
            db.drop_all()
            db.create_all()
            # Reset in-memory rate limiting dict before each test
            import app.routes.public as pub_routes
            pub_routes._refresh_timestamps.clear()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()


class TestPublicRefreshEndpoint(_BaseTestCase):
    """Tests for POST /p/<slug>/refresh endpoint."""

    def _setup_link(self, allow_refresh=True, is_active=True):
        """Helper to set up a full hierarchy and return the slug."""
        with self.app.app_context():
            _, _, workspace, usuario = _create_test_hierarchy(db.session)
            report = _create_report(db.session, workspace, usuario)
            link = _create_public_link(
                db.session, report, allow_refresh=allow_refresh,
                is_active=is_active, slug='test-slug'
            )
            db.session.commit()
            return link.custom_slug

    def test_public_refresh_allowed_success(self):
        """POST to /p/<slug>/refresh returns 202 when allow_refresh=True and mock succeeds."""
        slug = self._setup_link(allow_refresh=True)

        with patch('app.routes.public.refresh_dataset') as mock_refresh:
            mock_refresh.return_value = {"dataset_id": "test-ds", "status": "accepted"}

            resp = self.http.post(f'/p/{slug}/refresh')

        self.assertEqual(resp.status_code, 202)
        data = json.loads(resp.data)
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['dataset_id'], 'test-ds')
        self.assertIn('message', data)

    def test_public_refresh_not_allowed(self):
        """POST to /p/<slug>/refresh returns 403 when allow_refresh=False."""
        slug = self._setup_link(allow_refresh=False)

        resp = self.http.post(f'/p/{slug}/refresh')

        self.assertEqual(resp.status_code, 403)
        data = json.loads(resp.data)
        self.assertIn('error', data)

    def test_public_refresh_inactive_link(self):
        """POST to /p/<slug>/refresh returns 404 when is_active=False."""
        slug = self._setup_link(allow_refresh=True, is_active=False)

        resp = self.http.post(f'/p/{slug}/refresh')

        self.assertEqual(resp.status_code, 404)

    def test_public_refresh_nonexistent_slug(self):
        """POST to /p/nonexistent/refresh returns 404."""
        resp = self.http.post('/p/nonexistent-slug-xyz/refresh')
        self.assertEqual(resp.status_code, 404)

    def test_public_refresh_rate_limit(self):
        """Second POST within 30 minutes returns 429 with retry_after."""
        slug = self._setup_link(allow_refresh=True)

        with patch('app.routes.public.refresh_dataset') as mock_refresh:
            mock_refresh.return_value = {"dataset_id": "test-ds", "status": "accepted"}

            # First request should succeed
            resp1 = self.http.post(f'/p/{slug}/refresh')
            self.assertEqual(resp1.status_code, 202)

            # Second request immediately should be rate-limited
            resp2 = self.http.post(f'/p/{slug}/refresh')

        self.assertEqual(resp2.status_code, 429)
        data = json.loads(resp2.data)
        self.assertIn('error', data)
        self.assertIn('retry_after', data)
        self.assertGreater(data['retry_after'], 0)

    def test_public_refresh_powerbi_api_error(self):
        """Mock refresh_dataset raising HTTPError (non-429) returns 500."""
        slug = self._setup_link(allow_refresh=True)

        http_error = requests_lib.HTTPError(response=MagicMock(status_code=500))

        with patch('app.routes.public.refresh_dataset', side_effect=http_error):
            resp = self.http.post(f'/p/{slug}/refresh')

        self.assertEqual(resp.status_code, 500)
        data = json.loads(resp.data)
        self.assertIn('error', data)

    def test_public_refresh_quota_exceeded(self):
        """Mock refresh_dataset raising HTTPError with 429 returns 429 with quota message."""
        slug = self._setup_link(allow_refresh=True)

        http_error = requests_lib.HTTPError(response=MagicMock(status_code=429))

        with patch('app.routes.public.refresh_dataset', side_effect=http_error):
            resp = self.http.post(f'/p/{slug}/refresh')

        self.assertEqual(resp.status_code, 429)
        data = json.loads(resp.data)
        self.assertIn('error', data)
        self.assertIn('límite', data['error'].lower())

    def test_public_refresh_missing_credentials(self):
        """Mock refresh_dataset raising RuntimeError returns 500."""
        slug = self._setup_link(allow_refresh=True)

        with patch('app.routes.public.refresh_dataset', side_effect=RuntimeError("Credentials missing")):
            resp = self.http.post(f'/p/{slug}/refresh')

        self.assertEqual(resp.status_code, 500)
        data = json.loads(resp.data)
        self.assertIn('error', data)


class TestAdminRefreshEndpoint(_BaseTestCase):
    """Tests for POST /reports/<id>/refresh endpoint (admin only)."""

    def _setup_report(self):
        """Create full hierarchy and return report_id."""
        with self.app.app_context():
            _, _, workspace, usuario = _create_test_hierarchy(db.session)
            report = _create_report(db.session, workspace, usuario)
            db.session.commit()
            return report.id

    def _login_admin(self):
        """Create an admin user and log in."""
        with self.app.app_context():
            user = User(id=_id(), username='admin', is_admin=True)
            user.set_password('adminpass')
            db.session.add(user)
            db.session.commit()

        self.http.post('/login', data={'username': 'admin', 'password': 'adminpass'})

    def test_admin_refresh_requires_login(self):
        """POST to /reports/<id>/refresh without login redirects to login (302)."""
        report_id = self._setup_report()

        resp = self.http.post(f'/reports/{report_id}/refresh')

        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers.get('Location', ''))

    def test_admin_refresh_success(self):
        """POST to /reports/<id>/refresh as admin returns 202."""
        report_id = self._setup_report()
        self._login_admin()

        with patch('app.routes.reports.refresh_dataset') as mock_refresh:
            mock_refresh.return_value = {"dataset_id": "admin-ds", "status": "accepted"}

            resp = self.http.post(f'/reports/{report_id}/refresh')

        self.assertEqual(resp.status_code, 202)
        data = json.loads(resp.data)
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['dataset_id'], 'admin-ds')


class TestPublicLinkModelAllowRefresh(_BaseTestCase):
    """Tests for the allow_refresh field on PublicLink model."""

    def test_public_link_model_allow_refresh_default(self):
        """PublicLink created without allow_refresh defaults to False."""
        with self.app.app_context():
            _, _, workspace, usuario = _create_test_hierarchy(db.session)
            report = _create_report(db.session, workspace, usuario)
            link = PublicLink(
                id=_id(),
                token=uuid.uuid4().hex[:16],
                custom_slug='default-slug',
                report_id_fk=report.id,
                is_active=True,
            )
            db.session.add(link)
            db.session.commit()

            fetched = PublicLink.query.filter_by(custom_slug='default-slug').first()
            self.assertFalse(fetched.allow_refresh)

    def test_public_link_model_allow_refresh_true(self):
        """PublicLink created with allow_refresh=True persists correctly."""
        with self.app.app_context():
            _, _, workspace, usuario = _create_test_hierarchy(db.session)
            report = _create_report(db.session, workspace, usuario)
            link = PublicLink(
                id=_id(),
                token=uuid.uuid4().hex[:16],
                custom_slug='refresh-enabled-slug',
                report_id_fk=report.id,
                is_active=True,
                allow_refresh=True,
            )
            db.session.add(link)
            db.session.commit()

            fetched = PublicLink.query.filter_by(custom_slug='refresh-enabled-slug').first()
            self.assertTrue(fetched.allow_refresh)


if __name__ == '__main__':
    unittest.main()
