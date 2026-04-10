"""
Power BI integration utilities for embedding reports.
"""
import logging
import requests


def get_embed_for_report(report):
    """
    Obtain embed token and URL for a Power BI report using ROPC authentication.

    Traverses the model hierarchy: Report → Workspace → Tenant → Client

    Args:
        report: Report object with relationships loaded

    Returns:
        tuple: (embed_token, embed_url, report_id)

    Raises:
        RuntimeError: If required credentials are not available
        requests.HTTPError: If API requests fail
    """
    workspace = report.workspace
    tenant = workspace.tenant
    client = tenant.client

    tenant_id = tenant.tenant_id
    client_id = client.client_id
    client_secret = client.get_secret()

    user_pbi = report.usuario_pbi.username
    pass_pbi = report.usuario_pbi.get_password()

    workspace_id = workspace.workspace_id
    report_id = report.report_id

    if not client_secret:
        raise RuntimeError("Client secret not available. Please save the secret in the client configuration.")

    if not user_pbi or not pass_pbi:
        raise RuntimeError("Power BI username or password not available.")

    logging.debug(f"Obtaining token for tenant: {tenant_id}, client: {client_id}, user: {user_pbi}")

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://analysis.windows.net/powerbi/api/.default",
        "username": user_pbi,
        "password": pass_pbi
    }

    response = requests.post(token_url, data=data)
    response.raise_for_status()
    access_token = response.json().get("access_token")
    logging.debug("Access token received successfully")

    report_url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(report_url, headers=headers)
    resp.raise_for_status()
    report_info = resp.json()
    logging.debug(f"Embed URL obtained: {report_info.get('embedUrl')}")

    embed_token = access_token
    embed_url = report_info["embedUrl"]

    return embed_token, embed_url, report_id


# Backward-compatible alias
get_embed_for_config = get_embed_for_report
