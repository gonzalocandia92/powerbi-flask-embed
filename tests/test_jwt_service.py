"""
Unit tests for JWT service.
"""
import unittest
import time
import jwt as pyjwt
from app.services.jwt_service import (
    generate_token,
    verify_token,
    extract_token_from_header
)


class JWTServiceTestCase(unittest.TestCase):
    """Test case for JWT service functionality."""
    
    def test_generate_token(self):
        """Test JWT token generation."""
        cliente_id = 123
        client_id = "test-client-id"
        
        result = generate_token(cliente_id, client_id)
        
        # Should return dict with expected keys
        self.assertIsInstance(result, dict)
        self.assertIn('access_token', result)
        self.assertIn('token_type', result)
        self.assertIn('expires_in', result)
        
        # Token type should be Bearer
        self.assertEqual(result['token_type'], 'Bearer')
        
        # Expires_in should be positive integer
        self.assertIsInstance(result['expires_in'], int)
        self.assertGreater(result['expires_in'], 0)
        
        # Access token should be a string
        self.assertIsInstance(result['access_token'], str)
    
    def test_verify_token_valid(self):
        """Test verifying a valid JWT token."""
        cliente_id = 123
        client_id = "test-client-id"
        
        token_data = generate_token(cliente_id, client_id)
        token = token_data['access_token']
        
        # Verify the token
        payload = verify_token(token)
        
        # Check payload contents
        self.assertEqual(payload['sub'], cliente_id)
        self.assertEqual(payload['client_id'], client_id)
        self.assertIn('iat', payload)
        self.assertIn('exp', payload)
    
    def test_verify_token_invalid(self):
        """Test verifying an invalid JWT token."""
        invalid_token = "invalid.token.here"
        
        with self.assertRaises(pyjwt.InvalidTokenError):
            verify_token(invalid_token)
    
    def test_verify_token_tampered(self):
        """Test verifying a tampered JWT token."""
        cliente_id = 123
        client_id = "test-client-id"
        
        token_data = generate_token(cliente_id, client_id)
        token = token_data['access_token']
        
        # Tamper with the token
        parts = token.split('.')
        if len(parts) == 3:
            # Change a character in the payload
            tampered_token = parts[0] + '.' + parts[1] + 'X.' + parts[2]
            
            with self.assertRaises(pyjwt.InvalidTokenError):
                verify_token(tampered_token)
    
    def test_extract_token_from_header_valid(self):
        """Test extracting token from valid Authorization header."""
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test.token"
        header = f"Bearer {token}"
        
        extracted = extract_token_from_header(header)
        
        self.assertEqual(extracted, token)
    
    def test_extract_token_from_header_no_bearer(self):
        """Test extracting token from header without Bearer prefix."""
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test.token"
        
        extracted = extract_token_from_header(token)
        
        self.assertIsNone(extracted)
    
    def test_extract_token_from_header_empty(self):
        """Test extracting token from empty header."""
        extracted = extract_token_from_header("")
        
        self.assertIsNone(extracted)
    
    def test_extract_token_from_header_none(self):
        """Test extracting token from None header."""
        extracted = extract_token_from_header(None)
        
        self.assertIsNone(extracted)
    
    def test_extract_token_from_header_case_insensitive(self):
        """Test extracting token with different case Bearer."""
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test.token"
        header = f"bearer {token}"
        
        extracted = extract_token_from_header(header)
        
        self.assertEqual(extracted, token)
    
    def test_token_contains_all_claims(self):
        """Test that generated token contains all required claims."""
        cliente_id = 456
        client_id = "another-client"
        
        token_data = generate_token(cliente_id, client_id)
        payload = verify_token(token_data['access_token'])
        
        # Check all required claims are present
        self.assertIn('sub', payload)
        self.assertIn('client_id', payload)
        self.assertIn('iat', payload)
        self.assertIn('exp', payload)
        
        # Check expiration is in the future
        self.assertGreater(payload['exp'], payload['iat'])


if __name__ == '__main__':
    unittest.main()
