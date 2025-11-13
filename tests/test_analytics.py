"""
Unit tests for analytics functionality.
"""
import unittest
from datetime import datetime, timedelta
from app.utils.analytics import (
    anonymize_ip,
    is_bot,
    parse_user_agent,
    generate_visitor_id
)


class AnalyticsTestCase(unittest.TestCase):
    """Test case for analytics functionality."""
    
    def setUp(self):
        """Set up test fixtures."""
        pass
    
    def tearDown(self):
        """Tear down test fixtures."""
        pass
    
    def test_anonymize_ip(self):
        """Test IP anonymization."""
        ip1 = '192.168.1.1'
        ip2 = '192.168.1.2'
        
        hash1 = anonymize_ip(ip1)
        hash2 = anonymize_ip(ip2)
        
        # Hashes should be different
        self.assertNotEqual(hash1, hash2)
        
        # Same IP should produce same hash
        self.assertEqual(hash1, anonymize_ip(ip1))
        
        # Hash should be 64 characters (SHA-256 hex)
        self.assertEqual(len(hash1), 64)
    
    def test_is_bot(self):
        """Test bot detection."""
        # Known bots
        self.assertTrue(is_bot('Mozilla/5.0 (compatible; Googlebot/2.1)'))
        self.assertTrue(is_bot('Mozilla/5.0 (compatible; bingbot/2.0)'))
        self.assertTrue(is_bot('facebookexternalhit/1.1'))
        self.assertTrue(is_bot('curl/7.64.1'))
        
        # Real browsers
        self.assertFalse(is_bot('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'))
        self.assertFalse(is_bot('Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X)'))
        
        # Empty user agent
        self.assertTrue(is_bot(''))
        self.assertTrue(is_bot(None))
    
    def test_parse_user_agent(self):
        """Test user agent parsing."""
        # Desktop browser
        ua_desktop = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        result = parse_user_agent(ua_desktop)
        self.assertEqual(result['device_type'], 'pc')
        self.assertIn('Chrome', result['browser'])
        self.assertIn('Windows', result['os'])
        
        # Mobile browser
        ua_mobile = 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15'
        result = parse_user_agent(ua_mobile)
        self.assertEqual(result['device_type'], 'mobile')
        
        # Empty user agent
        result = parse_user_agent('')
        self.assertEqual(result['device_type'], 'unknown')
        self.assertEqual(result['browser'], 'unknown')
    
    def test_generate_visitor_id(self):
        """Test visitor ID generation."""
        id1 = generate_visitor_id()
        id2 = generate_visitor_id()
        
        # IDs should be unique
        self.assertNotEqual(id1, id2)
        
        # IDs should be valid UUIDs (36 characters with dashes)
        self.assertEqual(len(id1), 36)
        self.assertEqual(id1.count('-'), 4)


if __name__ == '__main__':
    unittest.main()
