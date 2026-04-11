"""
Comprehensive tests for the domain refactoring.
Covers the full hierarchy: Client → Tenant → Workspace → Report → PublicLink
and the Empresa ↔ Report M2M relationship.
"""
import os
import unittest
import json
import uuid

os.environ.setdefault('FERNET_KEY', 'o9eBKpiFgJRzgZNyBbFaQ8YeHImGZ5QpFnLn4EP9nj0=')
os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('PRIVATE_JWT_SECRET', 'test-jwt-secret')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')

from app import create_app, db
from app.models import (
    Client, Tenant, Workspace, Report, PublicLink,
    Empresa, UsuarioPBI, User, Visit, FuturaEmpresa
)
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


def _create_report(session, workspace, usuario, **kwargs):
    defaults = dict(id=_id(), name='Test Report', report_id=str(uuid.uuid4()),
                    workspace_id_fk=workspace.id, usuario_pbi_id=usuario.id,
                    es_publico=True, es_privado=False)
    defaults.update(kwargs)
    report = Report(**defaults)
    session.add(report)
    session.flush()
    return report


class _BaseTestCase(unittest.TestCase):
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

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()


class TestModelRelationships(_BaseTestCase):
    def test_full_chain_traversal(self):
        with self.app.app_context():
            client, tenant, workspace, usuario = _create_test_hierarchy(db.session)
            report = _create_report(db.session, workspace, usuario, name='Chain Report')
            db.session.commit()
            r = db.session.get(Report, report.id)
            self.assertEqual(r.workspace.id, workspace.id)
            self.assertEqual(r.workspace.tenant.id, tenant.id)
            self.assertEqual(r.workspace.tenant.client.id, client.id)

    def test_workspace_report_one_to_many(self):
        with self.app.app_context():
            _c, _t, workspace, usuario = _create_test_hierarchy(db.session)
            _create_report(db.session, workspace, usuario, name='R1')
            _create_report(db.session, workspace, usuario, name='R2')
            db.session.commit()
            self.assertEqual(workspace.reports.count(), 2)

    def test_report_public_links_one_to_many(self):
        with self.app.app_context():
            _c, _t, workspace, usuario = _create_test_hierarchy(db.session)
            report = _create_report(db.session, workspace, usuario)
            l1 = PublicLink(id=_id(), token='tok1', custom_slug='slug-1', report_id_fk=report.id, is_active=True)
            l2 = PublicLink(id=_id(), token='tok2', custom_slug='slug-2', report_id_fk=report.id, is_active=True)
            db.session.add_all([l1, l2])
            db.session.commit()
            self.assertEqual(report.public_links.count(), 2)
            self.assertEqual(l1.report.id, report.id)


class TestPublicLinkCreation(_BaseTestCase):
    def test_create_public_link_model(self):
        with self.app.app_context():
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            report = _create_report(db.session, ws, u)
            link = PublicLink(id=_id(), token='abc123', custom_slug='my-public-link',
                              report_id_fk=report.id, is_active=True)
            db.session.add(link)
            db.session.commit()
            saved = db.session.get(PublicLink, link.id)
            self.assertEqual(saved.report.id, report.id)
            self.assertTrue(saved.is_active)

    def test_public_link_unique_slug(self):
        with self.app.app_context():
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            r = _create_report(db.session, ws, u)
            l1 = PublicLink(id=_id(), token='t1', custom_slug='dup-slug', report_id_fk=r.id)
            db.session.add(l1)
            db.session.commit()
            l2 = PublicLink(id=_id(), token='t2', custom_slug='dup-slug', report_id_fk=r.id)
            db.session.add(l2)
            with self.assertRaises(Exception):
                db.session.commit()
            db.session.rollback()


