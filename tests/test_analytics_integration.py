"""
Integration tests for analytics functionality.
"""
import os
import unittest
from datetime import datetime, timedelta

os.environ.setdefault('FERNET_KEY', 'o9eBKpiFgJRzgZNyBbFaQ8YeHImGZ5QpFnLn4EP9nj0=')
os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('PRIVATE_JWT_SECRET', 'test-jwt-secret')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')

from app import create_app, db
from app.models import Visit, PublicLink, Report, User, Client, Tenant, Workspace, UsuarioPBI
from app.utils.analytics import track_visit, generate_visitor_id


_next_id = 0


def _id():
    """Generate a unique ID for SQLite BigInteger columns."""
    global _next_id
    _next_id += 1
    return _next_id


def _create_test_hierarchy(session):
    """Create a full Client→Tenant→Workspace hierarchy for testing."""
    client = Client(id=_id(), name='Test Client', client_id='test-client-id')
    client.set_secret('test-secret')
    session.add(client)
    session.flush()

    tenant = Tenant(id=_id(), name='Test Tenant', tenant_id='test-tenant-id', client_id_fk=client.id)
    session.add(tenant)
    session.flush()

    workspace = Workspace(id=_id(), name='Test Workspace', workspace_id='test-workspace-id', tenant_id_fk=tenant.id)
    session.add(workspace)
    session.flush()

    usuario = UsuarioPBI(id=_id(), nombre='Test User PBI', username='test@pbi.com')
    usuario.set_password('test-pass')
    session.add(usuario)
    session.flush()

    return client, tenant, workspace, usuario


class AnalyticsIntegrationTestCase(unittest.TestCase):
    """Test case for analytics integration."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        with self.app.app_context():
            db.drop_all()
            db.create_all()

            user = User(id=_id(), username='testadmin', is_admin=True)
            user.set_password('testpass')
            db.session.add(user)
            db.session.commit()

    def tearDown(self):
        """Tear down test fixtures."""
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_track_visit_creates_record(self):
        """Test that tracking a visit creates a database record."""
        with self.app.app_context():
            with self.app.test_request_context(
                '/',
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                },
                environ_base={'REMOTE_ADDR': '192.168.1.1'}
            ):
                from flask import request
                visitor_id = generate_visitor_id()

                visit = track_visit('test-link', request, visitor_id)

                self.assertIsNotNone(visit)
                self.assertEqual(visit.link_slug, 'test-link')
                self.assertEqual(visit.visitor_id, visitor_id)
                self.assertFalse(visit.is_bot)

                db_visit = Visit.query.filter_by(visitor_id=visitor_id).first()
                self.assertIsNotNone(db_visit)
                self.assertEqual(db_visit.link_slug, 'test-link')

    def test_bot_visits_detected(self):
        """Test that bot visits are properly detected."""
        with self.app.app_context():
            with self.app.test_request_context(
                '/',
                headers={
                    'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1)',
                },
                environ_base={'REMOTE_ADDR': '192.168.1.1'}
            ):
                from flask import request
                visitor_id = generate_visitor_id()

                visit = track_visit('test-link', request, visitor_id)

                self.assertIsNotNone(visit)
                self.assertTrue(visit.is_bot)

    def test_analytics_api_endpoint(self):
        """Test the analytics API endpoint."""
        with self.app.app_context():
            for i in range(5):
                visit = Visit(
                    id=_id(),
                    link_slug='test-link',
                    timestamp=datetime.utcnow() - timedelta(days=i),
                    visitor_id=generate_visitor_id(),
                    is_bot=False
                )
                db.session.add(visit)
            db.session.commit()

            # Login
            response = self.client.post('/auth/login', data={
                'username': 'testadmin',
                'password': 'testpass'
            }, follow_redirects=True)

            # Call API
            response = self.client.get('/analytics/api/stats?link_slug=test-link&days=7')

            self.assertEqual(response.status_code, 200)
            data = response.get_json()

            self.assertTrue(data['success'])
            self.assertEqual(data['data']['overview']['total_visits'], 5)


if __name__ == '__main__':
    unittest.main()
