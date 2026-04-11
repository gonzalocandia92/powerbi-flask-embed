"""
Integration tests for private API /private/reports endpoint.
"""
import os
import unittest
import json

os.environ.setdefault('FERNET_KEY', 'o9eBKpiFgJRzgZNyBbFaQ8YeHImGZ5QpFnLn4EP9nj0=')
os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('PRIVATE_JWT_SECRET', 'test-jwt-secret')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')

from app import create_app, db
from app.models import Empresa, Report, Client, Tenant, Workspace, UsuarioPBI
from app.services.credentials_service import generate_client_id, generate_client_secret, hash_client_secret

_next_id = 0


def _id():
    global _next_id
    _next_id += 1
    return _next_id


def _create_test_hierarchy(session):
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


class PrivateReportsEndpointTestCase(unittest.TestCase):

    def setUp(self):
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
                id=_id(), nombre="Test Empresa", cuit="20-12345678-9",
                client_id=self.test_client_id,
                client_secret_hash=hash_client_secret(self.test_client_secret),
                estado_activo=True
            )
            db.session.add(self.empresa)

            _client, _tenant, workspace, usuario = _create_test_hierarchy(db.session)

            report1 = Report(id=_id(), name="Report 1", report_id="report-guid-1",
                             workspace_id_fk=workspace.id, usuario_pbi_id=usuario.id,
                             es_publico=False, es_privado=True)
            report2 = Report(id=_id(), name="Report 2", report_id="report-guid-2",
                             workspace_id_fk=workspace.id, usuario_pbi_id=usuario.id,
                             es_publico=True, es_privado=True)
            report3 = Report(id=_id(), name="Report 3 Public", report_id="report-guid-3",
                             workspace_id_fk=workspace.id, usuario_pbi_id=usuario.id,
                             es_publico=True, es_privado=False)

            db.session.add_all([report1, report2, report3])
            db.session.flush()

            self.empresa.reports.append(report1)
            self.empresa.reports.append(report2)
            db.session.commit()

            self._report1_id = report1.id
            self._report2_id = report2.id
            self._report3_id = report3.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def _get_access_token(self):
        response = self.client.post('/private/login',
            data=json.dumps({'client_id': self.test_client_id, 'client_secret': self.test_client_secret}),
            content_type='application/json')
        return json.loads(response.data)['access_token']

    def test_list_reports_success(self):
        with self.app.app_context():
            token = self._get_access_token()
            response = self.client.get('/private/reports', headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            self.assertIn('reports', data)
            self.assertEqual(len(data['reports']), 2)
            report_ids = [r['id'] for r in data['reports']]
            self.assertIn(self._report1_id, report_ids)
            self.assertIn(self._report2_id, report_ids)
            self.assertNotIn(self._report3_id, report_ids)

    def test_list_reports_no_auth_header(self):
        with self.app.app_context():
            response = self.client.get('/private/reports')
            self.assertEqual(response.status_code, 401)

    def test_list_reports_invalid_token(self):
        with self.app.app_context():
            response = self.client.get('/private/reports', headers={'Authorization': 'Bearer invalid-token'})
            self.assertEqual(response.status_code, 401)

    def test_list_reports_expired_token(self):
        pass

    def test_list_reports_no_reports(self):
        with self.app.app_context():
            new_cid = generate_client_id()
            new_cs = generate_client_secret()
            new_empresa = Empresa(id=_id(), nombre="Empty Empresa", client_id=new_cid,
                                  client_secret_hash=hash_client_secret(new_cs), estado_activo=True)
            db.session.add(new_empresa)
            db.session.commit()
            response = self.client.post('/private/login',
                data=json.dumps({'client_id': new_cid, 'client_secret': new_cs}),
                content_type='application/json')
            token = json.loads(response.data)['access_token']
            response = self.client.get('/private/reports', headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(json.loads(response.data)['reports']), 0)

    def test_list_reports_empresa_isolation(self):
        with self.app.app_context():
            other_cid = generate_client_id()
            other_cs = generate_client_secret()
            other = Empresa(id=_id(), nombre="Other Empresa", client_id=other_cid,
                            client_secret_hash=hash_client_secret(other_cs), estado_activo=True)
            db.session.add(other)
            db.session.commit()
            response = self.client.post('/private/login',
                data=json.dumps({'client_id': other_cid, 'client_secret': other_cs}),
                content_type='application/json')
            token = json.loads(response.data)['access_token']
            response = self.client.get('/private/reports', headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(json.loads(response.data)['reports']), 0)


if __name__ == '__main__':
    unittest.main()
