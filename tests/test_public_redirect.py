"""Tests for public central-ticket temporary redirect."""
import os
import unittest

os.environ.setdefault('FERNET_KEY', 'o9eBKpiFgJRzgZNyBbFaQ8YeHImGZ5QpFnLn4EP9nj0=')
os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')

from app import create_app


class PublicRedirectTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        self.client = self.app.test_client()

    def test_central_ticket_redirects_temporarily(self):
        response = self.client.get('/p/central-ticket')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers.get('Location'),
            'https://app.powerbi.com/links/CZwaplCsbc?ctid=94fb2600-60af-4872-80d9-727cacb45759&pbi_source=linkShare&bookmarkGuid=20cd48f9-0fec-4308-9276-228fc85e92cb'
        )


if __name__ == '__main__':
    unittest.main()
