# Architecture Documentation

## Project Structure

The Power BI Flask Embed application follows a modular architecture pattern:

```
powerbi-flask-embed/
├── app/                          # Main application package
│   ├── __init__.py              # Application factory and configuration
│   ├── models.py                # Database models (SQLAlchemy)
│   ├── forms.py                 # WTForms form definitions
│   ├── routes/                  # Blueprint routes (modular routing)
│   │   ├── __init__.py
│   │   ├── auth.py              # Authentication (login/logout)
│   │   ├── main.py              # Dashboard and main pages
│   │   ├── tenants.py           # Azure AD tenant management
│   │   ├── clients.py           # Azure AD client management
│   │   ├── workspaces.py        # Power BI workspace management
│   │   ├── reports.py           # Power BI report management
│   │   ├── usuarios_pbi.py      # Power BI user credentials
│   │   ├── configs.py           # Report configuration management
│   │   └── public.py            # Public report viewing (no auth)
│   ├── templates/               # Jinja2 HTML templates
│   │   ├── layout.html          # Base layout template
│   │   ├── login.html           # Login page
│   │   ├── index.html           # Dashboard
│   │   ├── base_list.html       # Generic list template
│   │   ├── base_form.html       # Generic form template
│   │   ├── create_public_link.html
│   │   ├── report_base.html     # Report viewer
│   │   └── error_public.html
│   └── utils/                   # Utility functions
│       ├── __init__.py
│       ├── decorators.py        # Database retry decorator
│       └── powerbi.py           # Power BI API integration
├── run.py                       # Application entry point
├── Dockerfile                   # Docker container definition
├── docker-compose.yml           # Docker Compose configuration
├── requirements.txt             # Python dependencies
└── README.md                    # Project documentation
```

## Design Patterns

### 1. Application Factory Pattern

The application uses the factory pattern in `app/__init__.py`:

```python
def create_app():
    """Create and configure the Flask application."""
    app = Flask(__name__)
    # Configuration
    # Extension initialization
    # Blueprint registration
    return app
```

**Benefits:**
- Easy testing with different configurations
- Ability to create multiple instances
- Cleaner dependency injection

### 2. Blueprint Pattern

Routes are organized into blueprints by functionality:

- **auth**: Authentication and session management
- **main**: Dashboard and main interface
- **tenants**: Azure AD tenant CRUD operations
- **clients**: Azure AD client CRUD operations
- **workspaces**: Power BI workspace CRUD operations
- **reports**: Power BI report CRUD operations
- **usuarios_pbi**: Power BI user credential management
- **configs**: Report configuration linking
- **public**: Public report access

**Benefits:**
- Modular code organization
- Easier maintenance and testing
- Clear separation of concerns
- URL prefix organization

### 3. Template Inheritance

Templates use Jinja2 inheritance:

```
layout.html (base template)
    ├── login.html
    ├── index.html
    ├── base_list.html
    ├── base_form.html
    └── ... (other pages)
```

