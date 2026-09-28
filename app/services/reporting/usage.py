"""Consumer-owned billing persistence for analytical report sections."""
from __future__ import annotations

from app import db
from app.models import Report
from app.services import ai_billing

from .contracts import ReportSection


def record_section_usage(report_id: int, section: ReportSection) -> None:
    """Persist a ``ReportGenerator`` (origin="definition") analysis section.

    Coordinator-requested extra analyses are recorded with
    ``record_extra_analysis_usage`` instead: same ledger table, but tagged
    ``report_stage="extra_analysis"`` and ``origin="coordinator"`` so they are
    never confused with the mandatory ``ReportDefinition`` questions.
    """
    report = db.session.get(Report, report_id)
    if report is None:
        raise ValueError(f"Report not found: {report_id}")
    try:
        for raw in section.ai_usage_events:
            event = dict(raw)
            metadata = dict(event.pop("metadata_json", None) or {})
            metadata.update({
                "execution_source": "report",
                "report_stage": "analysis",
                "report_section_key": section.key,
            })
            if section.report_run_id:
                metadata["report_run_id"] = section.report_run_id
            ai_billing.record_ai_usage_event(report=report, metadata_json=metadata, **event)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def record_extra_analysis_usage(report_id: int, section: ReportSection) -> None:
    """Persist one coordinator-requested extra analysis (``origin="coordinator"``).

    Reuses the exact same ``AnalyticsExecutor`` billing events as
    ``record_section_usage`` (same ledger, same normal AnalyticsExecutor billing
    flow per V1.1 spec §17) but keeps ``report_stage="extra_analysis"`` distinct
    from ``analysis`` so admin observability never mixes mandatory and
    coordinator-originated analyses.
    """
    report = db.session.get(Report, report_id)
    if report is None:
        raise ValueError(f"Report not found: {report_id}")
    try:
        for raw in section.ai_usage_events:
            event = dict(raw)
            metadata = dict(event.pop("metadata_json", None) or {})
            metadata.update({
                "execution_source": "report",
                "report_stage": "extra_analysis",
                "report_section_key": section.key,
                "origin": section.origin,
                "purpose": section.purpose,
                "related_section_keys": list(section.related_section_keys),
            })
            if section.report_run_id:
                metadata["report_run_id"] = section.report_run_id
            ai_billing.record_ai_usage_event(report=report, metadata_json=metadata, **event)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def record_writer_usage(report_id: int, report_run_id: str, event: dict) -> None:
    """Persist one ``report_writer`` generation in the AI usage ledger."""
    report = db.session.get(Report, report_id)
    if report is None:
        raise ValueError(f"Report not found: {report_id}")
    try:
        payload = dict(event)
        metadata = dict(payload.pop("metadata_json", None) or {})
        metadata.update({
            "execution_source": "report",
            "report_stage": "writing",
            "component": "report_writer",
            "report_run_id": report_run_id,
        })
        ai_billing.record_ai_usage_event(report=report, metadata_json=metadata, **payload)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def record_coordinator_usage(report_id: int, report_run_id: str, event: dict) -> None:
    """Persist one ``report_coordinator`` generation in the AI usage ledger.

    Distinct ``report_stage="coordination"`` and ``component="report_coordinator"``
    keep this out of ``main_agent`` and ``report_writer`` observability, per
    V1.1 spec §16.
    """
    report = db.session.get(Report, report_id)
    if report is None:
        raise ValueError(f"Report not found: {report_id}")
    try:
        payload = dict(event)
        metadata = dict(payload.pop("metadata_json", None) or {})
        metadata.update({
            "execution_source": "report",
            "report_stage": "coordination",
            "component": "report_coordinator",
            "report_run_id": report_run_id,
        })
        ai_billing.record_ai_usage_event(report=report, metadata_json=metadata, **payload)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
