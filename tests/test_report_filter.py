"""
Tests for Power BI filter support in /private/report-config endpoint.
"""
import os
import unittest
import json
from unittest.mock import patch

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
    client = Client(id=_id(), name='Test Client', client_id='filter-client-id')
    client.set_secret('test-secret')
    session.add(client)
    session.flush()
    tenant = Tenant(id=_id(), name='Test Tenant', tenant_id='filter-tenant-id', client_id_fk=client.id)
    session.add(tenant)
    session.flush()
    workspace = Workspace(id=_id(), name='Test Workspace', workspace_id='filter-workspace-id', tenant_id_fk=tenant.id)
    session.add(workspace)
    session.flush()
    usuario = UsuarioPBI(id=_id(), nombre='Filter User PBI', username='filter@pbi.com')
    usuario.set_password('test-pass')
    session.add(usuario)
    session.flush()
    return client, tenant, workspace, usuario


class ReportFilterConfigTestCase(unittest.TestCase):
    """Tests for filter configuration in /private/report-config."""

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

            empresa = Empresa(
                id=_id(), nombre='Filter Test Empresa', cuit='20-99999999-9',
                client_id=self.test_client_id,
                client_secret_hash=hash_client_secret(self.test_client_secret),
                estado_activo=True,
            )
            db.session.add(empresa)
            db.session.flush()

            _, _, workspace, usuario = _create_test_hierarchy(db.session)

            # Report with filter enabled
            self.report_with_filter = Report(
                id=_id(), name='Filtered Report', report_id='filtered-report-guid',
                workspace_id_fk=workspace.id, usuario_pbi_id=usuario.id,
                es_publico=False, es_privado=True,
                filter_enabled=True,
                filter_table='Sucursales',
                filter_column='ID',
            )
            # Report with filter disabled
            self.report_no_filter = Report(
                id=_id(), name='No Filter Report', report_id='no-filter-report-guid',
                workspace_id_fk=workspace.id, usuario_pbi_id=usuario.id,
                es_publico=False, es_privado=True,
                filter_enabled=False,
            )

            db.session.add_all([self.report_with_filter, self.report_no_filter])
            db.session.flush()

            empresa.reports.append(self.report_with_filter)
            empresa.reports.append(self.report_no_filter)
            db.session.commit()

            self._report_filter_id = self.report_with_filter.id
            self._report_no_filter_id = self.report_no_filter.id
            self._workspace_id = workspace.workspace_id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def _get_token(self):
        resp = self.client.post(
            '/private/login',
            data=json.dumps({'client_id': self.test_client_id, 'client_secret': self.test_client_secret}),
            content_type='application/json',
        )
        return json.loads(resp.data)['access_token']

    @patch('app.routes.private.get_embed_for_report')
    def test_filter_appended_to_embed_url(self, mock_embed):
        """When filter_enabled=True and ?filter=2 is passed, embedUrl contains the Power BI filter."""
        mock_embed.return_value = ('fake-token', 'https://app.powerbi.com/reportEmbed?reportId=abc', 'abc')

        with self.app.app_context():
            token = self._get_token()
            resp = self.client.get(
                f'/private/report-config?report_id={self._report_filter_id}&filter=2',
                headers={'Authorization': 'Bearer ' + token},
            )
            self.assertEqual(resp.status_code, 200)
            data = json.loads(resp.data)
            self.assertIn('filter=', data['embedUrl'])
            self.assertIn('Sucursales%2FID%20eq%202', data['embedUrl'])

    @patch('app.routes.private.get_embed_for_report')
    def test_no_filter_when_not_provided(self, mock_embed):
        """When filter_enabled=True but no ?filter= param, embedUrl has no filter appended."""
        mock_embed.return_value = ('fake-token', 'https://app.powerbi.com/reportEmbed?reportId=abc', 'abc')

        with self.app.app_context():
            token = self._get_token()
            resp = self.client.get(
                f'/private/report-config?report_id={self._report_filter_id}',
                headers={'Authorization': 'Bearer ' + token},
            )
            self.assertEqual(resp.status_code, 200)
            data = json.loads(resp.data)
            self.assertNotIn('Sucursales', data['embedUrl'])

    @patch('app.routes.private.get_embed_for_report')
    def test_filter_ignored_when_filter_disabled(self, mock_embed):
        """When filter_enabled=False and ?filter=2 is passed, embedUrl is returned unchanged."""
        base_url = 'https://app.powerbi.com/reportEmbed?reportId=xyz'
        mock_embed.return_value = ('fake-token', base_url, 'xyz')

        with self.app.app_context():
            token = self._get_token()
            resp = self.client.get(
                f'/private/report-config?report_id={self._report_no_filter_id}&filter=2',
                headers={'Authorization': 'Bearer ' + token},
            )
            self.assertEqual(resp.status_code, 200)
            data = json.loads(resp.data)
            self.assertEqual(data['embedUrl'], base_url)

    def test_report_model_has_filter_fields(self):
        """Report model stores filter_enabled, filter_table, filter_column."""
        with self.app.app_context():
            report = db.session.get(Report, self._report_filter_id)
            self.assertTrue(report.filter_enabled)
            self.assertEqual(report.filter_table, 'Sucursales')
            self.assertEqual(report.filter_column, 'ID')

    def test_report_model_filter_disabled_by_default(self):
        """Report model defaults filter_enabled to False."""
        with self.app.app_context():
            report = db.session.get(Report, self._report_no_filter_id)
            self.assertFalse(report.filter_enabled)
            self.assertIsNone(report.filter_table)
            self.assertIsNone(report.filter_column)


if __name__ == '__main__':
    unittest.main()
