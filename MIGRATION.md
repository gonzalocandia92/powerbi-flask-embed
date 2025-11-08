# Migration Guide

This guide helps you migrate from the old monolithic structure to the new modular architecture.

## What Changed

### File Structure

#### Before
```
powerbi-flask-embed/
├── app.py              # All code in one file (~670 lines)
├── embed.py            # Legacy file (not used)
├── templates/
│   └── *.html
├── README.md
└── DATABASE_CONNECTION_FIX.md
```

#### After
```
powerbi-flask-embed/
├── run.py              # Application entry point
├── app/
│   ├── __init__.py     # Application factory
│   ├── models.py       # Database models
│   ├── forms.py        # WTForms
│   ├── routes/         # Modular routes
│   ├── templates/      # Jinja2 templates
│   └── utils/          # Utilities
├── README.md           # Comprehensive documentation
└── ARCHITECTURE.md     # System design documentation
```

### Import Changes

#### Before
```python
# Old app.py imports
from flask import Flask, render_template, ...
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
db = SQLAlchemy(app)
```

#### After
```python
# New modular imports
from app import create_app, db
from app.models import User, ReportConfig
from app.forms import LoginForm

app = create_app()
```

### Route Changes

#### Before (Monolithic)
```python
@app.route('/login')
def login():
    # Login logic
    pass

@app.route('/tenants')
def tenants_list():
    # Tenant logic
    pass
```

#### After (Blueprints)
```python
# app/routes/auth.py
bp = Blueprint('auth', __name__)

@bp.route('/login')
def login():
    # Login logic
    pass

# app/routes/tenants.py
bp = Blueprint('tenants', __name__, url_prefix='/tenants')

@bp.route('/')
def list():
    # Tenant logic
    pass
```

### URL Changes

Most URLs remain the same, but some have been updated for consistency:

| Old URL | New URL | Notes |
|---------|---------|-------|
| `/login` | `/login` | Same |
| `/` | `/` | Same |
| `/tenants` | `/tenants/` | Added trailing slash |
| `/tenants/new` | `/tenants/new` | Same |
| `/configs/<id>/view` | `/configs/<id>/view` | Same |
| `/p/<slug>` | `/p/<slug>` | Same (public links) |

### Template Reference Changes

#### Before
```python
return redirect(url_for('tenants_list'))
```

#### After
```python
return redirect(url_for('tenants.list'))
```

## Migration Steps

### For Development

1. **Pull the latest changes**
   ```bash
   git pull origin copilot/modularize-code-and-update-templates
   ```

2. **Update your `.env` file** (if needed)
   - No changes required to environment variables
   - Same configuration works as before

3. **Update Docker setup** (if using Docker)
   - Docker configuration updated automatically
   - Entry point changed from `app.py` to `run.py`

4. **Database migrations** (if needed)
   ```bash
   flask db upgrade
   ```

5. **Test the application**
   ```bash
   python run.py
   # Or with Docker:
   docker-compose up --build
   ```

### For Production

1. **Backup your database**
   ```bash
   pg_dump your_database > backup_$(date +%Y%m%d).sql
   ```

2. **Pull the new code**
   ```bash
   git pull origin copilot/modularize-code-and-update-templates
   ```

3. **Update dependencies** (if needed)
   ```bash
   pip install -r requirements.txt
   ```

4. **Run database migrations**
   ```bash
   flask db upgrade
   ```

5. **Update your deployment configuration**
   - Change application entry point from `app.py` to `run.py`
   - Example for Gunicorn:
     ```bash
     # Old
     gunicorn app:app
     
     # New
     gunicorn "app:create_app()"
     ```

6. **Restart the application**
   ```bash
   sudo systemctl restart your-app-service
   # Or with Docker:
   docker-compose down && docker-compose up -d
   ```

7. **Verify functionality**
   - Test login
   - Test report viewing
   - Test public links
   - Check logs for errors

## Breaking Changes

### ✅ None for Users
- All URLs remain the same
- Public links continue to work
- Database schema unchanged
- Environment variables unchanged

### ⚠️ For Developers

1. **Import paths changed**
   ```python
   # Old
   from app import User, db
   
   # New
   from app import db
   from app.models import User
   ```

2. **Blueprint structure**
   ```python
   # Old
   @app.route('/path')
   
   # New
   @bp.route('/path')
   ```

3. **Entry point changed**
   ```bash
   # Old
   python app.py
   
   # New
   python run.py
   ```

## Rollback Plan

If you need to rollback to the old version:

1. **Checkout the previous commit**
   ```bash
   git checkout 5b4ece2  # Last commit before modularization
   ```

2. **Restore your environment**
   ```bash
   pip install -r requirements.txt
   ```

3. **Restart the application**
   ```bash
   python app.py
   # Or with Docker:
   docker-compose up --build
   ```

## Common Issues and Solutions

### Issue: Import errors
**Solution:** Make sure you're importing from the correct modules:
```python
from app import create_app, db
from app.models import User, ReportConfig
from app.forms import LoginForm
```

### Issue: Template not found
**Solution:** Templates are now in `app/templates/`. Flask will find them automatically.

### Issue: Blueprint URL not working
**Solution:** Check that the blueprint is registered in `app/__init__.py`:
```python
app.register_blueprint(auth.bp)
app.register_blueprint(tenants.bp)
# etc.
```

### Issue: Database connection errors
**Solution:** The new code includes better error handling. Check your `SQLALCHEMY_DATABASE_URI` in `.env`.

## Benefits of the New Structure

### For Developers
- ✅ Easier to find and modify code
- ✅ Better code organization
- ✅ Easier to test individual components
- ✅ Better IDE support and autocomplete
- ✅ Follows Flask best practices

### For Operations
- ✅ Better error handling and logging
- ✅ Improved database connection resilience
- ✅ Easier to deploy and scale
- ✅ Better documentation

### For Security
- ✅ Clearer security boundaries
- ✅ Better credential management
- ✅ CodeQL verified (0 vulnerabilities)

## Testing Checklist

After migration, verify:

- [ ] Application starts without errors
- [ ] Login works correctly
- [ ] Dashboard displays configurations
- [ ] Can create new tenants/clients/workspaces/reports
- [ ] Can create report configurations
- [ ] Can generate public links
- [ ] Public links work without authentication
- [ ] Private reports require authentication
- [ ] Power BI reports load correctly
- [ ] Database operations complete successfully

## Getting Help

If you encounter issues during migration:

1. Check the logs for error messages
2. Review the [README.md](README.md) for setup instructions
3. Review the [ARCHITECTURE.md](ARCHITECTURE.md) for design details
4. Create an issue on GitHub with:
   - Error message
   - Steps to reproduce
   - Environment details (Python version, database, etc.)

## Additional Resources

- [README.md](README.md) - Setup and usage documentation
- [ARCHITECTURE.md](ARCHITECTURE.md) - System design documentation
- [Flask Blueprints](https://flask.palletsprojects.com/en/latest/blueprints/) - Official documentation
- [Application Factory](https://flask.palletsprojects.com/en/latest/patterns/appfactories/) - Official pattern guide
