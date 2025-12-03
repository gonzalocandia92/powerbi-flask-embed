"""
Service for generating and managing private client credentials.
"""
import secrets
from werkzeug.security import generate_password_hash, check_password_hash


def generate_client_id():
    """
    Generate a unique client_id for private clients.
    
    Returns:
        str: URL-safe random string (24 characters)
    """
    return secrets.token_urlsafe(24)


def generate_client_secret():
    """
    Generate a secure client_secret for private clients.
    
    Returns:
        str: URL-safe random string (32 characters)
    """
    return secrets.token_urlsafe(32)


def hash_client_secret(client_secret):
    """
    Hash a client secret for secure storage.
    
    Args:
        client_secret (str): Plain text client secret
        
    Returns:
        str: Hashed client secret
    """
    return generate_password_hash(client_secret)


def verify_client_secret(client_secret, hashed_secret):
    """
    Verify a client secret against its hash.
    
    Args:
        client_secret (str): Plain text client secret to verify
        hashed_secret (str): Hashed secret to check against
        
    Returns:
        bool: True if the secret matches, False otherwise
    """
    return check_password_hash(hashed_secret, client_secret)
