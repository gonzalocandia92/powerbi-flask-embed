"""
Tests for public reports that require a login with an application user.

Access rule: authenticated + active user who has backoffice access, or the
reports.read permission plus membership of an active empresa associated with the
report. Reports without requires_login stay open.
"""
import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

os.environ.setdefault('FERNET_KEY', 'o9eBKpiFgJRzgZNyBbFaQ8YeHImGZ5QpFnLn4EP9nj0=')
os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('PRIVATE_JWT_SECRET', 'test-jwt-secret')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')

from flask_login import login_user

from app import create_app, db
from app.models import (
    ChatSession, Client, Empresa, Permission, PublicLink, Report, Role, Tenant,
    User, UserEmpresa, UsuarioPBI, Workspace,
)

_next_id = 0


def _id():
    global _next_id
    _next_id += 1
    return _next_id


class GateTestCase(unittest.TestCase):
    """Builds two empresas, a gated and an open public report, and users of every kind."""

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
            self._seed()

        embed = patch(
            'app.routes.public.get_embed_for_report',
            return_value=('token', 'https://app.powerbi.com/reportEmbed', 'report-id'),
        )
        self.mock_embed = embed.start()
        track = patch('app.routes.public.track_visit')
        self.mock_track = track.start()
        self.addCleanup(embed.stop)
        self.addCleanup(track.stop)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    # -- fixtures -----------------------------------------------------------

    def _make_user(self, username, roles=(), empresas=(), is_admin=False, is_active=True):
        user = User(id=_id(), username=username, is_admin=is_admin, is_active=is_active)
        user.set_password('pw')
        user.roles = list(roles)
        db.session.add(user)
        db.session.flush()
        for empresa in empresas:
            db.session.add(UserEmpresa(user_id=user.id, empresa_id=empresa.id))
        return user

    def _seed(self):
        read = Permission(id=_id(), name='reports.read')
        backoffice = Permission(id=_id(), name='backoffice.access')
        reader_role = Role(id=_id(), name='Lector de reportes', permissions=[read])
        staff_role = Role(id=_id(), name='Staff', permissions=[backoffice])
        db.session.add_all([read, backoffice, reader_role, staff_role])

        self.empresa_a = Empresa(
            id=_id(), nombre='Empresa A', client_id='cid-a', client_secret_hash='x', estado_activo=True
        )
        self.empresa_b = Empresa(
            id=_id(), nombre='Empresa B', client_id='cid-b', client_secret_hash='x', estado_activo=True
        )
        db.session.add_all([self.empresa_a, self.empresa_b])
        self.empresa_a_id = self.empresa_a.id
        self.empresa_b_id = self.empresa_b.id

        client = Client(id=_id(), name='c', client_id='client-id')
        client.set_secret('s')
        db.session.add(client)
        db.session.flush()
        tenant = Tenant(id=_id(), name='t', tenant_id='tenant-id', client_id_fk=client.id)
        db.session.add(tenant)
        db.session.flush()
        workspace = Workspace(id=_id(), name='w', workspace_id='ws-id', tenant_id_fk=tenant.id)
        usuario = UsuarioPBI(id=_id(), nombre='u', username='u@example.com')
        usuario.set_password('p')
        db.session.add_all([workspace, usuario])
        db.session.flush()

        def make_report(name, **kwargs):
            report = Report(
                id=_id(), name=name, report_id=str(uuid.uuid4()),
                workspace_id_fk=workspace.id, usuario_pbi_id=usuario.id,
                es_publico=True, es_privado=False, **kwargs
            )
            db.session.add(report)
            db.session.flush()
            return report

        gated = make_report('Gated', requires_login=True, allow_refresh=True)
        gated.empresas.append(self.empresa_a)
        open_report = make_report('Open')
        orphan = make_report('Orphan gated', requires_login=True)  # login required, no empresas
        for slug, report in (('gated', gated), ('open', open_report), ('orphan', orphan)):
            db.session.add(PublicLink(
                id=_id(), token=uuid.uuid4().hex[:16], custom_slug=slug,
                report_id_fk=report.id, is_active=True,
            ))
        self.gated_id = gated.id

        self._make_user('viewer', roles=[reader_role], empresas=[self.empresa_a])
        self._make_user('viewer_b', roles=[reader_role], empresas=[self.empresa_b])
        self._make_user('no_role', empresas=[self.empresa_a])
        self._make_user('no_member', roles=[reader_role])
        self._make_user('staff', roles=[staff_role])
        self._make_user('admin', is_admin=True)
        self._make_user('inactive', roles=[reader_role], empresas=[self.empresa_a], is_active=False)

        db.session.add(ChatSession(id=_id(), slug='gated', title='gated chat'))
        db.session.add(ChatSession(id=_id(), slug='open', title='open chat'))
        db.session.commit()

    def login(self, username, next_url=None):
        url = '/login' + (f'?next={next_url}' if next_url else '')
        return self.http.post(url, data={'username': username, 'password': 'pw'})

    def _session_ids(self):
        with self.app.app_context():
            return {s.slug: s.id for s in ChatSession.query.all()}


