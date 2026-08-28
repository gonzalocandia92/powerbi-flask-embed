"""Legacy service-principal token acquisition for API-key connectors."""

import os

import msal


CLIENT_ID = os.getenv("POWERBI_CLIENT_ID")
CLIENT_SECRET = os.getenv("POWERBI_CLIENT_SECRET")
TENANT_ID = os.getenv("POWERBI_TENANT_ID")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["https://analysis.windows.net/powerbi/api/.default"]


def get_powerbi_access_token() -> str:
    if not all([CLIENT_ID, CLIENT_SECRET, TENANT_ID]):
        raise ValueError(
            "Missing POWERBI_CLIENT_ID, POWERBI_CLIENT_SECRET, or "
            "POWERBI_TENANT_ID for the legacy MCP route."
        )

    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )
    result = app.acquire_token_silent(SCOPES, account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=SCOPES)
    if "access_token" in result:
        return result["access_token"]
    error = result.get(
        "error_description", result.get("error", "Unknown Power BI token error")
    )
    raise RuntimeError(f"Could not obtain a Power BI access token: {error}")
