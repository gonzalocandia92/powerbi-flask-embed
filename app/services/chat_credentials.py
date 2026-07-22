"""Resolve per-report credentials for the chat flow."""
from __future__ import annotations

from typing import Dict, List

from app.models import Report


def resolve_powerbi_env_for_report(report: Report) -> Dict[str, str]:
    """Build strict DB-derived credentials from the report's relationships.

    Raises RuntimeError if any required credential is missing.
    """
    if report is None:
        raise RuntimeError("Report is required to resolve credentials")

    workspace = report.workspace
    tenant = workspace.tenant if workspace else None
    client = tenant.client if tenant else None
    usuario_pbi = report.usuario_pbi

    tenant_id = tenant.tenant_id if tenant else None
    workspace_id = workspace.workspace_id if workspace else None
    client_id = client.client_id if client else None
    client_secret = client.get_secret() if client else None
    username = usuario_pbi.username if usuario_pbi else None
    password = usuario_pbi.get_password() if usuario_pbi else None

    missing: List[str] = []
    values = {
        "TENANT_ID": tenant_id,
        "WORKSPACE_ID": workspace_id,
        "CLIENT_ID": client_id,
        "CLIENT_SECRET": client_secret,
        "USER": username,
        "PASS": password,
    }
    for key, value in values.items():
        if not value or not str(value).strip():
            missing.append(key)

    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Missing credentials in DB: {joined}")

    return {
        "TENANT_ID": str(tenant_id),
        "WORKSPACE_ID": str(workspace_id),
        "CLIENT_ID": str(client_id),
        "CLIENT_SECRET": str(client_secret),
        "USER": str(username),
        "PASS": str(password),
    }


resolve_mcp_env_for_report = resolve_powerbi_env_for_report
