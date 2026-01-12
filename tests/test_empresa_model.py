"""
Unit tests for Empresa model and many-to-many relationships.
"""
import unittest
from datetime import datetime
from app import create_app, db
from app.models import Empresa, ReportConfig, FuturaEmpresa, Tenant, Client, Workspace, Report, UsuarioPBI, User
from app.services.credentials_service import generate_client_id, generate_client_secret, hash_client_secret


class EmpresaModelTestCase(unittest.TestCase):
    """Test case for Empresa model."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        
        with self.app.app_context():
            db.drop_all()
            db.create_all()
    
    def tearDown(self):
        """Tear down test fixtures."""
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
    
    def test_create_empresa(self):
        """Test creating an empresa."""
        with self.app.app_context():
            empresa = Empresa(
                id=1,
                nombre="Test Empresa",
                cuit="20-12345678-9",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret"),
                estado_activo=True
            )
            db.session.add(empresa)
            db.session.commit()
            
            retrieved = Empresa.query.get(1)
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.nombre, "Test Empresa")
            self.assertEqual(retrieved.cuit, "20-12345678-9")
            self.assertTrue(retrieved.estado_activo)
    
    def test_empresa_without_cuit(self):
        """Test creating an empresa without CUIT."""
        with self.app.app_context():
            empresa = Empresa(
                id=1,
                nombre="Test Empresa",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret"),
                estado_activo=True
            )
            db.session.add(empresa)
            db.session.commit()
            
            retrieved = Empresa.query.get(1)
            self.assertIsNone(retrieved.cuit)


class EmpresaReportConfigRelationshipTestCase(unittest.TestCase):
    """Test case for many-to-many relationship between Empresa and ReportConfig."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        
        with self.app.app_context():
            db.drop_all()
            db.create_all()
            
            # Create test dependencies
            tenant = Tenant(id=1, name="Test Tenant", tenant_id="tenant-123")
            client = Client(id=1, name="Test Client", client_id="client-123")
            workspace = Workspace(id=1, name="Test Workspace", workspace_id="workspace-123")
            report = Report(id=1, name="Test Report", report_id="report-123")
            usuario_pbi = UsuarioPBI(id=1, nombre="Test User", username="test@example.com")
            usuario_pbi.set_password("password")
            
            db.session.add_all([tenant, client, workspace, report, usuario_pbi])
            db.session.commit()
    
    def tearDown(self):
        """Tear down test fixtures."""
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
    
    def test_many_to_many_relationship(self):
        """Test many-to-many relationship between Empresa and ReportConfig."""
        with self.app.app_context():
            # Create empresas
            empresa1 = Empresa(
                id=1,
                nombre="Empresa 1",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret1"),
                estado_activo=True
            )
            empresa2 = Empresa(
                id=2,
                nombre="Empresa 2",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret2"),
                estado_activo=True
            )
            db.session.add_all([empresa1, empresa2])
            
            # Create report config
            config = ReportConfig(
                id=1,
                name="Test Config",
                tenant_id=1,
                client_id=1,
                workspace_id=1,
                report_id_fk=1,
                usuario_pbi_id=1,
                es_publico=True,
                es_privado=True
            )
            db.session.add(config)
            db.session.flush()
            
            # Associate both empresas with the config
            config.empresas.append(empresa1)
            config.empresas.append(empresa2)
            db.session.commit()
            
            # Verify relationships
            retrieved_config = ReportConfig.query.get(1)
            self.assertEqual(len(retrieved_config.empresas), 2)
            
            retrieved_empresa1 = Empresa.query.get(1)
            self.assertEqual(len(retrieved_empresa1.report_configs), 1)
            self.assertEqual(retrieved_empresa1.report_configs[0].name, "Test Config")
    
    def test_remove_empresa_from_config(self):
        """Test removing an empresa from a config."""
        with self.app.app_context():
            # Create empresa and config
            empresa = Empresa(
                id=1,
                nombre="Test Empresa",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret"),
                estado_activo=True
            )
            db.session.add(empresa)
            
            config = ReportConfig(
                id=1,
                name="Test Config",
                tenant_id=1,
                client_id=1,
                workspace_id=1,
                report_id_fk=1,
                usuario_pbi_id=1,
                es_publico=False,
                es_privado=True
            )
            db.session.add(config)
            db.session.flush()
            
            config.empresas.append(empresa)
            db.session.commit()
            
            # Verify association
            self.assertEqual(len(config.empresas), 1)
            
            # Remove association
            config.empresas.remove(empresa)
            db.session.commit()
            
            # Verify removal
            retrieved_config = ReportConfig.query.get(1)
            self.assertEqual(len(retrieved_config.empresas), 0)


class FuturaEmpresaTestCase(unittest.TestCase):
    """Test case for FuturaEmpresa model."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        
        with self.app.app_context():
            db.drop_all()
            db.create_all()
    
    def tearDown(self):
        """Tear down test fixtures."""
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
    
    def test_create_futura_empresa(self):
        """Test creating a futura empresa."""
        with self.app.app_context():
            futura = FuturaEmpresa(
                id=1,
                external_id="EXT-001",
                nombre="Future Company",
                cuit="20-98765432-1",
                email="contact@futurecompany.com",
                telefono="+54 11 1234-5678",
                direccion="Av. Test 123",
                estado="pendiente"
            )
            db.session.add(futura)
            db.session.commit()
            
            retrieved = FuturaEmpresa.query.get(1)
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.nombre, "Future Company")
            self.assertEqual(retrieved.estado, "pendiente")
            self.assertIsNone(retrieved.fecha_procesamiento)
    
    def test_confirm_futura_empresa(self):
        """Test confirming a futura empresa and linking to created empresa."""
        with self.app.app_context():
            # Create user for processing
            user = User(id=1, username="admin", is_admin=True)
            user.set_password("password")
            db.session.add(user)
            
            # Create futura empresa
            futura = FuturaEmpresa(
                id=1,
                external_id="EXT-001",
                nombre="Future Company",
                cuit="20-98765432-1",
                estado="pendiente"
            )
            db.session.add(futura)
            db.session.flush()
            
            # Create empresa
            empresa = Empresa(
                id=1,
                nombre=futura.nombre,
                cuit=futura.cuit,
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret"),
                estado_activo=True
            )
            db.session.add(empresa)
            db.session.flush()
            
            # Confirm futura empresa
            futura.estado = "confirmada"
            futura.fecha_procesamiento = datetime.utcnow()
            futura.procesado_por_user_id = 1
            futura.empresa_id = empresa.id
            db.session.commit()
            
            # Verify
            retrieved_futura = FuturaEmpresa.query.get(1)
            self.assertEqual(retrieved_futura.estado, "confirmada")
            self.assertIsNotNone(retrieved_futura.fecha_procesamiento)
            self.assertEqual(retrieved_futura.empresa_id, 1)
            self.assertIsNotNone(retrieved_futura.empresa)
            self.assertEqual(retrieved_futura.empresa.nombre, "Future Company")


if __name__ == '__main__':
    unittest.main()
