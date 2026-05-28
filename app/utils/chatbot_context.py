"""
Utilities that expose app data as context for the KLARA chatbot.

These functions are the bridge between our Flask/DB world and the MCP server.
The other dev can call these from their MCP server (via HTTP or direct import)
to get report metadata, workspace IDs, and dataset identifiers — so Claude
always knows which data source it's working against.
"""
import os
from app.models import PublicLink, Report


def get_report_context(slug: str | None) -> str | None:
    """
    Given a public link slug, return a context string describing the active report.
    This is passed to Claude as part of the system prompt so it understands
    which dataset/workspace is currently open.

    Returns None if the slug is not found or not provided.
    """
    if not slug:
        return None

    link = PublicLink.query.filter_by(custom_slug=slug, is_active=True).first()
    if not link:
        return None

    report: Report = link.report
    workspace = report.workspace
    tenant = workspace.tenant

    lines = [
        f"Reporte activo: {report.name}",
        f"Workspace ID: {workspace.workspace_id}",
        f"Tenant ID: {tenant.tenant_id}",
    ]

    # Include the dataset ID override from env if set, otherwise note the report ID.
    dataset_id = os.getenv("CHATBOT_DATASET_ID") or report.report_id
    lines.append(f"Dataset ID: {dataset_id}")

    return "\n".join(lines)


def get_workspace_info(slug: str) -> dict | None:
    """
    Return workspace and dataset identifiers for a given slug.
    Intended for the MCP server to call so it knows WHERE to run DAX queries.

    Returns a dict like:
        {
            "workspace_id": "abc-123",
            "dataset_id": "xyz-456",   # from CHATBOT_DATASET_ID or report_id
            "tenant_id": "def-789",
            "report_name": "Ventas Q1",
        }
    or None if slug not found.
    """
    link = PublicLink.query.filter_by(custom_slug=slug, is_active=True).first()
    if not link:
        return None

    report: Report = link.report
    workspace = report.workspace
    tenant = workspace.tenant

    return {
        "workspace_id": workspace.workspace_id,
        "dataset_id": os.getenv("CHATBOT_DATASET_ID") or report.report_id,
        "tenant_id": tenant.tenant_id,
        "report_name": report.name,
    }


def get_all_active_reports() -> list[dict]:
    """
    Return metadata for all reports that have at least one active public link.
    Useful for the MCP server to enumerate available datasets.
    """
    links = PublicLink.query.filter_by(is_active=True).all()
    seen = set()
    result = []
    for link in links:
        r = link.report
        if r.id in seen:
            continue
        seen.add(r.id)
        result.append({
            "report_name": r.name,
            "report_id": r.report_id,
            "workspace_id": r.workspace.workspace_id,
            "slug": link.custom_slug,
        })
    return result
