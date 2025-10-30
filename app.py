import os
import uuid
import requests
import logging
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, redirect, url_for, request, flash, abort, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sqlalchemy.exc import OperationalError, DBAPIError

# Carga .env
load_dotenv()

# Configuración del logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('SQLALCHEMY_DATABASE_URI')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Configuración del pool de conexiones para evitar conexiones perdidas
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,                # Número de conexiones permanentes en el pool
    'pool_recycle': 3600,           # Reciclar conexiones después de 1 hora
    'pool_pre_ping': True,          # Verificar conexión antes de usarla
    'max_overflow': 20,             # Conexiones adicionales permitidas
    'pool_timeout': 30,             # Tiempo de espera para obtener una conexión
    'connect_args': {
        'connect_timeout': 10       # Timeout para establecer conexión inicial
    }
}

# Inicializa DB
db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Login manager
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

# Teardown para cerrar sesiones de base de datos después de cada request
@app.teardown_appcontext
def shutdown_session(exception=None):
    """
    Cierra la sesión de base de datos al final de cada request.
    Esto asegura que las conexiones se devuelvan al pool correctamente.
    """
    db.session.remove()

# Decorador para reintentar operaciones de base de datos
def retry_on_db_error(max_retries=3, delay=1):
    """
    Decorador que reintenta operaciones de base de datos en caso de error de conexión.
    
    Args:
        max_retries: Número máximo de reintentos
        delay: Tiempo de espera entre reintentos (en segundos)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (OperationalError, DBAPIError) as e:
                    last_exception = e
                    logging.warning(f"Error de conexión a la base de datos (intento {attempt + 1}/{max_retries}): {e}")
                    
                    # Cerrar y limpiar la sesión actual
                    db.session.rollback()
                    db.session.remove()
                    
                    # Esperar antes de reintentar (excepto en el último intento)
                    if attempt < max_retries - 1:
                        time.sleep(delay * (attempt + 1))  # Backoff exponencial
                    else:
                        logging.error(f"Error de conexión a la base de datos después de {max_retries} intentos: {e}")
                        raise last_exception
            return None
        return wrapper
    return decorator

# Cifra/decifra con Fernet
FERNET_KEY = os.getenv('FERNET_KEY')
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY no está definida en .env. Generala con cryptography.Fernet.generate_key()")

fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)

# ---------- MODELOS ----------
class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Tenant(db.Model):
    __tablename__ = 'tenants'
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    tenant_id = db.Column(db.String(120), nullable=False)

class Client(db.Model):
    __tablename__ = 'clients'
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    client_id = db.Column(db.String(200), nullable=False)
    _client_secret = db.Column("client_secret", db.LargeBinary, nullable=True)

    def set_secret(self, plain: str):
        self._client_secret = fernet.encrypt(plain.encode())

    def get_secret(self):
        if not self._client_secret:
            return None
        try:
            return fernet.decrypt(self._client_secret).decode()
        except InvalidToken:
            return None

class Workspace(db.Model):
    __tablename__ = 'workspaces'
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    workspace_id = db.Column(db.String(200), nullable=False)

class Report(db.Model):
    __tablename__ = 'reports'
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    report_id = db.Column(db.String(200), nullable=False)
    embed_url = db.Column(db.String(1000), nullable=True)

# NUEVO MODELO: UsuarioPBI
class UsuarioPBI(db.Model):
    __tablename__ = 'usuarios_pbi'
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(200), nullable=False, unique=True)
    username = db.Column(db.String(200), nullable=False)
    _password = db.Column("password", db.LargeBinary, nullable=False)

    def set_password(self, plain: str):
        self._password = fernet.encrypt(plain.encode())

    def get_password(self):
        try:
            return fernet.decrypt(self._password).decode()
        except InvalidToken:
            return None

class ReportConfig(db.Model):
    __tablename__ = 'report_configs'
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)

    tenant_id = db.Column(db.BigInteger, db.ForeignKey('tenants.id'), nullable=False)
    client_id = db.Column(db.BigInteger, db.ForeignKey('clients.id'), nullable=False)
    workspace_id = db.Column(db.BigInteger, db.ForeignKey('workspaces.id'), nullable=False)
    report_id_fk = db.Column(db.BigInteger, db.ForeignKey('reports.id'), nullable=False)
    usuario_pbi_id = db.Column(db.BigInteger, db.ForeignKey('usuarios_pbi.id'), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    tenant = db.relationship('Tenant')
    client = db.relationship('Client')
    workspace = db.relationship('Workspace')
    report = db.relationship('Report')
    usuario_pbi = db.relationship('UsuarioPBI')

class PublicLink(db.Model):
    __tablename__ = 'public_links'
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    token = db.Column(db.String(120), unique=True, nullable=False)
    custom_slug = db.Column(db.String(120), unique=True, nullable=True)
    report_config_id = db.Column(db.BigInteger, db.ForeignKey('report_configs.id'), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    report_config = db.relationship('ReportConfig')

# ---------- LOGIN ----------
@login_manager.user_loader
@retry_on_db_error(max_retries=3, delay=1)
def load_user(user_id):
    return User.query.get(int(user_id))

# ---------- RUTAS DE AUTENTICACIÓN ----------
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Length

class LoginForm(FlaskForm):
    username = StringField('Usuario', validators=[DataRequired()])
    password = PasswordField('Contraseña', validators=[DataRequired()])
    remember = BooleanField('Recordarme')
    submit = SubmitField('Entrar')

@app.route('/login', methods=['GET', 'POST'])
@retry_on_db_error(max_retries=3, delay=1)
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            return redirect(url_for('index'))
        flash('Usuario o contraseña inválidos', 'danger')
    return render_template('login.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ---------- PÁGINAS PRINCIPALES ----------
@app.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def index():
    # Cargar configuraciones con sus links públicos
    configs = ReportConfig.query.options(
        db.joinedload(ReportConfig.tenant),
        db.joinedload(ReportConfig.client),
        db.joinedload(ReportConfig.workspace),
        db.joinedload(ReportConfig.report),
        db.joinedload(ReportConfig.usuario_pbi)
    ).order_by(ReportConfig.created_at.desc()).all()
    
    # Obtener todos los links públicos activos
    public_links = PublicLink.query.filter_by(is_active=True).all()
    
    # Crear un diccionario para acceso rápido: config_id -> [links]
    links_by_config = {}
    for link in public_links:
        if link.report_config_id not in links_by_config:
            links_by_config[link.report_config_id] = []
        links_by_config[link.report_config_id].append(link)
    
    return render_template('index.html', configs=configs, links_by_config=links_by_config)

# ---------- CRUDs simplificados ----------
from wtforms import SelectField, TextAreaField

class TenantForm(FlaskForm):
    name = StringField("Nombre", validators=[DataRequired()])
    tenant_id = StringField("Tenant ID", validators=[DataRequired()])
    submit = SubmitField("Guardar")

# Tenants
@app.route('/tenants')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def tenants_list():
    tenants = Tenant.query.all()
    return render_template('base_list.html', 
                         items=tenants,
                         title='Tenants',
                         model_name='Tenant',
                         model_name_plural='tenants',
                         new_url=url_for('tenants_new'),
                         headers=['#', 'Nombre', 'Tenant ID'],
                         fields=['id', 'name', 'tenant_id'])

@app.route('/tenants/new', methods=['GET','POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def tenants_new():
    form = TenantForm()
    if form.validate_on_submit():
        t = Tenant(name=form.name.data, tenant_id=form.tenant_id.data)
        db.session.add(t)
        db.session.commit()
        flash("Tenant creado", "success")
        return redirect(url_for('tenants_list'))
    return render_template('base_form.html', 
                         form=form,
                         title='Nuevo Tenant',
                         back_url=url_for('tenants_list'))

class ClientForm(FlaskForm):
    name = StringField("Nombre cliente", validators=[DataRequired()])
    client_id = StringField("Client ID", validators=[DataRequired()])
    client_secret = PasswordField("Client Secret (se cifrará)")
    submit = SubmitField("Guardar")

# Clients
@app.route('/clients')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def clients_list():
    clients = Client.query.all()
    return render_template('base_list.html', 
                         items=clients,
                         title='Clients',
                         model_name='Client',
                         model_name_plural='clients',
                         new_url=url_for('clients_new'),
                         headers=['#', 'Nombre', 'Client ID', 'Secret'],
                         fields=['id', 'name', 'client_id', 'client_secret'])

@app.route('/clients/new', methods=['GET','POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def clients_new():
    form = ClientForm()
    if form.validate_on_submit():
        c = Client(name=form.name.data, client_id=form.client_id.data)
        if form.client_secret.data:
            c.set_secret(form.client_secret.data)
        db.session.add(c)
        db.session.commit()
        flash("Client creado", "success")
        return redirect(url_for('clients_list'))
    return render_template('base_form.html', 
                         form=form,
                         title='Nuevo Client',
                         back_url=url_for('clients_list'))

class WorkspaceForm(FlaskForm):
    name = StringField("Nombre", validators=[DataRequired()])
    workspace_id = StringField("Workspace ID", validators=[DataRequired()])
    submit = SubmitField("Guardar")

# Workspaces
@app.route('/workspaces')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def workspaces_list():
    ws = Workspace.query.all()
    return render_template('base_list.html', 
                         items=ws,
                         title='Workspaces',
                         model_name='Workspace',
                         model_name_plural='workspaces',
                         new_url=url_for('workspaces_new'),
                         headers=['#', 'Nombre', 'Workspace ID'],
                         fields=['id', 'name', 'workspace_id'])

@app.route('/workspaces/new', methods=['GET','POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def workspaces_new():
    form = WorkspaceForm()
    if form.validate_on_submit():
        w = Workspace(name=form.name.data, workspace_id=form.workspace_id.data)
        db.session.add(w)
        db.session.commit()
        flash("Workspace creado", "success")
        return redirect(url_for('workspaces_list'))
    return render_template('base_form.html', 
                         form=form,
                         title='Nuevo Workspace',
                         back_url=url_for('workspaces_list'))

class ReportForm(FlaskForm):
    name = StringField("Nombre", validators=[DataRequired()])
    report_id = StringField("Report ID", validators=[DataRequired()])
    embed_url = StringField("Embed URL (opcional)")
    submit = SubmitField("Guardar")

# Reports
@app.route('/reports')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def reports_list():
    r = Report.query.all()
    return render_template('base_list.html', 
                         items=r,
                         title='Reports',
                         model_name='Report',
                         model_name_plural='reports',
                         new_url=url_for('reports_new'),
                         headers=['#', 'Nombre', 'Report ID', 'Embed URL'],
                         fields=['id', 'name', 'report_id', 'embed_url'])

@app.route('/reports/new', methods=['GET','POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def reports_new():
    form = ReportForm()
    if form.validate_on_submit():
        rp = Report(name=form.name.data, report_id=form.report_id.data, embed_url=form.embed_url.data)
        db.session.add(rp)
        db.session.commit()
        flash("Reporte creado", "success")
        return redirect(url_for('reports_list'))
    return render_template('base_form.html', 
                         form=form,
                         title='Nuevo Report',
                         back_url=url_for('reports_list'))

# UsuarioPBI
class UsuarioPBIForm(FlaskForm):
    nombre = StringField("Nombre identificador", validators=[DataRequired()])
    username = StringField("Usuario Power BI", validators=[DataRequired()])
    password = PasswordField("Contraseña Power BI", validators=[DataRequired()])
    submit = SubmitField("Guardar")

@app.route('/usuarios-pbi')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def usuarios_pbi_list():
    usuarios = UsuarioPBI.query.all()
    return render_template('base_list.html', 
                         items=usuarios,
                         title='Usuarios Power BI',
                         model_name='Usuario PBI',
                         model_name_plural='usuarios PBI',
                         new_url=url_for('usuarios_pbi_new'),
                         headers=['#', 'Nombre', 'Username'],
                         fields=['id', 'nombre', 'username'])

@app.route('/usuarios-pbi/new', methods=['GET','POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def usuarios_pbi_new():
    form = UsuarioPBIForm()
    if form.validate_on_submit():
        usuario = UsuarioPBI(
            nombre=form.nombre.data,
            username=form.username.data
        )
        usuario.set_password(form.password.data)
        db.session.add(usuario)
        db.session.commit()
        flash("Usuario PBI creado", "success")
        return redirect(url_for('usuarios_pbi_list'))
    return render_template('base_form.html', 
                         form=form,
                         title='Nuevo Usuario PBI',
                         back_url=url_for('usuarios_pbi_list'))

# ReportConfig
class ReportConfigForm(FlaskForm):
    name = StringField("Nombre configuración", validators=[DataRequired()])
    tenant = SelectField("Tenant", coerce=int, validators=[DataRequired()])
    client = SelectField("Client", coerce=int, validators=[DataRequired()])
    workspace = SelectField("Workspace", coerce=int, validators=[DataRequired()])
    report = SelectField("Report", coerce=int, validators=[DataRequired()])
    usuario_pbi = SelectField("Usuario Power BI", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Guardar")

@app.route('/configs')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def configs_list():
    cs = ReportConfig.query.options(
        db.joinedload(ReportConfig.tenant),
        db.joinedload(ReportConfig.client),
        db.joinedload(ReportConfig.workspace),
        db.joinedload(ReportConfig.report),
        db.joinedload(ReportConfig.usuario_pbi)
    ).all()
    
    return render_template('base_list.html', 
                         items=cs,
                         title='Configuraciones',
                         model_name='Configuración',
                         model_name_plural='configuraciones',
                         new_url=url_for('configs_new'),
                         headers=['#', 'Nombre', 'Tenant', 'Client', 'Workspace', 'Report', 'Usuario PBI'],
                         fields=['id', 'name', 'tenant.name', 'client.name', 'workspace.name', 'report.name', 'usuario_pbi.nombre'])

@app.route('/configs/new', methods=['GET','POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def configs_new():
    form = ReportConfigForm()
    # poblar selects
    form.tenant.choices = [(t.id, t.name) for t in Tenant.query.order_by(Tenant.name).all()]
    form.client.choices = [(c.id, c.name) for c in Client.query.order_by(Client.name).all()]
    form.workspace.choices = [(w.id, w.name) for w in Workspace.query.order_by(Workspace.name).all()]
    form.report.choices = [(r.id, r.name) for r in Report.query.order_by(Report.name).all()]
    form.usuario_pbi.choices = [(u.id, u.nombre) for u in UsuarioPBI.query.order_by(UsuarioPBI.nombre).all()]

    if form.validate_on_submit():
        rc = ReportConfig(
            name=form.name.data,
            tenant_id=form.tenant.data,
            client_id=form.client.data,
            workspace_id=form.workspace.data,
            report_id_fk=form.report.data,
            usuario_pbi_id=form.usuario_pbi.data
        )
        db.session.add(rc)
        db.session.commit()
        flash("Configuración creada", "success")
        return redirect(url_for('configs_list'))
    return render_template('base_form.html', 
                         form=form,
                         title='Nueva Configuración',
                         back_url=url_for('configs_list'))

# Public Links
class PublicLinkForm(FlaskForm):
    custom_slug = StringField("Nombre personalizado para el link", validators=[DataRequired(), Length(max=120)])
    submit = SubmitField("Crear Link")

@app.route('/configs/<int:config_id>/link/new', methods=['GET','POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def configs_new_link(config_id):
    cfg = ReportConfig.query.get_or_404(config_id)
    form = PublicLinkForm()
    
    if form.validate_on_submit():
        custom_slug = form.custom_slug.data.lower().strip()
        
        # Verificar si el slug ya existe
        existing_link = PublicLink.query.filter_by(custom_slug=custom_slug).first()
        if existing_link:
            flash("Este nombre personalizado ya está en uso. Por favor elige otro.", "danger")
            return render_template('create_public_link.html', form=form, config=cfg)
        
        # Generar token único (para seguridad interna)
        token = uuid.uuid4().hex[:16]
        
        link = PublicLink(
            token=token, 
            custom_slug=custom_slug,
            report_config_id=cfg.id, 
            is_active=True
        )
        db.session.add(link)
        db.session.commit()
        
        # Generar URL completa para mostrar al usuario
        #base_url = request.url_root.rstrip('/') --> Esto servía en desarrollo sin SSL de cloudflare
        base_url = f"https://{request.host}"
        public_url = f"{base_url}/p/{custom_slug}"
        
        flash(f"Link público creado: {public_url}", "success")
        return redirect(url_for('configs_list'))
    
    return render_template('create_public_link.html', form=form, config=cfg)

# ---------- VISTAS DE REPORTES ----------
@app.route('/p/<custom_slug>')
@retry_on_db_error(max_retries=3, delay=1)
def public_view(custom_slug):
    link = PublicLink.query.filter_by(custom_slug=custom_slug, is_active=True).first_or_404()
    cfg = link.report_config

    try:
        embed_token, embed_url, report_id = get_embed_for_config(cfg)
    except Exception as e:
        logging.error(f"Error generando embed token: {e}")
        return render_template('error_public.html', 
                             error_message=f"Error generando embed token: {e}",
                             config_name=cfg.name), 500

    return render_template('report_base.html',
                           embed_token=embed_token,
                           embed_url=embed_url,
                           report_id=report_id,
                           config_name=cfg.name,
                           is_public=True)

@app.route('/configs/<int:config_id>/view')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def config_view(config_id):
    cfg = ReportConfig.query.get_or_404(config_id)
    
    try:
        embed_token, embed_url, report_id = get_embed_for_config(cfg)
    except Exception as e:
        logging.error(f"Error generando embed token: {e}")
        flash(f"Error cargando reporte: {e}", "danger")
        return redirect(url_for('configs_list'))

    return render_template('report_base.html',
                           embed_token=embed_token,
                           embed_url=embed_url,
                           report_id=report_id,
                           config_name=cfg.name,
                           is_public=False)

# ---------- LÓGICA PARA OBTENER EMBED TOKEN ----------
def get_embed_for_config(cfg: ReportConfig):
    """
    Obtiene el embed token y la URL del reporte para Power BI usando ROPC.
    Usa la configuración almacenada en la base de datos.
    """
    # Obtener las credenciales y IDs desde el modelo
    tenant_id = cfg.tenant.tenant_id
    client_id = cfg.client.client_id
    client_secret = cfg.client.get_secret()
    
    user_pbi = cfg.usuario_pbi.username
    pass_pbi = cfg.usuario_pbi.get_password()
    
    workspace_id = cfg.workspace.workspace_id
    report_id = cfg.report.report_id

    # Verificar que tenemos los datos necesarios
    if not client_secret:
        raise RuntimeError("Client secret no disponible. Guarda el secret en el cliente.")
    if not user_pbi or not pass_pbi:
        raise RuntimeError("Usuario o contraseña de Power BI no disponibles.")

    logging.debug(f"Obteniendo token para tenant: {tenant_id}, client: {client_id}, user: {user_pbi}")

    # 1. Obtener token de Azure AD
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://analysis.windows.net/powerbi/api/.default",
        "username": user_pbi,
        "password": pass_pbi
    }

    r = requests.post(token_url, data=data)
    r.raise_for_status()
    access_token = r.json().get("access_token")
    logging.debug("Access token recibido correctamente")

    # 2. Obtener información del reporte desde Power BI REST API
    report_url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(report_url, headers=headers)
    resp.raise_for_status()
    report_info = resp.json()
    logging.debug(f"Embed URL obtenido: {report_info.get('embedUrl')}")

    # 3. Retornar el token de acceso (como embed token) y la embed URL
    embed_token = access_token
    embed_url = report_info["embedUrl"]

    return embed_token, embed_url, report_id

# ---------- UTILIDADES ADMIN ----------
@app.cli.command("create-admin")
def create_admin():
    username = input("Username admin: ")
    password = input("Password: ")
    if User.query.filter_by(username=username).first():
        print("Usuario ya existe")
        return
    u = User(username=username, is_admin=True)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    print("Admin creado")

# ---------- ARRANQUE ----------
if __name__ == '__main__':
    # Crear tablas si no existen (para dev). En prod usar migraciones.
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=2052, debug=False)