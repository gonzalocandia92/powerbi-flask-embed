"""
Authentication routes for login and logout.
"""
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required

from app import db
from app.models import User
from app.forms import LoginForm
from app.utils.decorators import retry_on_db_error

bp = Blueprint('auth', __name__)


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
    
    return render_template('login.html', form=form)


@bp.route('/logout')
@login_required
def logout():
    """Handle user logout."""
    logout_user()
    return redirect(url_for('auth.login'))
