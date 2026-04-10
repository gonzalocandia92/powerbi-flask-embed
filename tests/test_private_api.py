"""
Integration tests for private API endpoints.
"""
import os
import unittest
import json

os.environ.setdefault('FERNET_KEY', 'o9eBKpiFgJRzgZNyBbFaQ8YeHImGZ5QpFnLn4EP9nj0=')
os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('PRIVATE_JWT_SECRET', 'test-jwt-secret')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')

from app import create_app, db
from app.models import Empresa, Report, User, Tenant, Client, Workspace, UsuarioPBI
from app.services.credentials_service import generate_client_id, generate_client_secret, hash_client_secret


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

    workspace = Workspace(id=_id(), name='Test Workspace', workspace_id='test-workspace-id', tenant_id_fk=tenant.id)
    session.add(workspace)
    session.flush()

    usuario = UsuarioPBI(id=_id(), nombre='Test User PBI', username='test@pbi.com')
    usuario.set_password('test-pass')
    session.add(usuario)
    session.flush()

    return client, tenant, workspace, usuario


class PrivateAPITestCase(unittest.TestCase):
    """Test case for private API endpoints."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        self.client = self.app.test_client()

        with self.app.app_context():
            db.drop_all()
            db.create_all()

            self.test_client_id = generate_client_id()
            self.test_client_secret = generate_client_secret()

            self.empresa = Empresa(
                id=_id(),
                nombre="Test Empresa",
                cuit="20-12345678-9",
                client_id=self.test_client_id,
                client_secret_hash=hash_client_secret(self.test_client_secret),
                estado_activo=True
            )
            db.session.add(self.empresa)
            db.session.commit()
            self._empresa_id = self.empresa.id

    def tearDown(self):
        """Tear down test fixtures."""
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_login_success(self):
        """Test successful login with valid credentials."""
        with self.app.app_context():
            response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id,
                    'client_secret': self.test_client_secret
                }),
                content_type='application/json'
            )

            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)

            self.assertIn('access_token', data)
            self.assertIn('token_type', data)
            self.assertIn('expires_in', data)
            self.assertEqual(data['token_type'], 'Bearer')

    def test_login_invalid_client_id(self):
        """Test login with invalid client_id."""
        with self.app.app_context():
            response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': 'invalid-client-id',
                    'client_secret': self.test_client_secret
                }),
                content_type='application/json'
            )

            self.assertEqual(response.status_code, 401)
            data = json.loads(response.data)
            self.assertIn('error', data)

    def test_login_invalid_client_secret(self):
        """Test login with invalid client_secret."""
        with self.app.app_context():
            response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id,
                    'client_secret': 'wrong-secret'
                }),
                content_type='application/json'
            )

            self.assertEqual(response.status_code, 401)
            data = json.loads(response.data)
            self.assertIn('error', data)

    def test_login_inactive_client(self):
        """Test login with inactive empresa."""
        with self.app.app_context():
            empresa = Empresa.query.filter_by(client_id=self.test_client_id).first()
            empresa.estado_activo = False
            db.session.commit()

            response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id,
                    'client_secret': self.test_client_secret
                }),
                content_type='application/json'
            )

            self.assertEqual(response.status_code, 403)
            data = json.loads(response.data)
            self.assertIn('error', data)

    def test_login_missing_fields(self):
        """Test login with missing fields."""
        with self.app.app_context():
            response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id
                }),
                content_type='application/json'
            )

            self.assertEqual(response.status_code, 400)
            data = json.loads(response.data)
            self.assertIn('error', data)

    def test_login_no_json_body(self):
        """Test login without JSON body."""
        with self.app.app_context():
            response = self.client.post('/private/login')

            self.assertEqual(response.status_code, 400)
            data = json.loads(response.data)
            self.assertIn('error', data)

    def test_report_config_missing_auth_header(self):
        """Test report-config endpoint without Authorization header."""
        with self.app.app_context():
            response = self.client.get(
                '/private/report-config?report_id=1'
            )

            self.assertEqual(response.status_code, 401)
            data = json.loads(response.data)
            self.assertIn('error', data)

    def test_report_config_invalid_token(self):
        """Test report-config endpoint with invalid token."""
        with self.app.app_context():
            response = self.client.get(
                '/private/report-config?report_id=1',
                headers={'Authorization': 'Bearer invalid.token.here'}
            )

            self.assertEqual(response.status_code, 401)
            data = json.loads(response.data)
            self.assertIn('error', data)

    def test_report_config_missing_report_id(self):
        """Test report-config endpoint without report_id."""
        with self.app.app_context():
            login_response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id,
                    'client_secret': self.test_client_secret
                }),
                content_type='application/json'
            )
            token = json.loads(login_response.data)['access_token']

            response = self.client.get(
                '/private/report-config',
                headers={'Authorization': f'Bearer {token}'}
            )

            self.assertEqual(response.status_code, 400)
            data = json.loads(response.data)
            self.assertIn('error', data)

    def test_report_config_not_found(self):
        """Test report-config endpoint with non-existent report."""
        with self.app.app_context():
            login_response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id,
                    'client_secret': self.test_client_secret
                }),
                content_type='application/json'
            )
            token = json.loads(login_response.data)['access_token']

            response = self.client.get(
                '/private/report-config?report_id=9999',
                headers={'Authorization': f'Bearer {token}'}
            )

            self.assertEqual(response.status_code, 404)
            data = json.loads(response.data)
            self.assertIn('error', data)

    def test_report_config_accepts_config_id_param(self):
        """Test report-config endpoint backward compat with config_id param."""
        with self.app.app_context():
            login_response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id,
                    'client_secret': self.test_client_secret
                }),
                content_type='application/json'
            )
            token = json.loads(login_response.data)['access_token']

            response = self.client.get(
                '/private/report-config?config_id=9999',
                headers={'Authorization': f'Bearer {token}'}
            )

            self.assertEqual(response.status_code, 404)
            data = json.loads(response.data)
            self.assertIn('error', data)

    def test_report_config_not_private(self):
        """Test report-config endpoint with a public-only report."""
        with self.app.app_context():
            _client, _tenant, workspace, usuario = _create_test_hierarchy(db.session)

            report = Report(
                id=_id(),
                name="Public Report",
                report_id="public-report-guid",
                workspace_id_fk=workspace.id,
                usuario_pbi_id=usuario.id,
                es_publico=True,
                es_privado=False
            )
            db.session.add(report)
            db.session.commit()

            login_response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id,
                    'client_secret': self.test_client_secret
                }),
                content_type='application/json'
            )
            token = json.loads(login_response.data)['access_token']

            response = self.client.get(
                f'/private/report-config?report_id={report.id}',
                headers={'Authorization': f'Bearer {token}'}
            )

            self.assertEqual(response.status_code, 403)
            data = json.loads(response.data)
            self.assertIn('error', data)

    def test_report_config_wrong_empresa(self):
        """Test report-config endpoint with report not associated with this empresa."""
        with self.app.app_context():
            other_empresa = Empresa(
                id=_id(),
                nombre="Other Empresa",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret(generate_client_secret()),
                estado_activo=True
            )
            db.session.add(other_empresa)

            _client, _tenant, workspace, usuario = _create_test_hierarchy(db.session)

            report = Report(
                id=_id(),
                name="Private Report",
                report_id="private-report-guid",
                workspace_id_fk=workspace.id,
                usuario_pbi_id=usuario.id,
                es_publico=False,
                es_privado=True
            )
            db.session.add(report)
            db.session.flush()

            other_empresa.reports.append(report)
            db.session.commit()

            login_response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id,
                    'client_secret': self.test_client_secret
                }),
                content_type='application/json'
            )
            token = json.loads(login_response.data)['access_token']

            response = self.client.get(
                f'/private/report-config?report_id={report.id}',
                headers={'Authorization': f'Bearer {token}'}
            )

            self.assertEqual(response.status_code, 403)
            data = json.loads(response.data)
            self.assertIn('error', data)


if __name__ == '__main__':
    unittest.main()
