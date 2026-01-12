"""
Integration tests for private API endpoints.
"""
import unittest
import json
from app import create_app, db
from app.models import Empresa, ReportConfig, User, Tenant, Client, Workspace, Report, UsuarioPBI
from app.services.credentials_service import generate_client_id, generate_client_secret, hash_client_secret


class PrivateAPITestCase(unittest.TestCase):
    """Test case for private API endpoints."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        # Disable pool settings for in-memory SQLite
        self.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        self.client = self.app.test_client()
        
        with self.app.app_context():
            # Drop and recreate tables to ensure clean state
            db.drop_all()
            db.create_all()
            
            # Create test empresa
            self.test_client_id = generate_client_id()
            self.test_client_secret = generate_client_secret()
            
            self.empresa = Empresa(
                id=1,  # Explicit ID for SQLite compatibility
                nombre="Test Empresa",
                cuit="20-12345678-9",
                client_id=self.test_client_id,
                client_secret_hash=hash_client_secret(self.test_client_secret),
                estado_activo=True
            )
            db.session.add(self.empresa)
            db.session.commit()
    
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
            # Deactivate the empresa
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
            # Missing client_secret
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
            
            # Flask returns 400 for missing JSON body
            self.assertEqual(response.status_code, 400)
            data = json.loads(response.data)
            self.assertIn('error', data)
    
    def test_report_config_missing_auth_header(self):
        """Test report-config endpoint without Authorization header."""
        with self.app.app_context():
            response = self.client.post(
                '/private/report-config',
                data=json.dumps({'config_id': 1}),
                content_type='application/json'
            )
            
            self.assertEqual(response.status_code, 401)
            data = json.loads(response.data)
            self.assertIn('error', data)
    
    def test_report_config_invalid_token(self):
        """Test report-config endpoint with invalid token."""
        with self.app.app_context():
            response = self.client.post(
                '/private/report-config',
                data=json.dumps({'config_id': 1}),
                headers={'Authorization': 'Bearer invalid.token.here'},
                content_type='application/json'
            )
            
            self.assertEqual(response.status_code, 401)
            data = json.loads(response.data)
            self.assertIn('error', data)
    
    def test_report_config_missing_config_id(self):
        """Test report-config endpoint without config_id."""
        with self.app.app_context():
            # Get valid token first
            login_response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id,
                    'client_secret': self.test_client_secret
                }),
                content_type='application/json'
            )
            token = json.loads(login_response.data)['access_token']
            
            # Try to get config without config_id
            response = self.client.post(
                '/private/report-config',
                data=json.dumps({}),
                headers={'Authorization': f'Bearer {token}'},
                content_type='application/json'
            )
            
            self.assertEqual(response.status_code, 400)
            data = json.loads(response.data)
            self.assertIn('error', data)
    
    def test_report_config_not_found(self):
        """Test report-config endpoint with non-existent config."""
        with self.app.app_context():
            # Get valid token first
            login_response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id,
                    'client_secret': self.test_client_secret
                }),
                content_type='application/json'
            )
            token = json.loads(login_response.data)['access_token']
            
            # Try to get non-existent config
            response = self.client.post(
                '/private/report-config',
                data=json.dumps({'config_id': 9999}),
                headers={'Authorization': f'Bearer {token}'},
                content_type='application/json'
            )
            
            self.assertEqual(response.status_code, 404)
            data = json.loads(response.data)
            self.assertIn('error', data)
    
    def test_report_config_not_private(self):
        """Test report-config endpoint with public config."""
        with self.app.app_context():
            # Create test data
            tenant = Tenant(id=1, name="Test Tenant", tenant_id="test-tenant")
            client = Client(id=1, name="Test Client", client_id="test-client")
            workspace = Workspace(id=1, name="Test Workspace", workspace_id="test-workspace")
            report = Report(id=1, name="Test Report", report_id="test-report")
            usuario = UsuarioPBI(id=1, nombre="Test User", username="test@example.com")
            usuario.set_password("password")
            
            db.session.add_all([tenant, client, workspace, report, usuario])
            db.session.commit()
            
            # Create public config
            config = ReportConfig(
                id=1,
                name="Test Config",
                tenant_id=tenant.id,
                client_id=client.id,
                workspace_id=workspace.id,
                report_id_fk=report.id,
                usuario_pbi_id=usuario.id,
                tipo_privacidad='publico'
            )
            db.session.add(config)
            db.session.commit()
            
            # Get valid token
            login_response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id,
                    'client_secret': self.test_client_secret
                }),
                content_type='application/json'
            )
            token = json.loads(login_response.data)['access_token']
            
            # Try to get public config
            response = self.client.post(
                '/private/report-config',
                data=json.dumps({'config_id': config.id}),
                headers={'Authorization': f'Bearer {token}'},
                content_type='application/json'
            )
            
            self.assertEqual(response.status_code, 403)
            data = json.loads(response.data)
            self.assertIn('error', data)
    
    def test_report_config_wrong_client(self):
        """Test report-config endpoint with config belonging to different client."""
        with self.app.app_context():
            # Create another private client
            other_client = ClientePrivado(
                id=2,  # Explicit ID for SQLite compatibility
                nombre="Other Client",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret(generate_client_secret()),
                estado_activo=True
            )
            db.session.add(other_client)
            
            # Create test data
            tenant = Tenant(id=1, name="Test Tenant", tenant_id="test-tenant")
            client = Client(id=1, name="Test Client", client_id="test-client")
            workspace = Workspace(id=1, name="Test Workspace", workspace_id="test-workspace")
            report = Report(id=1, name="Test Report", report_id="test-report")
            usuario = UsuarioPBI(id=1, nombre="Test User", username="test@example.com")
            usuario.set_password("password")
            
            db.session.add_all([tenant, client, workspace, report, usuario])
            db.session.commit()
            
            # Create private config for other client
            config = ReportConfig(
                id=1,
                name="Test Config",
                tenant_id=tenant.id,
                client_id=client.id,
                workspace_id=workspace.id,
                report_id_fk=report.id,
                usuario_pbi_id=usuario.id,
                tipo_privacidad='privado',
                cliente_privado_id=other_client.id
            )
            db.session.add(config)
            db.session.commit()
            
            # Get valid token for first client
            login_response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': self.test_client_id,
                    'client_secret': self.test_client_secret
                }),
                content_type='application/json'
            )
            token = json.loads(login_response.data)['access_token']
            
            # Try to get config belonging to other client
            response = self.client.post(
                '/private/report-config',
                data=json.dumps({'config_id': config.id}),
                headers={'Authorization': f'Bearer {token}'},
                content_type='application/json'
            )
            
            self.assertEqual(response.status_code, 403)
            data = json.loads(response.data)
            self.assertIn('error', data)


if __name__ == '__main__':
    unittest.main()
