# Power BI Flask Embed

A professional Flask application for embedding Microsoft Power BI reports using Azure Active Directory authentication and the Power BI REST API.

## Features

- **Secure Authentication**: Uses Azure AD ROPC (Resource Owner Password Credentials) flow
- **Report Management**: Centralized management of Power BI reports and configurations
- **Public Links**: Generate shareable public links for reports without authentication
- **Analytics & Metrics**: Comprehensive visit tracking with privacy-first approach
- **Modular Architecture**: Clean, maintainable code structure with separation of concerns
- **Database Connection Resilience**: Automatic retry mechanism for database connection failures
- **Responsive UI**: Professional, mobile-friendly interface built with Bootstrap 5
- **Encryption**: Secure storage of sensitive credentials using Fernet encryption

## Architecture

The application follows a modular design pattern:

```
app/
├── __init__.py          # Application factory
├── models.py            # Database models
├── forms.py             # WTForms definitions
├── routes/              # Blueprint routes
│   ├── auth.py          # Authentication routes
│   ├── main.py          # Dashboard routes
│   ├── analytics.py     # Analytics and metrics
│   ├── tenants.py       # Tenant management
│   ├── clients.py       # Client management
│   ├── workspaces.py    # Workspace management
│   ├── reports.py       # Report management
│   ├── usuarios_pbi.py  # Power BI user management
│   ├── configs.py       # Configuration management
│   └── public.py        # Public report viewing
├── utils/               # Utility functions
│   ├── decorators.py    # Database retry decorator
│   ├── powerbi.py       # Power BI API integration
│   └── analytics.py     # Analytics tracking service
└── templates/           # Jinja2 templates
```

## Requirements

- Python 3.11+
- PostgreSQL database (recommended for production)
  - SQLite is supported for development but has limitations with autoincrement on BigInteger fields
- Docker (optional, for containerized deployment)
- Azure AD application with configured permissions:
  - `Report.Read.All`
  - `Dataset.Read.All`
  - `Workspace.Read.All`
- Power BI Pro license for the service account

## Installation

### Using Docker (Recommended)

1. Clone the repository:
```bash
git clone https://github.com/gonzalocandia92/powerbi-flask-embed.git
cd powerbi-flask-embed
```

2. Create a `.env` file based on `.example_env`:
```env
SECRET_KEY=your-secret-key-here
FERNET_KEY=your-fernet-key-here
SQLALCHEMY_DATABASE_URI=postgresql://user:password@host:port/database

# Azure AD Configuration
TENANT_ID=your-tenant-id
CLIENT_ID=your-client-id
CLIENT_SECRET=your-client-secret

# Power BI User Credentials
USER=powerbi-user@domain.com
PASS=powerbi-password

# Power BI Configuration
WORKSPACE_ID=your-workspace-id
REPORT_ID=your-report-id
```

3. Generate encryption key:
```python
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
```

4. Start the application:
```bash
docker-compose up --build
```

### Manual Installation

1. Clone the repository:
```bash
git clone https://github.com/gonzalocandia92/powerbi-flask-embed.git
cd powerbi-flask-embed
```

2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Configure environment variables (create `.env` file as shown above)

5. Initialize the database:
```bash
flask db upgrade
```

6. Create an admin user:
```bash
flask create-admin
```

7. Run the application:
```bash
python run.py
```

The application will be available at `http://localhost:2052`

## Azure AD Configuration

### 1. Register Application in Azure Portal