class PublicViewGateTest(GateTestCase):

    def test_anonymous_is_sent_to_login_and_nothing_is_generated(self):
        resp = self.http.get('/p/gated')

        self.assertEqual(resp.status_code, 302)
        location = resp.headers['Location']
        self.assertIn('/login', location)
        self.assertIn('next=/p/gated', location)
        self.mock_embed.assert_not_called()
        self.mock_track.assert_not_called()

    def test_login_redirect_keeps_the_query_string(self):
        resp = self.http.get('/p/gated?utm_source=mail')

        self.assertIn('utm_source', resp.headers['Location'])

    def test_open_report_is_unaffected(self):
        resp = self.http.get('/p/open')

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('no-store', resp.headers.get('Cache-Control', ''))

    def test_member_with_permission_can_view(self):
        self.login('viewer')

        resp = self.http.get('/p/gated')

        self.assertEqual(resp.status_code, 200)
        self.assertIn('no-store', resp.headers['Cache-Control'])
        self.mock_track.assert_called_once()

    def test_denied_users_get_403_without_a_powerbi_token(self):
        for username in ('viewer_b', 'no_role', 'no_member'):
            with self.subTest(username):
                self.http.get('/logout')
                self.login(username)
                self.mock_embed.reset_mock()

                resp = self.http.get('/p/gated')

                self.assertEqual(resp.status_code, 403)
                self.assertIn('Sin acceso a este reporte', resp.get_data(as_text=True))
                self.mock_embed.assert_not_called()

    def test_backoffice_staff_and_admin_can_view_any_gated_report(self):
        for username in ('staff', 'admin'):
            with self.subTest(username):
                self.http.get('/logout')
                self.login(username)

                self.assertEqual(self.http.get('/p/gated').status_code, 200)
                self.assertEqual(self.http.get('/p/orphan').status_code, 200)

    def test_gated_report_without_empresas_is_closed_to_regular_viewers(self):
        self.login('viewer')

        self.assertEqual(self.http.get('/p/orphan').status_code, 403)

    def test_inactive_empresa_does_not_grant_access(self):
        with self.app.app_context():
            db.session.get(Empresa, self.empresa_a_id).estado_activo = False
            db.session.commit()
        self.login('viewer')

        self.assertEqual(self.http.get('/p/gated').status_code, 403)

    def test_user_deactivated_after_login_loses_access(self):
        self.login('viewer')
        self.assertEqual(self.http.get('/p/gated').status_code, 200)

        with self.app.app_context():
            User.query.filter_by(username='viewer').one().is_active = False
            db.session.commit()

        resp = self.http.get('/p/gated')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

    def test_inactive_user_cannot_log_in(self):
        self.login('inactive')

        resp = self.http.get('/p/gated')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

    def test_turning_the_flag_off_reopens_the_report(self):
        with self.app.app_context():
            db.session.get(Report, self.gated_id).requires_login = False
            db.session.commit()

        self.assertEqual(self.http.get('/p/gated').status_code, 200)


class RefreshGateTest(GateTestCase):

    def test_anonymous_gets_401_and_no_refresh(self):
        with patch('app.routes.public.refresh_dataset') as refresh:
            resp = self.http.post('/p/gated/refresh')

        self.assertEqual(resp.status_code, 401)
        self.assertTrue(resp.get_json()['login_required'])
        refresh.assert_not_called()

    def test_logged_in_user_without_access_gets_403(self):
        self.login('viewer_b')
        with patch('app.routes.public.refresh_dataset') as refresh:
            resp = self.http.post('/p/gated/refresh')

        self.assertEqual(resp.status_code, 403)
        refresh.assert_not_called()

    def test_allowed_user_can_refresh(self):
        self.login('viewer')
        with patch('app.routes.public.refresh_dataset') as refresh:
            refresh.return_value = {'dataset_id': 'ds', 'status': 'accepted'}
            resp = self.http.post('/p/gated/refresh')

        self.assertEqual(resp.status_code, 202)


