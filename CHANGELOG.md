# Changelog

## [Unreleased] - 2026-01-10

### Added
- **Empresa Model**: Renamed `ClientePrivado` to `Empresa` for better semantic meaning (companies that access reports via API)
  - Added `cuit` field for company tax identification
  - Maintained backward compatibility with `ClientePrivado` alias
  
- **Many-to-Many Relationships**: Reports can now be associated with multiple empresas and vice versa
  - Created `empresa_report_config` association table
  - Empresas can access multiple private reports
  - Reports can be accessed by multiple empresas
  
- **Dual Privacy Model**: Reports can now be both public AND private simultaneously
  - Added `es_publico` boolean field (can be accessed via public links)
  - Added `es_privado` boolean field (can be accessed via API by empresas)
  - Reports no longer restricted to single privacy type
  - Legacy `tipo_privacidad` field maintained for backward compatibility
  
- **FuturaEmpresa Model**: New workflow for managing pending company approvals
  - Simulates external system integration for company onboarding
  - Tracks company approval status (pendiente/confirmada/rechazada)
  - Links confirmed companies to created Empresa records
  - Stores external system IDs for synchronization
  
- **New API Endpoints**:
  - `GET /private/reports`: List all reports accessible by authenticated empresa
  - Improved `GET /private/report-config`: Now supports many-to-many relationships
  - `POST /private/login`: Updated to work with Empresa model
  
- **API Documentation**:
  - Added `/docs` route with comprehensive API documentation
  - OpenAPI 3.0 specification available at `/docs/openapi.json`
  - Interactive documentation with examples and response codes
  
- **Futuras Empresas Management**:
  - New admin interface for managing pending companies
  - Simulate external API calls for company list retrieval
  - Confirm/reject workflow with notes and audit trail
  - Automatic credential generation on confirmation
  
- **Enhanced UI**:
  - New navbar dropdown for "Futuras Empresas"
  - API Documentation link in navbar
  - Updated Configuración dropdown to show Empresas
  - Improved configs list showing privacy type and empresa associations
  
- **Unit Tests**:
  - `test_empresa_model.py`: Tests for Empresa model and relationships
  - `test_private_reports_endpoint.py`: Tests for new /private/reports endpoint
  - Updated existing tests to use Empresa instead of ClientePrivado

### Changed
- **Report Configuration Form**: Now supports selecting multiple empresas and dual privacy settings
- **Configs List View**: Enhanced to show privacy type, associated empresas, and public links count
- **Empresa Admin Routes**: Complete CRUD operations with improved templates
- **Private API**: Updated all endpoints to use Empresa model

### Fixed
- **Configs Endpoint**: `/configs/` now shows complete information instead of just ID and name
- Database migration to add new fields and relationships

### Migration Guide

#### Database Migration
Run the following to apply database changes:
```bash
flask db upgrade
```

#### API Changes
If you're using the private API:
1. Endpoints remain the same (`/private/login`, `/private/report-config`)
2. New endpoint available: `/private/reports` to list all accessible reports
3. Authentication tokens remain compatible
4. Consider using new `/private/reports` endpoint to discover available reports

#### UI Changes
1. "Clientes Privados" renamed to "Empresas" in navigation
2. Legacy "Clientes Privados" link still available for backward compatibility
3. New "Futuras Empresas" section for managing pending companies
4. Report configurations now support both public and private access simultaneously

## Security Notes
- All empresa credentials are hashed using Werkzeug's password hashing
- JWT tokens remain valid (default: 1 hour)
- External system simulation for futuras empresas (ready for real API integration)
