# Empresas y Reportes - Guía de Uso

## Índice
1. [Introducción](#introducción)
2. [Gestión de Empresas](#gestión-de-empresas)
3. [Configuración de Reportes](#configuración-de-reportes)
4. [Futuras Empresas](#futuras-empresas)
5. [API Privada](#api-privada)
6. [Ejemplos de Integración](#ejemplos-de-integración)

## Introducción

El sistema ahora soporta una gestión flexible de empresas (anteriormente "Clientes Privados") y reportes con las siguientes mejoras:

- **Empresas**: Compañías que acceden a reportes privados mediante autenticación API
- **Reportes Flexibles**: Los reportes pueden ser públicos, privados, o ambos
- **Relaciones Many-to-Many**: Una empresa puede acceder a múltiples reportes y un reporte puede ser accedido por múltiples empresas
- **Workflow de Aprobación**: Sistema de futuras empresas para gestionar solicitudes pendientes

## Gestión de Empresas

### ¿Qué es una Empresa?

Una Empresa (anteriormente Cliente Privado) es una compañía que puede acceder a reportes privados mediante autenticación API. Cada empresa tiene:

- **Nombre**: Identificador descriptivo
- **CUIT**: Identificación tributaria (opcional)
- **Credenciales API**: `client_id` y `client_secret` generados automáticamente
- **Estado**: Activa/Inactiva
- **Reportes Asociados**: Lista de reportes a los que tiene acceso

### Crear una Empresa

1. Navegar a **Configuración → Empresas**
2. Click en **Nueva Empresa**
3. Completar el formulario:
   - Nombre de la empresa
   - CUIT (opcional)
4. Click en **Guardar**
5. **IMPORTANTE**: Guardar las credenciales mostradas (solo se muestran una vez)

### Gestionar Empresas

Desde la lista de empresas puedes:

- **Editar**: Modificar nombre y CUIT
- **Activar/Desactivar**: Controlar acceso sin eliminar la empresa
- **Regenerar Credenciales**: Crear nuevas credenciales (las anteriores dejan de funcionar)
- **Eliminar**: Solo si no tiene reportes asociados

## Configuración de Reportes

### Tipos de Privacidad

Los reportes ahora soportan configuración dual:

- **Público**: Accesible mediante links públicos sin autenticación
- **Privado**: Accesible mediante API con autenticación de empresa
- **Ambos**: El reporte puede ser tanto público como privado

### Crear Configuración de Reporte

1. Navegar a **Configuración → Configuraciones**
2. Click en **Nueva Configuración**
3. Completar información básica:
   - Nombre de la configuración
   - Tenant, Client, Workspace, Report
   - Usuario de Power BI
4. Configurar privacidad:
   - Marcar "Es Público" para permitir links públicos
   - Marcar "Es Privado" para acceso vía API
5. Guardar para continuar a edición

### Asociar Empresas a un Reporte

1. Editar una configuración existente
2. En la sección "Empresas Asociadas", seleccionar las empresas
3. Solo las empresas seleccionadas podrán acceder al reporte vía API
4. Guardar cambios

### Generar Links Públicos

Si el reporte es público:

1. Desde la lista de configuraciones, click en el ícono de link
2. Ingresar un slug personalizado (ej: `ventas-2024`)
3. Compartir el link generado: `https://tudominio.com/p/ventas-2024`

## Futuras Empresas

### ¿Qué son Futuras Empresas?

Sistema para gestionar solicitudes de nuevas empresas que aún no están aprobadas. Simula integración con un sistema externo.

### Workflow de Aprobación

#### 1. Recibir Empresas del Sistema Externo

- Navegar a **Futuras Empresas**
- Click en **Simular Consulta Externa**
- Se obtendrán empresas pendientes del "sistema externo"

#### 2. Revisar Empresa

- Click en el ícono de ojo para ver detalles completos
- Revisar información: nombre, CUIT, email, teléfono, etc.
- Ver datos adicionales si los hay

#### 3. Confirmar o Rechazar

**Para Confirmar**:
1. Click en el botón de confirmar (✓)
2. Opcionalmente agregar notas
3. Se creará automáticamente:
   - Empresa en el sistema
   - Credenciales de acceso
   - Notificación al sistema externo (simulada)

**Para Rechazar**:
1. Click en el botón de rechazar (✗)
2. Opcionalmente agregar motivo
3. Se notifica al sistema externo (simulada)

### Historial de Procesamiento

La sección inferior muestra las últimas 20 empresas procesadas con:
- Estado final (Confirmada/Rechazada)
- Usuario que procesó
- Fecha de procesamiento
- Notas agregadas

## API Privada

### Documentación Completa

Ver documentación interactiva en: `https://tudominio.com/docs`

### Endpoints Disponibles

#### 1. Autenticación

```bash
POST /private/login
Content-Type: application/json

{
  "client_id": "tu-client-id",
  "client_secret": "tu-client-secret"
}
```

**Respuesta**:
```json
{
  "access_token": "eyJhbGci...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

#### 2. Listar Reportes Disponibles

```bash
GET /private/reports
Authorization: Bearer {access_token}
```

**Respuesta**:
```json
{
  "empresa_id": 1,
  "empresa_nombre": "Mi Empresa SA",
  "reports": [
    {
      "config_id": 1,
      "config_name": "Dashboard Ventas",
      "report_id": "abc-123",
      "report_name": "Ventas 2024"
    }
  ]
}
```

#### 3. Obtener Configuración de Embed

```bash
GET /private/report-config?config_id=1
Authorization: Bearer {access_token}
```

**Respuesta**:
```json
{
  "embedUrl": "https://app.powerbi.com/reportEmbed?reportId=...",
  "reportId": "abc-123",
  "accessToken": "AAD-token...",
  "workspaceId": "xyz-789"
}
```

## Ejemplos de Integración

### JavaScript (React/Vue/Angular)

```javascript
// 1. Autenticación
async function authenticate() {
  const response = await fetch('https://api.tudominio.com/private/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      client_id: process.env.CLIENT_ID,
      client_secret: process.env.CLIENT_SECRET
    })
  });
  
  const { access_token } = await response.json();
  return access_token;
}

// 2. Listar reportes disponibles
async function getAvailableReports(token) {
  const response = await fetch('https://api.tudominio.com/private/reports', {
    headers: { 'Authorization': `Bearer ${token}` }
  });
  
  const data = await response.json();
  return data.reports;
}

// 3. Obtener configuración y embeber
async function embedReport(token, configId, containerId) {
  const response = await fetch(
    `https://api.tudominio.com/private/report-config?config_id=${configId}`,
    { headers: { 'Authorization': `Bearer ${token}` } }
  );
  
  const config = await response.json();
  
  // Usar Power BI JavaScript SDK
  const embedContainer = document.getElementById(containerId);
  const report = powerbi.embed(embedContainer, {
    type: 'report',
    id: config.reportId,
    embedUrl: config.embedUrl,
    accessToken: config.accessToken,
    tokenType: models.TokenType.Aad
  });
}

// Uso completo
async function main() {
  const token = await authenticate();
  const reports = await getAvailableReports(token);
  
  console.log('Reportes disponibles:', reports);
  
  // Embeber el primer reporte
  if (reports.length > 0) {
    await embedReport(token, reports[0].config_id, 'reportContainer');
  }
}
```

### Python

```python
import requests
import time

class PowerBIClient:
    def __init__(self, base_url, client_id, client_secret):
        self.base_url = base_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_expires = 0
    
    def authenticate(self):
        """Obtener token de acceso"""
        response = requests.post(
            f'{self.base_url}/private/login',
            json={
                'client_id': self.client_id,
                'client_secret': self.client_secret
            }
        )
        response.raise_for_status()
        
        data = response.json()
        self.token = data['access_token']
        self.token_expires = time.time() + data['expires_in']
        
        return self.token
    
    def _ensure_authenticated(self):
        """Verificar y renovar token si es necesario"""
        if not self.token or time.time() >= self.token_expires - 60:
            self.authenticate()
    
    def get_available_reports(self):
        """Listar reportes disponibles"""
        self._ensure_authenticated()
        
        response = requests.get(
            f'{self.base_url}/private/reports',
            headers={'Authorization': f'Bearer {self.token}'}
        )
        response.raise_for_status()
        
        return response.json()
    
    def get_report_config(self, config_id):
        """Obtener configuración de embed para un reporte"""
        self._ensure_authenticated()
        
        response = requests.get(
            f'{self.base_url}/private/report-config',
            headers={'Authorization': f'Bearer {self.token}'},
            params={'config_id': config_id}
        )
        response.raise_for_status()
        
        return response.json()

# Uso
if __name__ == '__main__':
    client = PowerBIClient(
        base_url='https://api.tudominio.com',
        client_id='tu-client-id',
        client_secret='tu-client-secret'
    )
    
    # Listar reportes
    reports = client.get_available_reports()
    print(f"Empresa: {reports['empresa_nombre']}")
    print(f"Reportes disponibles: {len(reports['reports'])}")
    
    for report in reports['reports']:
        print(f"  - {report['config_name']} (ID: {report['config_id']})")
    
    # Obtener configuración de un reporte específico
    if reports['reports']:
        config_id = reports['reports'][0]['config_id']
        config = client.get_report_config(config_id)
        
        print(f"\nConfiguración del reporte:")
        print(f"  Embed URL: {config['embedUrl']}")
        print(f"  Report ID: {config['reportId']}")
```

### C# (.NET)

```csharp
using System;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;

public class PowerBIClient
{
    private readonly HttpClient _httpClient;
    private readonly string _baseUrl;
    private readonly string _clientId;
    private readonly string _clientSecret;
    private string _accessToken;
    private DateTime _tokenExpires;

    public PowerBIClient(string baseUrl, string clientId, string clientSecret)
    {
        _httpClient = new HttpClient();
        _baseUrl = baseUrl;
        _clientId = clientId;
        _clientSecret = clientSecret;
    }

    public async Task AuthenticateAsync()
    {
        var loginData = new
        {
            client_id = _clientId,
            client_secret = _clientSecret
        };

        var content = new StringContent(
            JsonSerializer.Serialize(loginData),
            Encoding.UTF8,
            "application/json"
        );

        var response = await _httpClient.PostAsync(
            $"{_baseUrl}/private/login",
            content
        );

        response.EnsureSuccessStatusCode();
        var result = await JsonSerializer.DeserializeAsync<LoginResponse>(
            await response.Content.ReadAsStreamAsync()
        );

        _accessToken = result.access_token;
        _tokenExpires = DateTime.UtcNow.AddSeconds(result.expires_in);
    }

    private async Task EnsureAuthenticatedAsync()
    {
        if (string.IsNullOrEmpty(_accessToken) || 
            DateTime.UtcNow >= _tokenExpires.AddMinutes(-1))
        {
            await AuthenticateAsync();
        }
    }

    public async Task<ReportsResponse> GetAvailableReportsAsync()
    {
        await EnsureAuthenticatedAsync();

        var request = new HttpRequestMessage(
            HttpMethod.Get,
            $"{_baseUrl}/private/reports"
        );
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            _accessToken
        );

        var response = await _httpClient.SendAsync(request);
        response.EnsureSuccessStatusCode();

        return await JsonSerializer.DeserializeAsync<ReportsResponse>(
            await response.Content.ReadAsStreamAsync()
        );
    }

    public async Task<ReportConfig> GetReportConfigAsync(int configId)
    {
        await EnsureAuthenticatedAsync();

        var request = new HttpRequestMessage(
            HttpMethod.Get,
            $"{_baseUrl}/private/report-config?config_id={configId}"
        );
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            _accessToken
        );

        var response = await _httpClient.SendAsync(request);
        response.EnsureSuccessStatusCode();

        return await JsonSerializer.DeserializeAsync<ReportConfig>(
            await response.Content.ReadAsStreamAsync()
        );
    }
}

// DTOs
public class LoginResponse
{
    public string access_token { get; set; }
    public string token_type { get; set; }
    public int expires_in { get; set; }
}

public class ReportsResponse
{
    public int empresa_id { get; set; }
    public string empresa_nombre { get; set; }
    public ReportInfo[] reports { get; set; }
}

public class ReportInfo
{
    public int config_id { get; set; }
    public string config_name { get; set; }
    public string report_id { get; set; }
    public string report_name { get; set; }
}

public class ReportConfig
{
    public string embedUrl { get; set; }
    public string reportId { get; set; }
    public string accessToken { get; set; }
    public string workspaceId { get; set; }
}
```

## Mejores Prácticas

### Seguridad

1. **Nunca Exponer Credenciales**: Guardar `client_secret` en variables de entorno o gestores de secretos
2. **HTTPS Obligatorio**: Usar siempre HTTPS en producción
3. **Renovación de Tokens**: Implementar lógica para renovar tokens antes de expiración
4. **Rotación de Credenciales**: Cambiar credenciales periódicamente

### Rendimiento

1. **Caché de Tokens**: Reutilizar tokens mientras sean válidos
2. **Caché de Lista de Reportes**: La lista cambia raramente, cachear por algunos minutos
3. **Manejo de Errores**: Implementar reintentos con backoff exponencial

### Auditoría

1. **Logs de Acceso**: El sistema registra todos los accesos a reportes
2. **Empresas Inactivas**: Desactivar empresas en lugar de eliminar para mantener historial
3. **Notas en Futuras Empresas**: Documentar razones de aprobación/rechazo

## Solución de Problemas

### Error 401: Invalid Credentials

- Verificar que `client_id` y `client_secret` sean correctos
- Verificar que la empresa esté activa
- Las credenciales se muestran solo una vez al crear/regenerar

### Error 403: Configuration Not Private

- El reporte debe tener "Es Privado" marcado
- La empresa debe estar asociada al reporte
- Editar la configuración y agregar la empresa

### Error 404: Configuration Not Found

- Verificar que el `config_id` exista
- Usar endpoint `/private/reports` para listar IDs válidos

### Token Expira Rápido

- Tokens tienen vida de 1 hora por defecto
- Configurar `JWT_EXPIRATION` en `.env` para cambiar duración
- Implementar renovación automática de tokens
