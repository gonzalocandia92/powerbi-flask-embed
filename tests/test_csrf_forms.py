"""
Regression tests for CSRF handling on confirm/delete buttons.

Delete buttons build a <form> in JavaScript after the user confirms and call
form.submit(). Such a form only reaches the server with a csrf_token if the
layout adds it at submit time; otherwise CSRFProtect answers 400.
"""
import os
import re
import unittest
import uuid

os.environ.setdefault('FERNET_KEY', 'o9eBKpiFgJRzgZNyBbFaQ8YeHImGZ5QpFnLn4EP9nj0=')
os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('PRIVATE_JWT_SECRET', 'test-jwt-secret')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')

from app import create_app, db
from app.models import Client, Tenant, Workspace, UsuarioPBI, Report, User


class DeleteReportCsrfTest(unittest.TestCase):

    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.http = self.app.test_client()

        with self.app.app_context():
            db.drop_all()
            db.create_all()
            client = Client(id=1, name='c', client_id='cid')
            client.set_secret('s')
            db.session.add(client)
            db.session.flush()
            db.session.add(Tenant(id=1, name='t', tenant_id='tid', client_id_fk=1))
            db.session.flush()
            db.session.add(Workspace(id=1, name='w', workspace_id='wid', tenant_id_fk=1))
            usuario = UsuarioPBI(id=1, nombre='u', username='u@example.com')
            usuario.set_password('p')
            db.session.add(usuario)
            db.session.flush()
            db.session.add(Report(
                id=1, name='R', report_id=str(uuid.uuid4()),
                workspace_id_fk=1, usuario_pbi_id=1,
            ))
            admin = User(id=1, username='admin', is_admin=True)
            admin.set_password('pw')
            db.session.add(admin)
            db.session.commit()

        login_page = self.http.get('/login').get_data(as_text=True)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', login_page)
        token = token or re.search(r'value="([^"]+)"[^>]*name="csrf_token"', login_page)
        resp = self.http.post(
            '/login',
            data={'username': 'admin', 'password': 'pw', 'csrf_token': token.group(1)},
        )
        self.assertEqual(resp.status_code, 302)

        self.detail_html = self.http.get('/reports/1/detail').get_data(as_text=True)
        self.csrf_token = re.search(
            r'<meta name="csrf-token" content="([^"]+)"', self.detail_html
        ).group(1)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def _report_exists(self):
        with self.app.app_context():
            return db.session.get(Report, 1) is not None

    def test_delete_without_token_is_rejected(self):
        resp = self.http.post('/reports/1/delete')

        self.assertEqual(resp.status_code, 400)
        self.assertTrue(self._report_exists())

    def test_delete_with_token_succeeds(self):
        resp = self.http.post('/reports/1/delete', data={'csrf_token': self.csrf_token})

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self._report_exists())

    def test_layout_adds_token_to_forms_submitted_from_javascript(self):
        """The delete button's on-the-fly form needs the token added at submit() time."""
        self.assertIn('HTMLFormElement.prototype.submit', self.detail_html)
        self.assertIn('ensureCsrfToken(this)', self.detail_html)
        self.assertIn("createElement('form')", self.detail_html)


if __name__ == '__main__':
    unittest.main()