class ChatbotGateTest(GateTestCase):

    def test_models_endpoint_denies_anonymous_before_catalog_or_billing(self):
        with patch('app.routes.chatbot.ai_billing.resolve_report_billing_context') as billing:
            response = self.http.get('/api/chatbot/models?slug=gated')
        self.assertEqual(response.status_code, 401)
        billing.assert_not_called()

    def test_models_endpoint_denies_user_from_another_empresa(self):
        self.login('viewer_b')
        with patch('app.routes.chatbot.model_catalog.available_client_models') as catalog:
            response = self.http.get('/api/chatbot/models?slug=gated')
        self.assertEqual(response.status_code, 403)
        catalog.assert_not_called()

    def test_models_endpoint_keeps_catalog_for_authorized_reader(self):
        self.login('viewer')
        with patch('app.routes.chatbot.ai_billing.resolve_report_billing_context') as billing, \
                patch('app.routes.chatbot.model_catalog.available_client_models', return_value=[]) as catalog, \
                patch('app.routes.chatbot.model_catalog.session_model_key', return_value=None):
            billing.return_value.empresa_id = None
            response = self.http.get('/api/chatbot/models?slug=gated')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['models'], [])
        catalog.assert_called_once()

    def test_context_endpoint_requires_login_for_gated_slug(self):
        with patch('app.utils.chatbot_context.get_current_dataset_id', return_value='ds-1'):
            anonymous = self.http.get('/api/chatbot/context/gated')
            open_slug = self.http.get('/api/chatbot/context/open')
            self.login('viewer')
            allowed = self.http.get('/api/chatbot/context/gated')

        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(open_slug.status_code, 200)
        self.assertEqual(allowed.status_code, 200)

    def test_reports_listing_hides_gated_reports_from_everyone_but_backoffice(self):
        def slugs():
            return {r['slug'] for r in self.http.get('/api/chatbot/reports').get_json()}

        self.assertEqual(slugs(), {'open'})
        self.login('viewer')
        self.assertEqual(slugs(), {'open'})
        self.http.get('/logout')
        self.login('staff')
        self.assertEqual(slugs(), {'open', 'gated', 'orphan'})

    def test_sessions_of_gated_reports_are_hidden_from_everyone_but_backoffice(self):
        ids = self._session_ids()

        def listed():
            return {s['slug'] for s in self.http.get('/api/chatbot/sessions').get_json()}

        self.assertEqual(listed(), {'open'})
        self.assertEqual(self.http.get(f"/api/chatbot/sessions/{ids['gated']}").status_code, 404)
        self.assertEqual(self.http.get(f"/api/chatbot/sessions/{ids['open']}").status_code, 200)

        self.login('viewer')
        self.assertEqual(listed(), {'open'})
        self.assertEqual(self.http.get(f"/api/chatbot/sessions/{ids['gated']}").status_code, 404)

        self.http.get('/logout')
        self.login('staff')
        self.assertEqual(listed(), {'open', 'gated'})
        self.assertEqual(self.http.get(f"/api/chatbot/sessions/{ids['gated']}").status_code, 200)

    def test_chat_access_check(self):
        """The check /chat runs before reaching the chatbot service."""
        from app.routes.chatbot import _slug_access_error

        with self.app.test_request_context('/chat', method='POST'):
            response, status = _slug_access_error('gated')
            self.assertEqual(status, 401)
            self.assertIsNone(_slug_access_error('open'))
            self.assertIsNone(_slug_access_error('unknown-slug'))

        for username, expected in (('viewer', None), ('viewer_b', 403), ('staff', None)):
            with self.subTest(username):
                with self.app.app_context():
                    user = User.query.filter_by(username=username).one()
                    with self.app.test_request_context('/chat', method='POST'):
                        login_user(user)
                        result = _slug_access_error('gated')
                        if expected is None:
                            self.assertIsNone(result)
                        else:
                            self.assertEqual(result[1], expected)

    def test_chat_endpoint_end_to_end(self):
        # /chat is an async view; create_app() replaces Flask's async_to_sync, so asgiref is not needed.
        result = {'answer': 'ok', 'conversation_id': 1, 'report_id': 1}
        with patch(
            'app.routes.chatbot.chatbot_service.procesar_interaccion_completa',
            new=AsyncMock(return_value=result),
        ) as service:
            anonymous = self.http.post('/chat', json={'message': 'hola', 'slug': 'gated'})
            self.login('viewer')
            allowed = self.http.post('/chat', json={'message': 'hola', 'slug': 'gated'})

        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        service.assert_awaited_once()


class LoginFlowTest(GateTestCase):

    def test_login_returns_to_the_report_that_asked_for_it(self):
        resp = self.login('viewer', next_url='/p/gated')

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers['Location'].endswith('/p/gated'))
        self.assertEqual(self.http.get('/p/gated').status_code, 200)

    def test_report_viewers_land_on_their_account_page_not_the_backoffice(self):
        resp = self.login('viewer')

        self.assertTrue(resp.headers['Location'].endswith('/cuenta'))
        self.assertEqual(self.http.get('/cuenta').status_code, 200)

    def test_staff_still_land_on_the_backoffice(self):
        resp = self.login('staff')

        self.assertNotIn('/cuenta', resp.headers['Location'])

    def test_external_next_urls_are_ignored(self):
        resp = self.login('viewer', next_url='https://evil.example.com/')

        self.assertTrue(resp.headers['Location'].endswith('/cuenta'))

    def test_account_page_requires_login(self):
        resp = self.http.get('/cuenta')

        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])


