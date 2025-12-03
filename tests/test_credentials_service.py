"""
Unit tests for credentials service.
"""
import unittest
from app.services.credentials_service import (
    generate_client_id,
    generate_client_secret,
    hash_client_secret,
    verify_client_secret
)


class CredentialsServiceTestCase(unittest.TestCase):
    """Test case for credentials service functionality."""
    
    def test_generate_client_id(self):
        """Test client ID generation."""
        client_id1 = generate_client_id()
        client_id2 = generate_client_id()
        
        # IDs should be unique
        self.assertNotEqual(client_id1, client_id2)
        
        # IDs should be strings
        self.assertIsInstance(client_id1, str)
        
        # IDs should be URL-safe
        self.assertTrue(client_id1.replace('-', '').replace('_', '').isalnum())
    
    def test_generate_client_secret(self):
        """Test client secret generation."""
        secret1 = generate_client_secret()
        secret2 = generate_client_secret()
        
        # Secrets should be unique
        self.assertNotEqual(secret1, secret2)
        
        # Secrets should be strings
        self.assertIsInstance(secret1, str)
        
        # Secrets should be URL-safe
        self.assertTrue(secret1.replace('-', '').replace('_', '').isalnum())
        
        # Secrets should be long enough (at least 32 chars)
        self.assertGreaterEqual(len(secret1), 32)
    
    def test_hash_client_secret(self):
        """Test client secret hashing."""
        secret = "test-secret-123"
        hash1 = hash_client_secret(secret)
        hash2 = hash_client_secret(secret)
        
        # Hashes should be different (bcrypt includes salt)
        self.assertNotEqual(hash1, hash2)
        
        # Hash should not be the plain secret
        self.assertNotEqual(hash1, secret)
        
        # Hash should be a string
        self.assertIsInstance(hash1, str)
    
    def test_verify_client_secret_correct(self):
        """Test verifying correct client secret."""
        secret = "test-secret-123"
        hashed = hash_client_secret(secret)
        
        # Correct secret should verify
        self.assertTrue(verify_client_secret(secret, hashed))
    
    def test_verify_client_secret_incorrect(self):
        """Test verifying incorrect client secret."""
        secret = "test-secret-123"
        hashed = hash_client_secret(secret)
        
        # Incorrect secret should not verify
        self.assertFalse(verify_client_secret("wrong-secret", hashed))
    
    def test_verify_client_secret_case_sensitive(self):
        """Test that secret verification is case-sensitive."""
        secret = "TestSecret123"
        hashed = hash_client_secret(secret)
        
        # Different case should not verify
        self.assertFalse(verify_client_secret("testsecret123", hashed))


if __name__ == '__main__':
    unittest.main()