1. Navigate to [Azure Portal](https://portal.azure.com)
2. Go to **Azure Active Directory** > **App registrations**
3. Click **New registration**
4. Configure the application:
   - Name: `PowerBI-Flask-Embed`
   - Supported account types: Select appropriate option
   - Redirect URI: Not required for ROPC flow

### 2. Configure API Permissions

1. Go to **API permissions**
2. Add permissions for **Power BI Service**:
   - `Dataset.Read.All` (Delegated)
   - `Report.Read.All` (Delegated)
   - `Workspace.Read.All` (Delegated)
3. Grant admin consent for your organization

### 3. Enable ROPC Flow

1. Go to **Authentication**
2. Under **Advanced settings**, enable:
   - "Allow public client flows"
3. Save changes

### 4. Create Client Secret

1. Go to **Certificates & secrets**
2. Click **New client secret**
3. Add description and set expiration
4. Copy the secret value (only shown once)

### 5. Collect Required IDs

- **Tenant ID**: Found in Azure AD overview
- **Client ID**: Found in app registration overview
- **Workspace ID**: From Power BI workspace URL
- **Report ID**: From Power BI report URL

## Usage

### Creating a Report Configuration

1. Log in to the application
2. Navigate to **Configuration** menu
3. Create required components in order:
   - **Tenant**: Azure AD tenant configuration
   - **Client**: Azure AD application credentials
   - **Workspace**: Power BI workspace
   - **Report**: Power BI report details
   - **Power BI User**: Service account credentials
4. Create a **Configuration** linking all components

### Generating Public Links

1. Navigate to the dashboard
2. Find the configuration you want to share
3. Click **New Link**
4. Enter a custom slug (e.g., `sales-report-2024`)
5. Share the generated URL: `https://yourdomain.com/p/sales-report-2024`

### Viewing Reports

- **Private**: Requires login, click "Ver Reporte" on any configuration
- **Public**: Access via generated public link, no authentication required

### Analytics and Metrics

The application includes comprehensive analytics for tracking visits to public links.

#### Accessing Analytics

1. Log in to the application
2. Click **Analytics** in the navigation bar
3. View metrics for all public links or filter by specific link slug
4. Select time range (7, 30, 60, or 90 days)

#### Metrics Tracked

The analytics dashboard provides the following insights:

1. **Total Visits**: Count of all human visits (bots excluded)
2. **Unique Visitors**: Number of distinct visitors based on cookie ID
3. **Bot Visits**: Automated traffic detected and filtered out
4. **Hourly Distribution**: 24-hour breakdown of visit patterns
5. **Daily Trend**: Visit trends over the selected time period
6. **Top Referrers**: Sources directing traffic to your reports
7. **UTM Parameters**: Track campaign sources, mediums, and campaigns
8. **Device Types**: Mobile, tablet, and desktop breakdowns
9. **Browser Statistics**: Most popular browsers accessing your reports
10. **Operating Systems**: OS distribution of visitors

#### Privacy and Compliance

Analytics are designed with privacy in mind:

- **IP Anonymization**: IP addresses are hashed with a salt before storage
- **Do Not Track**: Respects DNT header when present
- **No PII**: No personally identifiable information is collected
- **Cookie-based**: Uses anonymous UUID for visitor identification
- **Bot Detection**: Filters out 30+ bot patterns automatically

#### API Access

For programmatic access to analytics data:

```bash
GET /analytics/api/stats?link_slug=my-report&days=30
```

Response includes all metrics in JSON format. Requires authentication.

#### Configuration

Analytics can be configured via environment variables:

```env
# Enable/disable analytics tracking
ANALYTICS_ENABLED=true

# Salt for IP address hashing (change in production!)
ANALYTICS_SALT=your-random-salt-here

# Respect Do Not Track header
ANALYTICS_RESPECT_DNT=true
```

#### Testing with Sample Data

To test the analytics dashboard with sample data:

```bash
# Generate sample data for a demo link
python seed_analytics.py demo-report 30 50

# Parameters:
# 1. Link slug (default: demo-report)
# 2. Number of days (default: 30)
# 3. Visits per day (default: 50)
```

This will create realistic sample visits including:
- Varied visit patterns across different hours
- Multiple unique visitors with return visits
- Referrer data and UTM parameters
- Device and browser variety
- Bot visits (automatically filtered in analytics)

## Database Schema

The application uses the following main models:

- **User**: Application users for authentication
- **Tenant**: Azure AD tenant configurations
- **Client**: Azure AD application registrations
- **Workspace**: Power BI workspaces
- **Report**: Power BI reports
- **UsuarioPBI**: Power BI service account credentials
- **ReportConfig**: Complete configuration linking all components
- **PublicLink**: Public access links for reports
- **Visit**: Analytics data for public link visits

## Security Features

### Credential Encryption

All sensitive credentials (client secrets, Power BI passwords) are encrypted using Fernet symmetric encryption before storage in the database.

### Database Connection Resilience

The application includes automatic retry logic with exponential backoff for database operations, ensuring resilience against transient connection failures.

### Configuration

Database pool settings in `app/__init__.py`:
- `pool_size`: 10 permanent connections
- `pool_recycle`: 3600 seconds (1 hour)
- `pool_pre_ping`: True (validates connections before use)
- `max_overflow`: 20 additional connections
- `pool_timeout`: 30 seconds

## Development

### Running Tests

```bash
pytest
```

### Database Migrations

Create a new migration:
```bash
flask db migrate -m "Description of changes"
```

Apply migrations:
```bash
flask db upgrade
```

Rollback migration:
```bash
flask db downgrade
```

## Troubleshooting

### Common Issues

**Database Connection Errors**
- Verify `SQLALCHEMY_DATABASE_URI` in `.env`
- Ensure PostgreSQL is running
- Check network connectivity

**Authentication Failures**
- Verify Azure AD credentials in configuration
- Ensure ROPC flow is enabled
- Check API permissions are granted

**Report Not Loading**
- Verify Power BI service account has access to the workspace
- Check report ID and workspace ID are correct
- Review browser console for JavaScript errors

**SQLite Autoincrement Issues**
- SQLite has limitations with BigInteger autoincrement fields
- If you encounter "NOT NULL constraint failed: *.id" errors, use PostgreSQL instead
- For development only: You can use `db.create_all()` but migrations are preferred
- **Recommendation**: Use PostgreSQL for production deployments

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License.

## Support

For issues and questions:
- Create an issue in the GitHub repository
- Contact: gonzalocandia92

## Acknowledgments

- Microsoft Power BI for the embedding API
- Flask framework and extensions
- Bootstrap for the UI components
