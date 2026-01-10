"""
Private client API routes for authentication and report access.
"""
import logging
from flask import Blueprint, request, jsonify
import jwt as pyjwt

from app import db
from app.models import Empresa, ReportConfig
from app.services.credentials_service import verify_client_secret
from app.services.jwt_service import generate_token, verify_token, extract_token_from_header
from app.utils.powerbi import get_embed_for_config

bp = Blueprint('private', __name__, url_prefix='/private')


@bp.route('/login', methods=['POST'])
def login():
    """
    Authenticate an empresa and return a JWT token.
    
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
    
    # Find the empresa
    empresa = Empresa.query.filter_by(client_id=client_id).first()
    
    if not empresa:
        return jsonify({'error': 'Invalid credentials'}), 401
    
    # Check if empresa is active
    if not empresa.estado_activo:
        return jsonify({'error': 'Client is inactive'}), 403
    
    # Verify the client secret
    if not verify_client_secret(client_secret, empresa.client_secret_hash):
        return jsonify({'error': 'Invalid credentials'}), 401
    
    # Generate JWT token
    token_data = generate_token(empresa.id, empresa.client_id)
    
    logging.info(f"Empresa authenticated: {empresa.nombre} (ID: {empresa.id})")
    
    return jsonify(token_data), 200


@bp.route('/reports', methods=['GET'])
def list_reports():
    """
    Get list of report IDs accessible by the authenticated empresa.
    
    Headers:
        Authorization: Bearer <token>
    
    Response 200:
        {
            "empresa_id": integer,
            "empresa_nombre": "string",
            "reports": [
                {
                    "id": integer,
                    "name": "string"
                },
                ...
            ]
        }
    """
    # Extract and verify JWT token
    auth_header = request.headers.get('Authorization')
    token = extract_token_from_header(auth_header)
    
    if not token:
        return jsonify({'error': 'Authorization header missing or invalid'}), 401
    
    try:
        payload = verify_token(token)
        empresa_id = int(payload.get('sub'))
    except pyjwt.ExpiredSignatureError:
        logging.warning(f"Expired token used in /private/reports")
        return jsonify({'error': 'Token has expired'}), 401
    except pyjwt.InvalidTokenError as e:
        logging.warning(f"Invalid token in /private/reports: {str(e)}")
        return jsonify({'error': f'Invalid token: {str(e)}'}), 401
    
    # Get empresa
    empresa = Empresa.query.get(empresa_id)
    if not empresa:
        return jsonify({'error': 'Empresa not found'}), 404
    
    # Get all report configs associated with this empresa where es_privado=True
    report_configs = ReportConfig.query.join(
        ReportConfig.empresas
    ).filter(
        Empresa.id == empresa_id,
        ReportConfig.es_privado == True
    ).all()
    
    reports_data = []
    for config in report_configs:
        reports_data.append({
            'id': config.id,
            'name': config.report.name
        })
    
    response_data = {
        'empresa_id': empresa.id,
        'empresa_nombre': empresa.nombre,
        'reports': reports_data
    }
    
    logging.info(f"Report list retrieved for empresa ID {empresa_id}: {len(reports_data)} reports")
    return jsonify(response_data), 200


@bp.route('/report-config', methods=['GET'])
def report_config():
    """
    Get report configuration for an empresa.
    
    Headers:
        Authorization: Bearer <token>
    
    Query Parameters or Request body:
        config_id: integer (required)
    
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
        empresa_id = int(payload.get('sub'))
    except pyjwt.ExpiredSignatureError:
        logging.warning(f"Expired token used in /private/report-config")
        return jsonify({'error': 'Token has expired'}), 401
    except pyjwt.InvalidSignatureError as e:
        logging.warning(f"Invalid token signature in /private/report-config: {str(e)}. Token may have been generated with a different PRIVATE_JWT_SECRET.")
        return jsonify({'error': 'Invalid token signature. Ensure PRIVATE_JWT_SECRET is consistent.'}), 401
    except pyjwt.DecodeError as e:
        logging.warning(f"Token decode error in /private/report-config: {str(e)}")
        return jsonify({'error': f'Token format error: {str(e)}'}), 401
    except pyjwt.InvalidTokenError as e:
        logging.warning(f"Invalid token in /private/report-config: {str(e)}")
        return jsonify({'error': f'Invalid token: {str(e)}'}), 401
    
    # Prefer query param; use body JSON as fallback
    config_id = request.args.get('config_id', type=int)
    if config_id is None:
        data = request.get_json(silent=True) or {}
        config_id = data.get('config_id')

    if not config_id:
        return jsonify({'error': 'config_id is required (query ?config_id= or body JSON)'}), 400

    # Get configuration
    config = ReportConfig.query.get(config_id)
    if not config:
        return jsonify({'error': 'Configuration not found'}), 404
    if not config.es_privado:
        return jsonify({'error': 'Configuration is not private'}), 403
    
    # Check if this empresa has access to this config (many-to-many relationship)
    empresa = Empresa.query.get(empresa_id)
    if not empresa or config not in empresa.report_configs:
        return jsonify({'error': 'Configuration does not belong to this empresa'}), 403

    # Generate embed token and respond
    try:
        embed_token, embed_url, report_id = get_embed_for_config(config)
        response_data = {
            'embedUrl': embed_url,
            'reportId': report_id,
            'accessToken': embed_token,
            'workspaceId': config.workspace.workspace_id,
        }
        logging.info(f"Report config retrieved for empresa ID {empresa_id}: {config.name}")
        return jsonify(response_data), 200
    except Exception as e:
        logging.error(f"Error generating embed token for config {config_id}: {e}")
        return jsonify({'error': 'Error generating report embed token'}), 500