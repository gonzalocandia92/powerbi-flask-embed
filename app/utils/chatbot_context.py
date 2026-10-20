"""
Utilities that expose app data as context for the KLARA chatbot.

These functions are the bridge between our Flask/DB world and the MCP server.
They resolve report metadata, workspace IDs, and dataset identifiers.

Identity resolution is fail-closed: when the dataset of a report cannot be
resolved, ``DatasetResolutionError`` is raised. There is intentionally no
fallback to a process-wide dataset (the former ``CHATBOT_DATASET_ID``) nor to
the Power BI report GUID, because that silently pointed a report at the
dataset of another one.
"""
from typing import Optional, Tuple

from app.models import PublicLink, Report
from app.utils.powerbi import get_current_dataset_id


class DatasetResolutionError(RuntimeError):
    """Raised when the dataset of a report cannot be resolved."""


def _resolve_dataset_id(report: Report) -> str:
    try:
        dataset_id = get_current_dataset_id(report)
    except Exception as exc:
        raise DatasetResolutionError(
            f"Dataset could not be resolved for report {report.id}"
        ) from exc
    if not dataset_id:
        raise DatasetResolutionError(f"Dataset could not be resolved for report {report.id}")
    return dataset_id


def get_report_context(slug: str | None) -> str | None:
    """
    Given a public link slug, return a context string describing the active report.
    This is passed to Claude as part of the system prompt so it understands
    which dataset/workspace is currently open.

    Returns None if the slug is not found or not provided.
    Raises DatasetResolutionError if the dataset cannot be resolved.
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
        f"Dataset ID: {_resolve_dataset_id(report)}",
    ]

    return "\n".join(lines)


def get_workspace_info(slug: str) -> dict | None:
    """
    Return workspace and dataset identifiers for a given slug.
    Intended for the MCP server to call so it knows WHERE to run DAX queries.

    Returns a dict like:
        {
            "workspace_id": "abc-123",
            "dataset_id": "xyz-456",
            "tenant_id": "def-789",
            "report_name": "Ventas Q1",
        }
    or None if slug not found. Raises DatasetResolutionError if the dataset
    cannot be resolved.
    """
    link = PublicLink.query.filter_by(custom_slug=slug, is_active=True).first()
    if not link:
        return None

    report: Report = link.report
    workspace = report.workspace
    tenant = workspace.tenant

    return {
        "workspace_id": workspace.workspace_id,
        "dataset_id": _resolve_dataset_id(report),
        "tenant_id": tenant.tenant_id,
        "report_name": report.name,
    }


def get_report_and_dataset_by_slug(slug: str) -> Optional[Tuple[Report, str]]:
    """Resolve a public report and its dataset_id by slug."""
    if not slug:
        return None

    link = PublicLink.query.filter_by(custom_slug=slug, is_active=True).first()
    if not link:
        return None

    report: Report = link.report
    return report, _resolve_dataset_id(report)


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
