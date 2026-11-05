"""JSON shapes and request parsing of the schedules admin API (pure: no Flask, no database).

Datetimes leave as ISO-8601 UTC (``...+00:00``); the schedule's local wall time is sent next to them where a
person reads it, so the browser never converts timezones.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any, Mapping

from app.services.reporting.delivery_config import (
    parse_whatsapp_recipients, recipients_from_config, with_whatsapp_recipients,
)
from app.services.reporting.schedule_contracts import (
    INVALID_RULE, OccurrenceView, ScheduleSpec, ScheduleValidationError, ScheduleView,
)
from app.services.reporting.heartbeat import SchedulerHealth
from app.services.reporting.schedule_history import HistoryItem, ScheduleLabels
from app.services.reporting.schedule_service import PreviewItem

SPEC_FIELDS = ("name", "frequency", "weekday", "day_of_month", "local_time", "timezone", "freshness_policy",
               "misfire_policy", "refresh_deadline_hours", "misfire_grace_hours", "whatsapp_recipients")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_int(data: Mapping[str, Any], name: str) -> int | None:
    value = data.get(name)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ScheduleValidationError(f"«{name}» debe ser un número entero.", code=INVALID_RULE)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ScheduleValidationError(f"«{name}» debe ser un número entero.", code=INVALID_RULE) from None


def _local_time(value: Any) -> time:
    try:
        parsed = time.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is not None:
        raise ScheduleValidationError("La hora debe tener el formato HH:MM.", code=INVALID_RULE)
    return parsed.replace(second=0, microsecond=0)


def parse_spec(data: Any) -> ScheduleSpec:
    """Request body -> validated ``ScheduleSpec`` (raises ``ScheduleValidationError`` with a stable code)."""
    if not isinstance(data, Mapping):
        raise ScheduleValidationError("Se requiere un objeto JSON.", code="invalid_request")
    values: dict[str, Any] = {
        "name": data.get("name") if isinstance(data.get("name"), str) else "",
        "frequency": data.get("frequency"),
        "weekday": _optional_int(data, "weekday"),
        "day_of_month": _optional_int(data, "day_of_month"),
        "local_time": _local_time(data.get("local_time")),
        "timezone": data.get("timezone"),
        "refresh_deadline_hours": _optional_int(data, "refresh_deadline_hours"),
        "misfire_grace_hours": _optional_int(data, "misfire_grace_hours"),
        # The whole config is rebuilt from the request: today it only holds the delivery recipients.
        "config": with_whatsapp_recipients({}, parse_whatsapp_recipients(data.get("whatsapp_recipients"))),
    }
    for name in ("freshness_policy", "misfire_policy"):
        if data.get(name):
            values[name] = data[name]
    return ScheduleSpec(**values)


def spec_payload(spec: ScheduleSpec) -> dict[str, Any]:
    return {
        "name": spec.name, "frequency": spec.frequency, "weekday": spec.weekday, "day_of_month": spec.day_of_month,
        "local_time": spec.local_time.strftime("%H:%M"), "timezone": spec.timezone,
        "freshness_policy": spec.freshness_policy, "misfire_policy": spec.misfire_policy,
        "refresh_deadline_hours": spec.refresh_deadline_hours, "misfire_grace_hours": spec.misfire_grace_hours,
        "whatsapp_recipients": list(recipients_from_config(spec.config)),
    }


def preview_payload(item: PreviewItem) -> dict[str, Any]:
    period = item.period
    return {
        "scheduled_for": _iso(item.scheduled_for), "scheduled_local": item.local.strftime("%Y-%m-%dT%H:%M"),
        "period": {"label": period.label, "start": period.local_start.isoformat(),
                   "end": period.local_end.isoformat(), "comparison_label": period.comparison_label},
    }


def schedule_payload(schedule: ScheduleView, labels: ScheduleLabels | None = None, *,
                     upcoming: list[PreviewItem] | None = None,
                     status_counts: Mapping[str, int] | None = None) -> dict[str, Any]:
    payload = {
        "id": schedule.id, "definition_id": schedule.definition_id, **spec_payload(schedule.spec),
        "is_active": schedule.is_active, "next_run_at": _iso(schedule.next_run_at),
        # The same instant on the schedule's own wall clock (what the admin configured), not the browser's.
        "next_run_local": (schedule.next_run_at.astimezone(schedule.rule.tz).strftime("%Y-%m-%dT%H:%M")
                           if schedule.next_run_at is not None else None),
        "created_by_user_id": schedule.created_by_user_id,
        "created_at": _iso(schedule.created_at), "updated_at": _iso(schedule.updated_at),
        "definition_name": labels.definition_name if labels else None,
        "report_id": labels.report_id if labels else None,
        "report_name": labels.report_name if labels else None,
    }
    if upcoming is not None:
        payload["upcoming"] = [preview_payload(item) for item in upcoming]
    if status_counts is not None:
        payload["status_counts"] = dict(status_counts)
    return payload


def occurrence_payload(occurrence: OccurrenceView) -> dict[str, Any]:
    snapshot = occurrence.definition_snapshot if isinstance(occurrence.definition_snapshot, dict) else {}
    period = snapshot.get("period") if isinstance(snapshot.get("period"), dict) else {}
    return {
        "id": occurrence.id, "schedule_id": occurrence.schedule_id, "status": occurrence.status,
        "trigger_source": occurrence.trigger_source, "scheduled_for": _iso(occurrence.scheduled_for),
        "period": {"label": period.get("label"), "start": period.get("local_start"), "end": period.get("local_end"),
                   "start_utc": _iso(occurrence.period_start), "end_exclusive_utc": _iso(occurrence.period_end_exclusive)},
        "refresh_checked_at": _iso(occurrence.refresh_checked_at),
        "refresh_completed_at": _iso(occurrence.refresh_completed_at),
        "refresh_deadline_at": _iso(occurrence.refresh_deadline_at), "next_check_at": _iso(occurrence.next_check_at),
        "check_attempts": occurrence.check_attempts,
        "error": ({"code": occurrence.last_error_code, "message": occurrence.last_error_message}
                  if occurrence.last_error_code else None),
        "created_at": _iso(occurrence.created_at), "updated_at": _iso(occurrence.updated_at),
    }


def history_item_payload(item: HistoryItem, *, retryable: bool) -> dict[str, Any]:
    payload = occurrence_payload(item.occurrence)
    run = item.run
    payload["run"] = None if run is None else {
        "id": run.id, "status": run.status, "error_code": run.error_code, "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at), "completed_at": _iso(run.completed_at),
        "duration_seconds": run.duration_seconds, "cost_usd": run.cost_usd,
        "review": {"id": run.review_id, "status": run.review_status} if run.review_id else None,
    }
    payload["run_attempts"] = item.attempts
    payload["can_retry"] = retryable
    return payload


def scheduler_health_payload(health: SchedulerHealth) -> dict[str, Any]:
    """Liveness of the report-scheduler process, as shown above the schedules list."""
    return {
        "status": health.status, "stale": health.stale, "last_seen_at": _iso(health.last_seen_at),
        "age_seconds": health.age_seconds, "stale_after_seconds": health.stale_after_seconds,
        "failed_phases": list(health.failed_phases), "host": health.host,
    }
