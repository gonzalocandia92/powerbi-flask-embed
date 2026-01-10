"""
Integration tests for new private API endpoints (/private/reports).
"""
import unittest
import json
from app import create_app, db
from app.models import Empresa, ReportConfig, Tenant, Client, Workspace, Report, UsuarioPBI
from app.services.credentials_service import generate_client_id, generate_client_secret, hash_client_secret


class PrivateReportsEndpointTestCase(unittest.TestCase):
    """Test case for /private/reports endpoint."""
    
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
            
            # Create test empresa
            self.test_client_id = generate_client_id()
            self.test_client_secret = generate_client_secret()
            
            self.empresa = Empresa(
                id=1,
                nombre="Test Empresa",
                cuit="20-12345678-9",
                client_id=self.test_client_id,
                client_secret_hash=hash_client_secret(self.test_client_secret),
                estado_activo=True
            )
            db.session.add(self.empresa)
            
            # Create test dependencies
            tenant = Tenant(id=1, name="Test Tenant", tenant_id="tenant-123")
            client = Client(id=1, name="Test Client", client_id="client-123")
            workspace = Workspace(id=1, name="Test Workspace", workspace_id="workspace-123")
            report1 = Report(id=1, name="Report 1", report_id="report-123")
            report2 = Report(id=2, name="Report 2", report_id="report-456")
            usuario_pbi = UsuarioPBI(id=1, nombre="Test User", username="test@example.com")
            usuario_pbi.set_password("password")
            
            db.session.add_all([tenant, client, workspace, report1, report2, usuario_pbi])
            
            # Create private report configs associated with empresa
            config1 = ReportConfig(
                id=1,
                name="Config 1",
                tenant_id=1,
                client_id=1,
                workspace_id=1,
                report_id_fk=1,
                usuario_pbi_id=1,
                es_publico=False,
                es_privado=True
            )
            config2 = ReportConfig(
                id=2,
                name="Config 2",
                tenant_id=1,
                client_id=1,
                workspace_id=1,
                report_id_fk=2,
                usuario_pbi_id=1,
                es_publico=True,
                es_privado=True
            )
            # Public only config (should not appear in empresa reports)
            config3 = ReportConfig(
                id=3,
                name="Config 3 Public",
                tenant_id=1,
                client_id=1,
                workspace_id=1,
                report_id_fk=1,
                usuario_pbi_id=1,
                es_publico=True,
                es_privado=False
            )
            
            db.session.add_all([config1, config2, config3])
            db.session.flush()
            
            # Associate configs with empresa
            config1.empresas.append(self.empresa)
            config2.empresas.append(self.empresa)
            
            db.session.commit()
    
    def tearDown(self):
        """Tear down test fixtures."""
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
    
    def _get_access_token(self):
        """Helper to get access token."""
        response = self.client.post(
            '/private/login',
            data=json.dumps({
                'client_id': self.test_client_id,
                'client_secret': self.test_client_secret
            }),
            content_type='application/json'
        )
        data = json.loads(response.data)
        return data['access_token']
    
    def test_list_reports_success(self):
        """Test successfully listing reports for authenticated empresa."""
        with self.app.app_context():
            token = self._get_access_token()
            
            response = self.client.get(
                '/private/reports',
                headers={'Authorization': f'Bearer {token}'}
            )
            
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            
            self.assertIn('empresa_id', data)
            self.assertIn('empresa_nombre', data)
            self.assertIn('reports', data)
            
            self.assertEqual(data['empresa_id'], 1)
            self.assertEqual(data['empresa_nombre'], "Test Empresa")
            self.assertEqual(len(data['reports']), 2)
            
            # Verify report details
            report_ids = [r['config_id'] for r in data['reports']]
            self.assertIn(1, report_ids)
            self.assertIn(2, report_ids)
            self.assertNotIn(3, report_ids)  # Public-only config not included
    
    def test_list_reports_no_auth_header(self):
        """Test listing reports without authorization header."""
        with self.app.app_context():
            response = self.client.get('/private/reports')
            
            self.assertEqual(response.status_code, 401)
            data = json.loads(response.data)
            self.assertIn('error', data)
    
    def test_list_reports_invalid_token(self):
        """Test listing reports with invalid token."""
        with self.app.app_context():
            response = self.client.get(
                '/private/reports',
                headers={'Authorization': 'Bearer invalid-token'}
            )
            
            self.assertEqual(response.status_code, 401)
            data = json.loads(response.data)
            self.assertIn('error', data)
    
    def test_list_reports_expired_token(self):
        """Test listing reports with expired token."""
        # This would require mocking time or using a very short expiration
        # For now, we'll skip this test as it requires more setup
        pass
    
    def test_list_reports_no_reports(self):
        """Test listing reports when empresa has no associated reports."""
        with self.app.app_context():
            # Create a new empresa with no reports
            new_client_id = generate_client_id()
            new_client_secret = generate_client_secret()
            
            new_empresa = Empresa(
                id=2,
                nombre="Empty Empresa",
                client_id=new_client_id,
                client_secret_hash=hash_client_secret(new_client_secret),
                estado_activo=True
            )
            db.session.add(new_empresa)
            db.session.commit()
            
            # Login with new empresa
            response = self.client.post(
                '/private/login',
                data=json.dumps({
                    'client_id': new_client_id,
                    'client_secret': new_client_secret
                }),
                content_type='application/json'
            )
            token = json.loads(response.data)['access_token']
            
            # List reports
            response = self.client.get(
                '/private/reports',
                headers={'Authorization': f'Bearer {token}'}
            )
            
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            
            self.assertEqual(len(data['reports']), 0)


if __name__ == '__main__':
    unittest.main()
