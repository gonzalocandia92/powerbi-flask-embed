"""
Power BI integration utilities for embedding reports.
"""
import logging
import requests


def _get_access_token(report):
    """Obtain Azure AD access token using ROPC for a report's credentials."""
    workspace = report.workspace
    tenant = workspace.tenant
    client = tenant.client

    client_secret = client.get_secret()
    user_pbi = report.usuario_pbi.username
    pass_pbi = report.usuario_pbi.get_password()

    if not client_secret:
        raise RuntimeError("Client secret not available. Please save the secret in the client configuration.")
    if not user_pbi or not pass_pbi:
        raise RuntimeError("Power BI username or password not available.")

    logging.debug(
        f"Requesting Azure AD token — tenant: {tenant.tenant_id}, "
        f"client_id: {client.client_id}, user: {user_pbi}"
    )

    token_url = f"https://login.microsoftonline.com/{tenant.tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "password",
        "client_id": client.client_id,
        "client_secret": client_secret,
        "scope": "https://analysis.windows.net/powerbi/api/.default",
        "username": user_pbi,
        "password": pass_pbi
    }

    response = requests.post(token_url, data=data)
    if not response.ok:
        logging.error(
            f"Azure AD token request failed — status: {response.status_code}, "
            f"body: {response.text!r}"
        )
    response.raise_for_status()
    access_token = response.json().get("access_token")
    logging.debug("Azure AD access token obtained successfully")
    return access_token


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
    access_token = _get_access_token(report)

    workspace_id = report.workspace.workspace_id
    report_id = report.report_id

    report_url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(report_url, headers=headers)
    resp.raise_for_status()
    report_info = resp.json()
    logging.debug(f"Embed URL obtained: {report_info.get('embedUrl')}")

    embed_token = access_token
    embed_url = report_info["embedUrl"]

    return embed_token, embed_url, report_id


def refresh_dataset(report):
    """
    Trigger a semantic model (dataset) refresh for a Power BI report.

    Steps:
    1. Obtain access token via ROPC
    2. GET report info to extract datasetId
    3. POST to /datasets/{datasetId}/refreshes

    Args:
        report: Report model instance with relationships loaded

    Returns:
        dict: {"dataset_id": str, "status": "accepted"}

    Raises:
        RuntimeError: If credentials are missing
        requests.HTTPError: If any API call fails (includes 429 for quota exceeded)
    """
    access_token = _get_access_token(report)
    headers = {"Authorization": f"Bearer {access_token}"}
    workspace_id = report.workspace.workspace_id

    # Step 1: Get dataset_id from report info
    report_url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report.report_id}"
    logging.debug(f"Fetching report info for dataset_id — workspace: {workspace_id}, report: {report.report_id}")
    resp = requests.get(report_url, headers=headers)
    if not resp.ok:
        logging.error(
            f"Failed to fetch report info — status: {resp.status_code}, body: {resp.text!r}"
        )
    resp.raise_for_status()
    dataset_id = resp.json()["datasetId"]
    logging.debug(f"Dataset ID resolved: {dataset_id}")

    # Step 2: Trigger refresh
    refresh_url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/refreshes"
    logging.debug(f"Triggering dataset refresh — workspace: {workspace_id}, dataset: {dataset_id}")
    resp = requests.post(refresh_url, headers=headers, json={"notifyOption": "NoNotification"})
    if not resp.ok:
        logging.error(
            f"Dataset refresh request failed — status: {resp.status_code}, body: {resp.text!r}"
        )
    resp.raise_for_status()  # 202 = accepted, 429 = quota exceeded

    logging.debug(f"Dataset refresh accepted — dataset: {dataset_id}, HTTP status: {resp.status_code}")
    return {"dataset_id": dataset_id, "status": "accepted"}


def get_refresh_history(report, top=1):
    """
    Retrieve the refresh history for the semantic model (dataset) of a Power BI report.

    Args:
        report: Report model instance with relationships loaded
        top: Number of most-recent refresh entries to return (default: 1)

    Returns:
        list[dict]: Refresh history entries from Power BI API (newest first).
                    Empty list if none found.

    Raises:
        RuntimeError: If credentials are missing
        requests.HTTPError: If any API call fails
    """
    access_token = _get_access_token(report)
    headers = {"Authorization": f"Bearer {access_token}"}
    workspace_id = report.workspace.workspace_id

    # Resolve dataset_id from report info
    report_url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report.report_id}"
    logging.debug(f"Fetching report info for refresh history — workspace: {workspace_id}, report: {report.report_id}")
    resp = requests.get(report_url, headers=headers)
    if not resp.ok:
        logging.error(
            f"Failed to fetch report info for refresh history — status: {resp.status_code}, body: {resp.text!r}"
        )
    resp.raise_for_status()
    dataset_id = resp.json()["datasetId"]
    logging.debug(f"Dataset ID resolved for refresh history: {dataset_id}")

    # Fetch refresh history
    history_url = (
        f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}"
        f"/datasets/{dataset_id}/refreshes?$top={top}"
    )
    resp = requests.get(history_url, headers=headers)
    if not resp.ok:
        logging.error(
            f"Failed to fetch refresh history — status: {resp.status_code}, body: {resp.text!r}"
        )
    resp.raise_for_status()

    entries = resp.json().get("value", [])
    # Attach the resolved dataset_id to each entry for convenience
    for entry in entries:
        entry.setdefault("datasetId", dataset_id)

    return entries


# Backward-compatible alias
get_embed_for_config = get_embed_for_report