class BackofficeIsolationTest(GateTestCase):
    """Report viewers must not reach any backoffice screen."""

    def test_viewer_is_blocked_from_backoffice_pages(self):
        self.login('viewer')

        for path in ('/', '/reports/', f'/reports/{self.gated_id}/detail', '/admin/users/', '/docs/', '/admin/empresas/'):
            with self.subTest(path):
                self.assertEqual(self.http.get(path).status_code, 403)

    def test_viewer_cannot_delete_a_report(self):
        self.login('viewer')

        resp = self.http.post(f'/reports/{self.gated_id}/delete')

        self.assertEqual(resp.status_code, 403)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Report, self.gated_id))


class AdminScreensTest(GateTestCase):

    def test_report_detail_flags_login_protected_reports(self):
        self.login('admin')

        gated = self.http.get(f'/reports/{self.gated_id}/detail').get_data(as_text=True)

        with self.app.app_context():
            open_id = Report.query.filter_by(name='Open').one().id
        open_html = self.http.get(f'/reports/{open_id}/detail').get_data(as_text=True)
        self.assertIn('Requiere login', gated)
        self.assertNotIn('Requiere login', open_html)

    def test_user_access_screen_offers_the_reader_role(self):
        self.login('admin')
        with self.app.app_context():
            user_id = User.query.filter_by(username='viewer').one().id

        resp = self.http.get(f'/admin/users/{user_id}/access')

        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Lector de reportes', html)
        self.assertIn('Lee reportes con login', html)

    def test_assigning_the_reader_role_and_empresa_grants_access(self):
        """The whole admin flow: an external user starts without access and gains it from the access screen."""
        with self.app.app_context():
            user = User(id=_id(), username='newcomer')
            user.set_password('pw')
            db.session.add(user)
            db.session.commit()
            user_id = user.id
            role_id = Role.query.filter_by(name='Lector de reportes').one().id
        self.login('newcomer')
        self.assertEqual(self.http.get('/p/gated').status_code, 403)
        self.http.get('/logout')

        self.login('admin')
        resp = self.http.post(
            f'/admin/users/{user_id}/access',
            data={'backoffice_role_ids': [role_id], 'company_ids': [self.empresa_a_id]},
        )
        self.assertEqual(resp.status_code, 302)
        self.http.get('/logout')

        self.login('newcomer')
        self.assertEqual(self.http.get('/p/gated').status_code, 200)
        self.assertEqual(self.http.get('/reports/').status_code, 403)


class ReportFormTest(GateTestCase):

    def _edit_data(self, report, **extra):
        data = {
            'name': report.name,
            'report_id': report.report_id,
            'workspace': report.workspace_id_fk,
            'usuario_pbi': report.usuario_pbi_id,
            'empresa_facturadora_id': 0,
            'es_publico': 'y',
        }
        data.update(extra)
        return data

    def test_form_shows_the_login_option(self):
        self.login('admin')

        html = self.http.get('/reports/new').get_data(as_text=True)

        self.assertIn('name="requires_login"', html)
        self.assertIn('reports.read', html)

    def test_edit_saves_requires_login(self):
        self.login('admin')
        with self.app.app_context():
            report = Report.query.filter_by(name='Open').one()
            report_id, data = report.id, self._edit_data(report, requires_login='y')

        resp = self.http.post(f'/reports/{report_id}/edit', data=data)

        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            self.assertTrue(db.session.get(Report, report_id).requires_login)

    def test_warns_when_login_is_required_but_no_empresa_is_associated(self):
        self.login('admin')
        with self.app.app_context():
            report = Report.query.filter_by(name='Open').one()
            report_id, data = report.id, self._edit_data(report, requires_login='y')

        self.http.post(f'/reports/{report_id}/edit', data=data)

        with self.http.session_transaction() as session:
            messages = [message for _category, message in session.get('_flashes', [])]
        self.assertTrue(any('no tiene empresas asociadas' in message for message in messages))

    def test_no_warning_when_an_empresa_is_associated(self):
        self.login('admin')
        with self.app.app_context():
            report = Report.query.filter_by(name='Open').one()
            report_id = report.id
            data = self._edit_data(report, requires_login='y')
            data['empresas'] = [self.empresa_a_id]

        self.http.post(f'/reports/{report_id}/edit', data=data)

        with self.http.session_transaction() as session:
            messages = [message for _category, message in session.get('_flashes', [])]
        self.assertFalse(any('no tiene empresas asociadas' in message for message in messages))


if __name__ == '__main__':
    unittest.main()
