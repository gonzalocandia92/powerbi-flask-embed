"""
Integration tests for analytics functionality.
"""
import unittest
from datetime import datetime, timedelta
from app import create_app, db
from app.models import Visit, PublicLink, ReportConfig, User
from app.utils.analytics import track_visit, generate_visitor_id
from flask import Flask
from werkzeug.test import EnvironBuilder


class AnalyticsIntegrationTestCase(unittest.TestCase):
    """Test case for analytics integration."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Use existing migrated database for tests
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        
        with self.app.app_context():
            # Clear visits table for clean tests
            Visit.query.delete()
            db.session.commit()
            
            # Create test user if doesn't exist
            user = User.query.filter_by(username='testadmin').first()
            if not user:
                user = User(username='testadmin', is_admin=True)
                user.set_password('testpass')
                db.session.add(user)
                db.session.commit()
    
    def tearDown(self):
        """Tear down test fixtures."""
        with self.app.app_context():
            # Clean up test visits
            Visit.query.delete()
            db.session.commit()
            db.session.remove()
    
    def test_track_visit_creates_record(self):
        """Test that tracking a visit creates a database record."""
        with self.app.app_context():
            # Create a mock request
            with self.app.test_request_context(
                '/',
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                },
                environ_base={'REMOTE_ADDR': '192.168.1.1'}
            ):
                from flask import request
                visitor_id = generate_visitor_id()
                
                # Track visit
                visit = track_visit('test-link', request, visitor_id)
                
                # Verify visit was created
                self.assertIsNotNone(visit)
                self.assertEqual(visit.link_slug, 'test-link')
                self.assertEqual(visit.visitor_id, visitor_id)
                self.assertFalse(visit.is_bot)
                
                # Verify in database
                db_visit = Visit.query.filter_by(visitor_id=visitor_id).first()
                self.assertIsNotNone(db_visit)
                self.assertEqual(db_visit.link_slug, 'test-link')
    
    def test_bot_visits_detected(self):
        """Test that bot visits are properly detected."""
        with self.app.app_context():
            with self.app.test_request_context(
                '/',
                headers={
                    'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1)',
                },
                environ_base={'REMOTE_ADDR': '192.168.1.1'}
            ):
                from flask import request
                visitor_id = generate_visitor_id()
                
                visit = track_visit('test-link', request, visitor_id)
                
                self.assertIsNotNone(visit)
                self.assertTrue(visit.is_bot)
    
    def test_analytics_api_endpoint(self):
        """Test the analytics API endpoint."""
        with self.app.app_context():
            # Create some test visits
            for i in range(5):
                visit = Visit(
                    link_slug='test-link',
                    timestamp=datetime.utcnow() - timedelta(days=i),
                    visitor_id=generate_visitor_id(),
                    is_bot=False
                )
                db.session.add(visit)
            db.session.commit()
            
            # Login
            response = self.client.post('/auth/login', data={
                'username': 'testadmin',
                'password': 'testpass'
            }, follow_redirects=True)
            
            # Call API
            response = self.client.get('/analytics/api/stats?link_slug=test-link&days=7')
            
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            
            self.assertTrue(data['success'])
            self.assertEqual(data['data']['overview']['total_visits'], 5)


if __name__ == '__main__':
    unittest.main()
