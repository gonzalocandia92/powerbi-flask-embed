# MCP Aklara service

Remote FastMCP service for Power BI. It exposes OAuth-protected tools at
`/mcp` and keeps the `/{mcp-key-*}/sse` route during the legacy migration.

The OAuth flow delegates authorization, model grants, auditing, and Power BI
token acquisition to the Flask broker. Direct PostgreSQL and service-principal
credentials are used only by the legacy API-key route.

`MCP_PUBLIC_URL` also defines the trusted HTTP host and origin. Add any proxy
hostnames to `MCP_ALLOWED_HOSTS` and browser origins to `MCP_ALLOWED_ORIGINS`
as comma-separated values.

## Local development

Use Python 3.11 from the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements\dev.txt
python services\mcp_aklara\scripts\install_amo.py
python -m uvicorn services.mcp_aklara.src.main:app --host 0.0.0.0 --port 8000
```

The AMO installer requires network access and .NET Runtime 10 must be installed
on the host for local TOM/XMLA operations. The health endpoint returns 503 when
the TOM runtime cannot be loaded, so deployments do not accept traffic without
their XMLA dependency.

## Deployment

The root `docker-compose.yml` builds this service with Python 3.11, installs
.NET Runtime 10 and AMO 19.114.8, and connects it to Flask over the private
broker network. The external reverse proxy must publish `/mcp`, block
`/internal/mcp/*`, and strip untrusted client-certificate headers.
