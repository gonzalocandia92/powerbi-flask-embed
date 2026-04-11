"""
Private client API routes for authentication and report access.
"""
import logging
from flask import Blueprint, request, jsonify
import jwt as pyjwt

from app import db
from app.models import Empresa, Report
from app.services.credentials_service import verify_client_secret
from app.services.jwt_service import generate_token, verify_token, extract_token_from_header
from app.utils.powerbi import get_embed_for_report

bp = Blueprint('private', __name__, url_prefix='/private')


@bp.route('/login', methods=['POST'])
def login():
    """Authenticate an empresa and return a JWT token."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body must be JSON'}), 400
    
    client_id = data.get('client_id')
    client_secret = data.get('client_secret')
    if not client_id or not client_secret:
        return jsonify({'error': 'client_id and client_secret are required'}), 400
    
    empresa = Empresa.query.filter_by(client_id=client_id).first()
    if not empresa:
        return jsonify({'error': 'Invalid credentials'}), 401
    if not empresa.estado_activo:
        return jsonify({'error': 'Client is inactive'}), 403
    if not verify_client_secret(client_secret, empresa.client_secret_hash):
        return jsonify({'error': 'Invalid credentials'}), 401
    
    token_data = generate_token(empresa.id, empresa.client_id)
    logging.info(f"Empresa authenticated: {empresa.nombre} (ID: {empresa.id})")
    return jsonify(token_data), 200


@bp.route('/reports', methods=['GET'])
def list_reports():
    """Get list of reports accessible by the authenticated empresa."""
    auth_header = request.headers.get('Authorization')
    token = extract_token_from_header(auth_header)
    if not token:
        return jsonify({'error': 'Authorization header missing or invalid'}), 401
    
    try:
        payload = verify_token(token)
        empresa_id = int(payload.get('sub'))
    except pyjwt.ExpiredSignatureError:
        return jsonify({'error': 'Token has expired'}), 401
    except pyjwt.InvalidTokenError as e:
        return jsonify({'error': f'Invalid token: {str(e)}'}), 401
    
    empresa = db.session.get(Empresa, empresa_id)
    if not empresa:
        return jsonify({'error': 'Empresa not found'}), 404
    
    # Get reports associated with this empresa where es_privado=True
    private_reports = Report.query.join(Report.empresas).filter(
        Empresa.id == empresa_id,
        Report.es_privado == True
    ).all()
    
    reports_data = [{'id': r.id, 'name': r.name} for r in private_reports]
    
    return jsonify({
        'empresa_id': empresa.id,
        'empresa_nombre': empresa.nombre,
        'reports': reports_data
    }), 200


@bp.route('/report-config', methods=['GET'])
def report_config():
    """Get report embed configuration for an empresa."""
    auth_header = request.headers.get('Authorization')
    token = extract_token_from_header(auth_header)
    if not token:
        return jsonify({'error': 'Authorization header missing or invalid'}), 401
    
    try:
        payload = verify_token(token)
        empresa_id = int(payload.get('sub'))
    except pyjwt.ExpiredSignatureError:
        return jsonify({'error': 'Token has expired'}), 401
    except pyjwt.InvalidSignatureError as e:
        return jsonify({'error': 'Invalid token signature.'}), 401
    except pyjwt.DecodeError as e:
        return jsonify({'error': f'Token format error: {str(e)}'}), 401
    except pyjwt.InvalidTokenError as e:
        return jsonify({'error': f'Invalid token: {str(e)}'}), 401
    
    # Accept report_id as query param or body
    report_id = request.args.get('report_id', type=int)
    if report_id is None:
        # Also accept config_id for backward compatibility
        report_id = request.args.get('config_id', type=int)
    if report_id is None:
        data = request.get_json(silent=True) or {}
        report_id = data.get('report_id') or data.get('config_id')
    
    if not report_id:
        return jsonify({'error': 'report_id is required (query ?report_id= or body JSON)'}), 400
    
    report = db.session.get(Report, report_id)
    if not report:
        return jsonify({'error': 'Report not found'}), 404
    if not report.es_privado:
        return jsonify({'error': 'Report is not private'}), 403
    
    empresa = db.session.get(Empresa, empresa_id)
    if not empresa or report not in empresa.reports:
        return jsonify({'error': 'Report does not belong to this empresa'}), 403
    
    try:
        embed_token, embed_url, rid = get_embed_for_report(report)
        return jsonify({
            'embedUrl': embed_url,
            'reportId': rid,
            'accessToken': embed_token,
            'workspaceId': report.workspace.workspace_id,
        }), 200
    except Exception as e:
        logging.error(f"Error generating embed token for report {report_id}: {e}")
        return jsonify({'error': 'Error generating report embed token'}), 500