class TestPowerBIUrlParsing(_BaseTestCase):
    def test_valid_url(self):
        from app.routes.reports import parse_powerbi_url
        ws_id, rpt_id = parse_powerbi_url(
            'https://app.powerbi.com/groups/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
            '/reports/11111111-2222-3333-4444-555555555555/ReportSection')
        self.assertEqual(ws_id, 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
        self.assertEqual(rpt_id, '11111111-2222-3333-4444-555555555555')

    def test_invalid_url_returns_none(self):
        from app.routes.reports import parse_powerbi_url
        ws_id, rpt_id = parse_powerbi_url('https://example.com/not-powerbi')
        self.assertIsNone(ws_id)
        self.assertIsNone(rpt_id)

    def test_empty_url(self):
        from app.routes.reports import parse_powerbi_url
        ws_id, rpt_id = parse_powerbi_url('')
        self.assertIsNone(ws_id)
        self.assertIsNone(rpt_id)


class TestEmpresaReportM2M(_BaseTestCase):
    def test_associate_empresa_to_report(self):
        with self.app.app_context():
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            report = _create_report(db.session, ws, u, es_privado=True)
            empresa = Empresa(id=_id(), nombre='E1', client_id=generate_client_id(),
                              client_secret_hash=hash_client_secret('s'), estado_activo=True)
            db.session.add(empresa)
            db.session.flush()
            empresa.reports.append(report)
            db.session.commit()
            self.assertIn(report, empresa.reports)
            self.assertIn(empresa, report.empresas)

    def test_multiple_empresas_share_report(self):
        with self.app.app_context():
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            report = _create_report(db.session, ws, u, es_privado=True)
            e1 = Empresa(id=_id(), nombre='E1', client_id=generate_client_id(),
                         client_secret_hash=hash_client_secret('s'), estado_activo=True)
            e2 = Empresa(id=_id(), nombre='E2', client_id=generate_client_id(),
                         client_secret_hash=hash_client_secret('s'), estado_activo=True)
            db.session.add_all([e1, e2])
            db.session.flush()
            report.empresas.append(e1)
            report.empresas.append(e2)
            db.session.commit()
            self.assertEqual(len(report.empresas), 2)

    def test_remove_association(self):
        with self.app.app_context():
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            report = _create_report(db.session, ws, u, es_privado=True)
            empresa = Empresa(id=_id(), nombre='E1', client_id=generate_client_id(),
                              client_secret_hash=hash_client_secret('s'), estado_activo=True)
            db.session.add(empresa)
            db.session.flush()
            empresa.reports.append(report)
            db.session.commit()
            empresa.reports.remove(report)
            db.session.commit()
            self.assertEqual(len(empresa.reports), 0)
            self.assertIsNotNone(db.session.get(Report, report.id))


class TestPrivacyFields(_BaseTestCase):
    def test_default_privacy_values(self):
        with self.app.app_context():
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            report = Report(id=_id(), name='Defaults', report_id='rpt-defaults',
                            workspace_id_fk=ws.id, usuario_pbi_id=u.id)
            db.session.add(report)
            db.session.commit()
            r = db.session.get(Report, report.id)
            self.assertTrue(r.es_publico)
            self.assertFalse(r.es_privado)

    def test_both_flags_true(self):
        with self.app.app_context():
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            report = _create_report(db.session, ws, u, es_publico=True, es_privado=True)
            db.session.commit()
            r = db.session.get(Report, report.id)
            self.assertTrue(r.es_publico)
            self.assertTrue(r.es_privado)

    def test_update_privacy_flags(self):
        with self.app.app_context():
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            report = _create_report(db.session, ws, u, es_publico=True, es_privado=False)
            db.session.commit()
            report.es_privado = True
            report.es_publico = False
            db.session.commit()
            r = db.session.get(Report, report.id)
            self.assertFalse(r.es_publico)
            self.assertTrue(r.es_privado)


class TestDeleteConstraints(_BaseTestCase):
    def test_delete_report_removes_public_links(self):
        with self.app.app_context():
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            report = _create_report(db.session, ws, u)
            link = PublicLink(id=_id(), token='ltok', custom_slug='lslug',
                              report_id_fk=report.id, is_active=True)
            db.session.add(link)
            db.session.commit()
            link_id = link.id
            db.session.delete(report)
            db.session.commit()
            self.assertIsNone(db.session.get(PublicLink, link_id))

    def test_delete_report_clears_empresa_m2m(self):
        with self.app.app_context():
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            report = _create_report(db.session, ws, u, es_privado=True)
            empresa = Empresa(id=_id(), nombre='E1', client_id=generate_client_id(),
                              client_secret_hash=hash_client_secret('s'), estado_activo=True)
            db.session.add(empresa)
            db.session.flush()
            empresa.reports.append(report)
            db.session.commit()
            empresa_id = empresa.id
            db.session.delete(report)
            db.session.commit()
            e = db.session.get(Empresa, empresa_id)
            self.assertIsNotNone(e)
            self.assertEqual(len(e.reports), 0)


class TestEdgeCases(_BaseTestCase):
    def test_duplicate_report_id_allowed_in_different_workspaces(self):
        with self.app.app_context():
            c, t, ws1, u = _create_test_hierarchy(db.session)
            ws2 = Workspace(id=_id(), name='WS2', workspace_id='ws-2-id', tenant_id_fk=t.id)
            db.session.add(ws2)
            db.session.flush()
            r1 = _create_report(db.session, ws1, u, report_id='same-guid')
            r2 = _create_report(db.session, ws2, u, report_id='same-guid')
            db.session.commit()
            self.assertNotEqual(r1.id, r2.id)
            self.assertEqual(r1.report_id, r2.report_id)

    def test_report_created_at_auto_set(self):
        with self.app.app_context():
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            report = _create_report(db.session, ws, u)
            db.session.commit()
            self.assertIsNotNone(report.created_at)

    def test_client_secret_encryption_roundtrip(self):
        with self.app.app_context():
            client = Client(id=_id(), name='SecClient', client_id='sec-cid')
            client.set_secret('my-super-secret')
            db.session.add(client)
            db.session.commit()
            c = db.session.get(Client, client.id)
            self.assertEqual(c.get_secret(), 'my-super-secret')

    def test_private_api_reports_response_shape(self):
        with self.app.app_context():
            cid = generate_client_id()
            csecret = generate_client_secret()
            empresa = Empresa(id=_id(), nombre='API Empresa', client_id=cid,
                              client_secret_hash=hash_client_secret(csecret), estado_activo=True)
            db.session.add(empresa)
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            report = _create_report(db.session, ws, u, name='API Report', es_privado=True)
            empresa.reports.append(report)
            db.session.commit()

            resp = self.http.post('/private/login', data=json.dumps({
                'client_id': cid, 'client_secret': csecret
            }), content_type='application/json')
            token = json.loads(resp.data)['access_token']

            resp = self.http.get('/private/reports', headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(resp.status_code, 200)
            data = json.loads(resp.data)
            self.assertIn('reports', data)
            self.assertEqual(len(data['reports']), 1)
            self.assertIn('id', data['reports'][0])
            self.assertIn('name', data['reports'][0])
            self.assertEqual(data['reports'][0]['name'], 'API Report')

    def test_report_config_endpoint_uses_report_id_param(self):
        with self.app.app_context():
            cid = generate_client_id()
            csecret = generate_client_secret()
            empresa = Empresa(id=_id(), nombre='Cfg Empresa', client_id=cid,
                              client_secret_hash=hash_client_secret(csecret), estado_activo=True)
            db.session.add(empresa)
            _c, _t, ws, u = _create_test_hierarchy(db.session)
            report = _create_report(db.session, ws, u, name='Cfg Report', es_privado=True)
            empresa.reports.append(report)
            db.session.commit()
            report_id = report.id

            resp = self.http.post('/private/login', data=json.dumps({
                'client_id': cid, 'client_secret': csecret
            }), content_type='application/json')
            token = json.loads(resp.data)['access_token']

            resp = self.http.get(f'/private/report-config?report_id={report_id}',
                                 headers={'Authorization': f'Bearer {token}'})
            # Should not be 400 (missing report_id) - any other code means param accepted
            self.assertNotEqual(resp.status_code, 400)


if __name__ == '__main__':
    unittest.main()
