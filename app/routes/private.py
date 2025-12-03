"""
Private client API routes for authentication and report access.
"""
import logging
from flask import Blueprint, request, jsonify
import jwt as pyjwt

from app import db
from app.models import ClientePrivado, ReportConfig
from app.services.credentials_service import verify_client_secret
from app.services.jwt_service import generate_token, verify_token, extract_token_from_header
from app.utils.powerbi import get_embed_for_config

bp = Blueprint('private', __name__, url_prefix='/private')


@bp.route('/login', methods=['POST'])
def login():
    """
    Authenticate a private client and return a JWT token.
    
    Request body:
        {
            "client_id": "string",
            "client_secret": "string"
        }
    
    Response 200:
        {
            "access_token": "string",
            "token_type": "Bearer",
            "expires_in": integer
        }
    """
    data = request.get_json(silent=True)
    
    if not data:
        return jsonify({'error': 'Request body must be JSON'}), 400
    
    client_id = data.get('client_id')
    client_secret = data.get('client_secret')
    
    if not client_id or not client_secret:
        return jsonify({'error': 'client_id and client_secret are required'}), 400
    
    # Find the private client
    cliente = ClientePrivado.query.filter_by(client_id=client_id).first()
    
    if not cliente:
        return jsonify({'error': 'Invalid credentials'}), 401
    
    # Check if client is active
    if not cliente.estado_activo:
        return jsonify({'error': 'Client is inactive'}), 403
    
    # Verify the client secret
    if not verify_client_secret(client_secret, cliente.client_secret_hash):
        return jsonify({'error': 'Invalid credentials'}), 401
    
    # Generate JWT token
    token_data = generate_token(cliente.id, cliente.client_id)
    
    logging.info(f"Private client authenticated: {cliente.nombre} (ID: {cliente.id})")
    
    return jsonify(token_data), 200


@bp.route('/report-config', methods=['POST'])
def report_config():
    """
    Get report configuration for a private client.
    
    Headers:
        Authorization: Bearer <token>
    
    Request body:
        {
            "config_id": "integer"
        }
    
    Response 200:
        {
            "embedUrl": "string",
            "reportId": "string",
            "workspaceId": "string",
            "datasetId": "string",
            "accessToken": "string"
        }
    """
    # Extract and verify JWT token
    auth_header = request.headers.get('Authorization')
    token = extract_token_from_header(auth_header)
    
    if not token:
        return jsonify({'error': 'Authorization header missing or invalid'}), 401
    
    try:
        payload = verify_token(token)
        cliente_privado_id = payload.get('sub')
    except pyjwt.ExpiredSignatureError:
        return jsonify({'error': 'Token has expired'}), 401
    except pyjwt.InvalidTokenError:
        return jsonify({'error': 'Invalid token'}), 401
    
    # Get request data
    data = request.get_json(silent=True)
    
    if not data:
        return jsonify({'error': 'Request body must be JSON'}), 400
    
    config_id = data.get('config_id')
    
    if not config_id:
        return jsonify({'error': 'config_id is required'}), 400
    
    # Find the report configuration
    config = ReportConfig.query.get(config_id)
    
    if not config:
        return jsonify({'error': 'Configuration not found'}), 404
    
    # Verify that the configuration is private
    if config.tipo_privacidad != 'privado':
        return jsonify({'error': 'Configuration is not private'}), 403
    
    # Verify that the configuration belongs to this client
    if config.cliente_privado_id != cliente_privado_id:
        return jsonify({'error': 'Configuration does not belong to this client'}), 403
    
    # Generate embed token and return configuration
    try:
        embed_token, embed_url, report_id = get_embed_for_config(config)
        
        response_data = {
            'embedUrl': embed_url,
            'reportId': report_id,
            'accessToken': embed_token,
            'workspaceId': config.workspace.workspace_id,
            'datasetId': config.workspace.workspace_id  # Can be updated if dataset ID is tracked separately
        }
        
        logging.info(f"Report config retrieved for private client ID {cliente_privado_id}: {config.name}")
        
        return jsonify(response_data), 200
        
    except Exception as e:
        logging.error(f"Error generating embed token for config {config_id}: {e}")
        return jsonify({'error': 'Error generating report embed token'}), 500