**Benefits:**
- Consistent styling across pages
- DRY (Don't Repeat Yourself) principle
- Easy to maintain and update UI

### 4. Decorator Pattern

Custom decorators for cross-cutting concerns:

```python
@retry_on_db_error(max_retries=3, delay=1)
def database_operation():
    # Database operation with automatic retry
    pass
```

**Benefits:**
- Automatic error handling
- Resilience against transient failures
- Cleaner code without try-catch blocks

## Data Flow

### 1. Request Flow

```
Client Request
    ↓
Flask App (run.py)
    ↓
Blueprint Route (app/routes/*.py)
    ↓
Form Validation (app/forms.py)
    ↓
Business Logic / Utils (app/utils/*.py)
    ↓
Database Access (app/models.py)
    ↓
Template Rendering (app/templates/*.html)
    ↓
Response to Client
```

### 2. Authentication Flow

```
User Login
    ↓
auth.login route
    ↓
Form Validation
    ↓
User.check_password()
    ↓
login_user() (Flask-Login)
    ↓
Session Cookie Set
    ↓
Redirect to Dashboard
```

### 3. Power BI Embed Flow

```
User Accesses Report
    ↓
configs.view or public.view route
    ↓
get_embed_for_config()
    ↓
Azure AD Authentication (ROPC)
    ↓
Power BI REST API Call
    ↓
Embed Token & URL Retrieved
    ↓
report_base.html Template
    ↓
Power BI JavaScript SDK
    ↓
Embedded Report Displayed
```

## Database Schema

### Entity Relationship

```
User (authentication)

Tenant ──┐
Client ──┤
Workspace├─→ ReportConfig ──→ PublicLink
Report ──┤
UsuarioPBI┘
```

### Key Relationships

- **ReportConfig** is the central entity linking all components
- **PublicLink** references a **ReportConfig** for public access
- All credential fields are encrypted using Fernet

## Security Architecture

### 1. Credential Encryption

```python
# Encryption Flow
Plain Text Credential
    ↓
Fernet.encrypt()
    ↓
Encrypted Binary Storage
    ↓
Database (LargeBinary column)

# Decryption Flow
Database Read
    ↓
Fernet.decrypt()
    ↓
Plain Text for API Call
```

### 2. Authentication

- **Flask-Login** for session management
- **Werkzeug** for password hashing (scrypt)
- Session cookies with secure flags (production)

### 3. Database Connection Security

- Connection pooling with pre-ping validation
- Automatic retry with exponential backoff
- Connection recycling to prevent stale connections

## Extension Points

### Adding a New Route Blueprint

1. Create `app/routes/new_feature.py`
2. Define blueprint: `bp = Blueprint('new_feature', __name__)`
3. Add routes with decorators
4. Register in `app/__init__.py`: `app.register_blueprint(new_feature.bp)`

### Adding a New Model

1. Add class to `app/models.py`
2. Define columns with SQLAlchemy
3. Add relationships if needed
4. Create migration: `flask db migrate`
5. Apply migration: `flask db upgrade`

### Adding a New Form

1. Add class to `app/forms.py`
2. Define fields with WTForms
3. Add validators
4. Use in route with `form.validate_on_submit()`

## Configuration

### Environment Variables

Required:
- `SECRET_KEY`: Flask session encryption
- `FERNET_KEY`: Credential encryption key
- `SQLALCHEMY_DATABASE_URI`: Database connection string

Optional:
- `DEBUG`: Debug mode (default: False)
- `FLASK_ENV`: Environment (development/production)

### Database Engine Options

For PostgreSQL (production):
```python
{
    'pool_size': 10,
    'pool_recycle': 3600,
    'pool_pre_ping': True,
    'max_overflow': 20,
    'pool_timeout': 30,
    'connect_args': {'connect_timeout': 10}
}
```

For SQLite (development):
```python
{
    'pool_pre_ping': True
}
```

## Performance Considerations

### 1. Database Connection Pooling

- 10 permanent connections
- 20 additional overflow connections
- 1-hour connection recycling
- Pre-ping validation

### 2. Template Caching

- Jinja2 automatic template caching
- Production: bytecode compilation

### 3. Static Files

- Served via CDN (Bootstrap, Bootstrap Icons)
- No local static file overhead

## Testing Strategy

### Unit Tests

- Test individual functions
- Mock database calls
- Test form validation

### Integration Tests

- Test blueprint routes
- Test database operations
- Test Power BI API integration

### End-to-End Tests

- Test complete user flows
- Test authentication
- Test report embedding

## Deployment

### Docker Deployment

```bash
docker-compose up --build
```

### Manual Deployment

```bash
pip install -r requirements.txt
flask db upgrade
flask create-admin
python run.py
```

### Production Considerations

1. Use a WSGI server (Gunicorn, uWSGI)
2. Configure reverse proxy (Nginx, Apache)
3. Enable HTTPS/TLS
4. Set secure session cookies
5. Use production database (PostgreSQL)
6. Configure proper logging
7. Set up monitoring and alerts

## Maintenance

### Database Migrations

```bash
# Create migration
flask db migrate -m "Description"

# Apply migration
flask db upgrade

# Rollback
flask db downgrade
```

### Updating Dependencies

```bash
pip install --upgrade -r requirements.txt
```

### Log Management

- Application logs: stdout/stderr
- Database logs: PostgreSQL logs
- Access logs: Reverse proxy logs

## Future Enhancements

1. **User Management**: Add user roles and permissions
2. **Report Scheduling**: Automated report generation
3. **Analytics**: Track report views and user activity
4. **API**: RESTful API for programmatic access
5. **Multi-tenancy**: Support for multiple organizations
6. **Caching**: Redis for session and data caching
7. **Asynchronous Tasks**: Celery for background jobs
