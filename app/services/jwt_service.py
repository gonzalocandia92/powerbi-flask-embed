"""
Service for generating and validating JWT tokens for private client authentication.
"""
import os
import jwt
from datetime import datetime, timedelta


# Get configuration from environment
JWT_SECRET = os.getenv('PRIVATE_JWT_SECRET', os.getenv('SECRET_KEY', 'default-jwt-secret'))
JWT_ALGORITHM = 'HS256'
JWT_EXPIRATION = int(os.getenv('JWT_EXPIRATION', 3600))  # Default: 1 hour in seconds


def generate_token(cliente_privado_id, client_id):
    """
    Generate a JWT token for a private client.
    
    Args:
        cliente_privado_id (int): ID of the private client
        client_id (str): Client ID for the private client
        
    Returns:
        dict: Dictionary with access_token, token_type, and expires_in
    """
    now = datetime.utcnow()
    payload = {
        'sub': cliente_privado_id,
        'client_id': client_id,
        'iat': now,
        'exp': now + timedelta(seconds=JWT_EXPIRATION)
    }
    
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    
    return {
        'access_token': token,
        'token_type': 'Bearer',
        'expires_in': JWT_EXPIRATION
    }


def verify_token(token):
    """
    Verify and decode a JWT token.
    
    Args:
        token (str): JWT token to verify
        
    Returns:
        dict: Decoded token payload if valid
        
    Raises:
        jwt.ExpiredSignatureError: If token is expired
        jwt.InvalidSignatureError: If token signature is invalid
        jwt.DecodeError: If token format is invalid
        jwt.InvalidTokenError: If token is invalid for other reasons
    """
    # Let exceptions propagate naturally - don't catch and re-raise
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    return payload


def extract_token_from_header(authorization_header):
    """
    Extract JWT token from Authorization header.
    
    Args:
        authorization_header (str): Authorization header value (e.g., "Bearer <token>")
        
    Returns:
        str: Extracted token or None
    """
    if not authorization_header:
        return None
    
    parts = authorization_header.split()
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return None
    
    return parts[1]
