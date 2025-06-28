# Power BI Flask Embed
Aplicación Flask para embeber reportes de Power BI usando autenticación con ROPC (Resource Owner Password Credentials) y la API REST de Power BI.

---

## Características

- Autenticación usando el flujo ROPC (Resource Owner Password Credentials)
- Visualización de reportes privados embebidos en un entorno Flask
- Acceso AAD con permisos delegados
- Registro detallado con `logging` para depuración
- Preparado para agregar soporte SSL (Pendiente)


---

## Requisitos

- Docker
- Python 3.11+
- Cuenta con licencia Power BI Pro
- Cuenta Azure AD con permisos
- Registro de una aplicación en Azure AD con los permisos necesarios configurados:
- Aplicación registrada en Azure AD con permisos delegados:
  - `Report.Read.All`
  - `Dataset.Read.All`
  - `Workspace.Read.All`
- Variables de entorno definidas en un archivo `.env`

---

## Instalación

### 1. Clonar el repositorio

```bash
git clone https://github.com/tu_usuario/powerbi-flask-embed.git
cd powerbi-flask-embed
```

### 2. Configurar Variables de entorno (`.env`)

```env
TENANT_ID=tu_tenant_id_aqui
CLIENT_ID=tu_client_id_aqui
CLIENT_SECRET=tu_client_secret_aqui
USER=tu_usuario_email_aqui
PASS=tu_password_aqui
WORKSPACE_ID=tu_workspace_id_aqui
REPORT_ID=tu_report_id_aqui
```

### 2. Crear y ejecutar el contenedor
```bash
docker-compose up --build
```

---

## Configuración en Azure y Power BI

### 1. **Registro de Aplicación en Azure AD:**

   - Crear una aplicación en [Azure Portal](https://portal.azure.com).
   - Configurar permisos API para Power BI:
     - `Dataset.Read.All`
     - `Report.Read.All`
     - `Workspace.Read.All`
   - En la sección "Authentication", permitir el flujo "Resource Owner Password Credentials" (ROPC).
   - Obtener y guardar los siguientes datos:
     - `TENANT_ID` (ID del directorio)
     - `CLIENT_ID` (ID de la aplicación)
     - `CLIENT_SECRET` (secreto cliente)

### 2. **Power BI:**

   - Obtener:
     - `WORKSPACE_ID` (ID del grupo o espacio de trabajo)
     - `REPORT_ID` (ID del reporte a embeber)
   - Asegurar que el usuario (`USER`) tenga licencia Power BI Pro y permisos para acceder al reporte.


---


## Pendientes

- Integración de certificados SSL
- Encriptado del token renderizado en html
