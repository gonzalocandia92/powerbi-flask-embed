"""
Routes for managing futuras empresas (pending company approvals).
"""
import logging
import json
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user

from app import db
from app.models import FuturaEmpresa, Empresa
from app.forms import FuturaEmpresaForm
from app.services.credentials_service import generate_client_id, generate_client_secret, hash_client_secret
from app.utils.decorators import retry_on_db_error

bp = Blueprint('futuras_empresas', __name__, url_prefix='/admin/futuras-empresas')


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def list():
    """Display list of all pending futuras empresas."""
    futuras = FuturaEmpresa.query.filter_by(estado='pendiente').order_by(FuturaEmpresa.fecha_recepcion.desc()).all()
    procesadas = FuturaEmpresa.query.filter(FuturaEmpresa.estado.in_(['confirmada', 'rechazada'])).order_by(FuturaEmpresa.fecha_procesamiento.desc()).limit(20).all()
    
    return render_template(
        'admin/futuras_empresas/list.html',
        futuras=futuras,
        procesadas=procesadas,
        title='Futuras Empresas'
    )


@bp.route('/<int:futura_id>/view')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def view(futura_id):
    """View details of a futura empresa."""
    futura = FuturaEmpresa.query.get_or_404(futura_id)
    
    # Parse additional data if JSON
    datos_adicionales = {}
    if futura.datos_adicionales:
        try:
            datos_adicionales = json.loads(futura.datos_adicionales)
        except json.JSONDecodeError:
            datos_adicionales = {'raw': futura.datos_adicionales}
    
    return render_template(
        'admin/futuras_empresas/view.html',
        futura=futura,
        datos_adicionales=datos_adicionales,
        title=f'Futura Empresa: {futura.nombre}'
    )


@bp.route('/<int:futura_id>/confirm', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def confirm(futura_id):
    """Confirm a futura empresa and create an Empresa."""
    futura = FuturaEmpresa.query.get_or_404(futura_id)
    
    if futura.estado != 'pendiente':
        flash("Esta empresa ya fue procesada", "warning")
        return redirect(url_for('futuras_empresas.list'))
    
    # Check if empresa with same CUIT already exists
    if futura.cuit:
        existing = Empresa.query.filter_by(cuit=futura.cuit).first()
        if existing:
            flash(f"Ya existe una empresa con el CUIT {futura.cuit}: {existing.nombre}", "danger")
            return redirect(url_for('futuras_empresas.view', futura_id=futura_id))
    
    # Generate credentials
    client_id = generate_client_id()
    client_secret = generate_client_secret()
    
    # Create new empresa
    empresa = Empresa(
        nombre=futura.nombre,
        cuit=futura.cuit,
        client_id=client_id,
        client_secret_hash=hash_client_secret(client_secret),
        estado_activo=True
    )
    
    db.session.add(empresa)
    db.session.flush()  # Get empresa ID
    
    # Update futura empresa
    futura.estado = 'confirmada'
    futura.fecha_procesamiento = datetime.utcnow()
    futura.procesado_por_user_id = current_user.id
    futura.empresa_id = empresa.id
    futura.notas = request.form.get('notas', '')
    
    db.session.commit()
    
    logging.info(f"Futura empresa confirmed and created: {empresa.nombre} (ID: {empresa.id})")
    
    # Simulate POST to external system
    _simulate_external_notification(futura, empresa, 'confirmada')
    
    flash(f"Empresa confirmada y creada exitosamente: {empresa.nombre}", "success")
    
    return render_template(
        'admin/empresas/credentials.html',
        empresa=empresa,
        client_id=client_id,
        client_secret=client_secret,
        title='Credenciales de la Empresa',
        from_futura=True
    )


@bp.route('/<int:futura_id>/reject', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def reject(futura_id):
    """Reject a futura empresa."""
    futura = FuturaEmpresa.query.get_or_404(futura_id)
    
    if futura.estado != 'pendiente':
        flash("Esta empresa ya fue procesada", "warning")
        return redirect(url_for('futuras_empresas.list'))
    
    # Update futura empresa
    futura.estado = 'rechazada'
    futura.fecha_procesamiento = datetime.utcnow()
    futura.procesado_por_user_id = current_user.id
    futura.notas = request.form.get('notas', '')
    
    db.session.commit()
    
    logging.info(f"Futura empresa rejected: {futura.nombre} (ID: {futura.id})")
    
    # Simulate POST to external system
    _simulate_external_notification(futura, None, 'rechazada')
    
    flash(f"Empresa rechazada: {futura.nombre}", "info")
    
    return redirect(url_for('futuras_empresas.list'))


@bp.route('/simulate-fetch', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def simulate_fetch():
    """Simulate fetching empresas from external system."""
    # Simulate external GET endpoint response
    external_empresas = [
        {
            'external_id': f'EXT-{datetime.now().timestamp()}-001',
            'nombre': 'Empresa Demo SA',
            'cuit': '20-12345678-9',
            'email': 'contacto@empresademo.com',
            'telefono': '+54 11 1234-5678',
            'direccion': 'Av. Corrientes 1234, CABA',
            'datos_adicionales': json.dumps({
                'razon_social': 'Empresa Demo Sociedad Anónima',
                'tipo_empresa': 'SA',
                'sector': 'Tecnología'
            })
        },
        {
            'external_id': f'EXT-{datetime.now().timestamp()}-002',
            'nombre': 'Comercial Norte SRL',
            'cuit': '30-87654321-2',
            'email': 'info@comercialnorte.com',
            'telefono': '+54 11 8765-4321',
            'direccion': 'San Martín 567, Buenos Aires',
            'datos_adicionales': json.dumps({
                'razon_social': 'Comercial Norte Sociedad de Responsabilidad Limitada',
                'tipo_empresa': 'SRL',
                'sector': 'Comercio'
            })
        }
    ]
    
    # Add to database if not already exists
    added = 0
    for ext_emp in external_empresas:
        existing = FuturaEmpresa.query.filter_by(external_id=ext_emp['external_id']).first()
        if not existing:
            futura = FuturaEmpresa(**ext_emp)
            db.session.add(futura)
            added += 1
    
    db.session.commit()
    
    logging.info(f"Simulated fetch from external system: {added} new empresas added")
    flash(f"Simulación completada: {added} nuevas empresas obtenidas del sistema externo", "success")
    
    return redirect(url_for('futuras_empresas.list'))


def _simulate_external_notification(futura, empresa, estado):
    """
    Simulate POST notification to external system.
    In production, this would make an actual HTTP request to the external API.
    """
    payload = {
        'external_id': futura.external_id,
        'estado': estado,
        'internal_id': empresa.id if empresa else None,
        'client_id': empresa.client_id if empresa else None,
        'fecha_procesamiento': datetime.utcnow().isoformat()
    }
    
    # In production, uncomment and configure:
    # import requests
    # response = requests.post(
    #     'https://external-system.com/api/empresa-status',
    #     json=payload,
    #     headers={'Authorization': 'Bearer YOUR_TOKEN'}
    # )
    
    logging.info(f"Simulated POST to external system: {json.dumps(payload)}")
