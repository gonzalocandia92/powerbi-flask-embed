"""Consumer-owned billing persistence for analytical report sections."""
from __future__ import annotations

from app import db
from app.models import Report
from app.services import ai_billing

from .contracts import ReportSection


def record_section_usage(report_id: int, section: ReportSection) -> None:
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
            ai_billing.record_ai_usage_event(report=report, metadata_json=metadata, **event)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
