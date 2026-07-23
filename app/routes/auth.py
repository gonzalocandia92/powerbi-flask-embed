"""
Authentication routes for login and logout.
"""
import os
import secrets
import requests
from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, login_required

from app import db
from app.models import User
from app.forms import LoginForm
from app.utils.decorators import retry_on_db_error

bp = Blueprint('auth', __name__)

GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'


@bp.route('/login', methods=['GET', 'POST'])
@retry_on_db_error(max_retries=3, delay=1)
def login():
    """Handle user login."""
    form = LoginForm()
    
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        
        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            return redirect(url_for('main.index'))
        
        flash('Usuario o contraseña inválidos', 'danger')
    
    google_enabled = bool(os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET'))
    return render_template('login.html', form=form, google_enabled=google_enabled)


@bp.route('/login/google')
def google_login():
    """Initiate Google OAuth login flow."""
    client_id = os.getenv('GOOGLE_CLIENT_ID')
    if not client_id or not os.getenv('GOOGLE_CLIENT_SECRET'):
        flash('El login con Google no está configurado', 'danger')
        return redirect(url_for('auth.login'))

    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state

    redirect_uri = url_for('auth.google_callback', _external=True)
    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': 'openid email',
        'state': state,
        'access_type': 'online',
    }
    auth_url = GOOGLE_AUTH_URL + '?' + '&'.join(f'{k}={v}' for k, v in params.items())
    return redirect(auth_url)


@bp.route('/login/google/callback')
@retry_on_db_error(max_retries=3, delay=1)
def google_callback():
    """Handle Google OAuth callback."""
    # CSRF protection
    state = request.args.get('state')
    if not state or state != session.pop('oauth_state', None):
        flash('Solicitud de login inválida', 'danger')
        return redirect(url_for('auth.login'))

    error = request.args.get('error')
    if error:
        flash('Login con Google cancelado', 'warning')
        return redirect(url_for('auth.login'))

    code = request.args.get('code')
    if not code:
        flash('No se recibió el código de autorización de Google', 'danger')
        return redirect(url_for('auth.login'))

    client_id = os.getenv('GOOGLE_CLIENT_ID')
    client_secret = os.getenv('GOOGLE_CLIENT_SECRET')
    redirect_uri = url_for('auth.google_callback', _external=True)

    # Exchange authorization code for access token
    token_resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code',
        },
        timeout=10,
    )

    if not token_resp.ok:
        flash('Error al obtener el token de Google', 'danger')
        return redirect(url_for('auth.login'))

    access_token = token_resp.json().get('access_token')

    # Fetch user info from Google
    userinfo_resp = requests.get(
        GOOGLE_USERINFO_URL,
        headers={'Authorization': f'******'},
        timeout=10,
    )

    if not userinfo_resp.ok:
        flash('Error al obtener los datos del usuario de Google', 'danger')
        return redirect(url_for('auth.login'))

    google_email = userinfo_resp.json().get('email')
    if not google_email:
        flash('No se pudo obtener el email desde Google', 'danger')
        return redirect(url_for('auth.login'))

    # Only allow login if the email is already registered
    user = User.query.filter_by(email=google_email).first()
    if user is None:
        flash('Tu cuenta de Google no está registrada en el sistema. Contacta al administrador.', 'danger')
        return redirect(url_for('auth.login'))

    if not user.is_active:
        flash('Tu cuenta está desactivada. Contacta al administrador.', 'danger')
        return redirect(url_for('auth.login'))

    login_user(user)
    return redirect(url_for('main.index'))


@bp.route('/logout')
@login_required
def logout():
    """Handle user logout."""
    logout_user()
    return redirect(url_for('auth.login'))
