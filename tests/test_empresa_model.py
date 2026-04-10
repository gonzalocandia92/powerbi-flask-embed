"""
Unit tests for Empresa model and many-to-many relationships with Report.
"""
import os
import unittest
from datetime import datetime

os.environ.setdefault('FERNET_KEY', 'o9eBKpiFgJRzgZNyBbFaQ8YeHImGZ5QpFnLn4EP9nj0=')
os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('PRIVATE_JWT_SECRET', 'test-jwt-secret')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')

from app import create_app, db
from app.models import Empresa, FuturaEmpresa, Client, Tenant, Workspace, Report, UsuarioPBI, User
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
                id=_id(),
                nombre="Test Empresa",
                cuit="20-12345678-9",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret"),
                estado_activo=True
            )
            db.session.add(empresa)
            db.session.commit()

            retrieved = db.session.get(Empresa, empresa.id)
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.nombre, "Test Empresa")
            self.assertEqual(retrieved.cuit, "20-12345678-9")
            self.assertTrue(retrieved.estado_activo)

    def test_empresa_without_cuit(self):
        """Test creating an empresa without CUIT."""
        with self.app.app_context():
            empresa = Empresa(
                id=_id(),
                nombre="Test Empresa",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret"),
                estado_activo=True
            )
            db.session.add(empresa)
            db.session.commit()

            retrieved = db.session.get(Empresa, empresa.id)
            self.assertIsNone(retrieved.cuit)


class EmpresaReportRelationshipTestCase(unittest.TestCase):
    """Test case for many-to-many relationship between Empresa and Report."""

    def setUp(self):
        """Set up test fixtures."""
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}

        with self.app.app_context():
            db.drop_all()
            db.create_all()

            _client, _tenant, workspace, usuario = _create_test_hierarchy(db.session)
            self._workspace_id = workspace.id
            self._usuario_id = usuario.id
            db.session.commit()

    def tearDown(self):
        """Tear down test fixtures."""
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_many_to_many_relationship(self):
        """Test many-to-many relationship between Empresa and Report."""
        with self.app.app_context():
            empresa1 = Empresa(
                id=_id(),
                nombre="Empresa 1",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret1"),
                estado_activo=True
            )
            empresa2 = Empresa(
                id=_id(),
                nombre="Empresa 2",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret2"),
                estado_activo=True
            )
            db.session.add_all([empresa1, empresa2])

            report = Report(
                id=_id(),
                name="Test Report",
                report_id="test-report-guid",
                workspace_id_fk=self._workspace_id,
                usuario_pbi_id=self._usuario_id,
                es_publico=True,
                es_privado=True
            )
            db.session.add(report)
            db.session.flush()

            report.empresas.append(empresa1)
            report.empresas.append(empresa2)
            db.session.commit()

            retrieved_report = db.session.get(Report, report.id)
            self.assertEqual(len(retrieved_report.empresas), 2)

            retrieved_empresa1 = db.session.get(Empresa, empresa1.id)
            self.assertEqual(len(retrieved_empresa1.reports), 1)
            self.assertEqual(retrieved_empresa1.reports[0].name, "Test Report")

    def test_remove_empresa_from_report(self):
        """Test removing an empresa from a report."""
        with self.app.app_context():
            empresa = Empresa(
                id=_id(),
                nombre="Test Empresa",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret"),
                estado_activo=True
            )
            db.session.add(empresa)

            report = Report(
                id=_id(),
                name="Test Report",
                report_id="test-report-guid",
                workspace_id_fk=self._workspace_id,
                usuario_pbi_id=self._usuario_id,
                es_publico=False,
                es_privado=True
            )
            db.session.add(report)
            db.session.flush()

            report.empresas.append(empresa)
            db.session.commit()

            self.assertEqual(len(report.empresas), 1)

            report.empresas.remove(empresa)
            db.session.commit()

            retrieved_report = db.session.get(Report, report.id)
            self.assertEqual(len(retrieved_report.empresas), 0)

    def test_empresa_reports_bidirectional(self):
        """Test that the M2M relationship is accessible from both sides."""
        with self.app.app_context():
            empresa = Empresa(
                id=_id(),
                nombre="Bidi Empresa",
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret"),
                estado_activo=True
            )
            db.session.add(empresa)

            report = Report(
                id=_id(),
                name="Bidi Report",
                report_id="bidi-report-guid",
                workspace_id_fk=self._workspace_id,
                usuario_pbi_id=self._usuario_id,
                es_publico=True,
                es_privado=True
            )
            db.session.add(report)
            db.session.flush()

            empresa.reports.append(report)
            db.session.commit()

            self.assertIn(empresa, report.empresas)
            self.assertIn(report, empresa.reports)


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
                id=_id(),
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

            retrieved = db.session.get(FuturaEmpresa, futura.id)
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.nombre, "Future Company")
            self.assertEqual(retrieved.estado, "pendiente")
            self.assertIsNone(retrieved.fecha_procesamiento)

    def test_confirm_futura_empresa(self):
        """Test confirming a futura empresa and linking to created empresa."""
        with self.app.app_context():
            user = User(id=_id(), username="admin", is_admin=True)
            user.set_password("password")
            db.session.add(user)

            futura = FuturaEmpresa(
                id=_id(),
                external_id="EXT-001",
                nombre="Future Company",
                cuit="20-98765432-1",
                estado="pendiente"
            )
            db.session.add(futura)
            db.session.flush()

            empresa = Empresa(
                id=_id(),
                nombre=futura.nombre,
                cuit=futura.cuit,
                client_id=generate_client_id(),
                client_secret_hash=hash_client_secret("secret"),
                estado_activo=True
            )
            db.session.add(empresa)
            db.session.flush()

            futura.estado = "confirmada"
            futura.fecha_procesamiento = datetime.utcnow()
            futura.procesado_por_user_id = user.id
            futura.empresa_id = empresa.id
            db.session.commit()

            retrieved_futura = db.session.get(FuturaEmpresa, futura.id)
            self.assertEqual(retrieved_futura.estado, "confirmada")
            self.assertIsNotNone(retrieved_futura.fecha_procesamiento)
            self.assertEqual(retrieved_futura.empresa_id, empresa.id)
            self.assertIsNotNone(retrieved_futura.empresa)
            self.assertEqual(retrieved_futura.empresa.nombre, "Future Company")


if __name__ == '__main__':
    unittest.main()
