"""Scheduled reports (Phase 3): admin page + JSON API to configure schedules and follow their occurrences.

A thin adapter: it authenticates, parses (``schedule_payloads``), calls ``ScheduleService`` / ``ScheduleHistory``
and shapes the answer. No rule and no SQL here. Managing schedules is for administrators, like report
definitions (``ai_reporting``); run-now and retry may spend AI tokens, so they are admin actions too.

API refusals are JSON (``401`` logged out, ``403`` not admin), never a redirect a ``fetch`` would follow; the
page redirects like the other admin screens. Every API view is wrapped by ``admin_api`` (a test enforces it).
"""
from __future__ import annotations

import logging
import zoneinfo
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, jsonify, render_template, request, url_for
from flask_login import current_user, login_required

from app.persistence.schedule_states import (
    DEFAULT_FRESHNESS_POLICY, DEFAULT_MISFIRE_POLICY, FRESHNESS_POLICIES, MISFIRE_POLICIES, OCCURRENCE_STATUSES,
    RETRYABLE_OCCURRENCE_STATUSES,
)
from app.services.reporting.schedule_contracts import (
    DEFINITION_NOT_FOUND, ScheduleConflictError, ScheduleError, ScheduleNotFoundError, ScheduleValidationError,
)
from app.services.reporting.schedule_rules import MONTHLY, WEEKLY
from app.services.reporting.scheduler_factory import (
    build_schedule_history, build_schedule_service, scheduler_health,
)
from app.utils.decorators import admin_required
from app.utils.request_args import int_arg

from .review_urls import template_url
from .schedule_payloads import (
    history_item_payload, occurrence_payload, parse_spec, preview_payload, schedule_payload, scheduler_health_payload,
)

LOG = logging.getLogger(__name__)

bp = Blueprint("ai_reporting_schedules", __name__, url_prefix="/admin/ai-reporting")

ACCESS_MARK = "__access__"
ACCESS_ADMIN_API = "admin_api"
HISTORY_PER_PAGE = 20
HISTORY_PER_PAGE_MAX = 100
UPCOMING_COUNT = 5
_ID_SENTINEL = 987654321  # an int (the converters are ``int``) that cannot collide with a path segment


def _error(message: str, code: str, status: int):
    response = jsonify({"error": message, "code": code})
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def admin_api(view):
    """Administrators only, refusing as JSON. Also turns schedule errors into their HTTP answer."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return _error("Iniciá sesión para continuar.", "login_required", 401)
        if not current_user.is_admin:
            return _error("Se requieren permisos de administrador.", "admin_required", 403)
        try:
            return view(*args, **kwargs)
        except ScheduleNotFoundError as exc:
            return _error(str(exc), exc.code, 404)
        except ScheduleValidationError as exc:
            return _error(str(exc), exc.code, 404 if exc.code == DEFINITION_NOT_FOUND else 400)
        except ScheduleConflictError as exc:
            return _error(str(exc), exc.code, 409)
        except ScheduleError as exc:
            return _error(str(exc), exc.code, 400)
        except Exception:  # noqa: BLE001 - never leak internals; the log has the detail
            LOG.exception("[ReportScheduler] admin API %s failed", view.__name__)
            return _error("No se pudo completar la operación.", "internal_error", 500)
    setattr(wrapper, ACCESS_MARK, ACCESS_ADMIN_API)
    return wrapper


def _json(payload, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ScheduleValidationError("Se requiere un objeto JSON.", code="invalid_request")
    return data


def _full_payload(service, history, schedule):
    labels = history.labels([schedule.id]).get(schedule.id)
    upcoming = service.preview(schedule.rule, count=UPCOMING_COUNT) if schedule.is_active else []
    return schedule_payload(schedule, labels, upcoming=upcoming,
                            status_counts=service.store.count_occurrences(schedule.id))


# ── schedules ─────────────────────────────────────────────────────────────────────────────────

@bp.route("/schedules", methods=["GET"])
@admin_api
def list_schedules():
    service, history = build_schedule_service(), build_schedule_history()
    schedules = service.store.list_schedules(definition_id=request.args.get("definition_id", type=int))
    labels = history.labels([s.id for s in schedules])
    # ``scheduler``: is the report-scheduler process alive? (the page warns when it is not)
    return _json({"items": [schedule_payload(s, labels.get(s.id)) for s in schedules],
                  "scheduler": scheduler_health_payload(scheduler_health(datetime.now(timezone.utc)))})


@bp.route("/schedules", methods=["POST"])
@admin_api
def create_schedule():
    data = _body()
    definition_id = data.get("definition_id")
    if not isinstance(definition_id, int) or isinstance(definition_id, bool):
        raise ScheduleValidationError("Elegí una definición de informe.", code="definition_required")
    service = build_schedule_service()
    schedule = service.create(definition_id, parse_spec(data), created_by_user_id=current_user.id,
                              active=data.get("is_active", True) is not False)
    return _json(_full_payload(service, build_schedule_history(), schedule), 201)


@bp.route("/schedules/<int:schedule_id>", methods=["GET"])
@admin_api
def get_schedule(schedule_id: int):
    service = build_schedule_service()
    return _json(_full_payload(service, build_schedule_history(), service.get(schedule_id)))


@bp.route("/schedules/<int:schedule_id>", methods=["PUT"])
@admin_api
def update_schedule(schedule_id: int):
    service = build_schedule_service()
    schedule = service.update(schedule_id, parse_spec(_body()))
    return _json(_full_payload(service, build_schedule_history(), schedule))


@bp.route("/schedules/<int:schedule_id>/pause", methods=["POST"])
@admin_api
def pause_schedule(schedule_id: int):
    service = build_schedule_service()
    return _json(_full_payload(service, build_schedule_history(), service.pause(schedule_id)))


@bp.route("/schedules/<int:schedule_id>/resume", methods=["POST"])
@admin_api
def resume_schedule(schedule_id: int):
    service = build_schedule_service()
    return _json(_full_payload(service, build_schedule_history(), service.resume(schedule_id)))


@bp.route("/schedules/preview", methods=["POST"])
@admin_api
def preview_schedule():
    """Next dates and analysed periods of a configuration that is not saved yet (live form preview)."""
    spec = parse_spec(_body())
    count = int_arg("count", UPCOMING_COUNT, maximum=12)
    return _json({"items": [preview_payload(item) for item in build_schedule_service().preview(spec.rule(), count=count)]})


# ── occurrences ───────────────────────────────────────────────────────────────────────────────

@bp.route("/schedules/<int:schedule_id>/occurrences", methods=["GET"])
@admin_api
def list_occurrences(schedule_id: int):
    build_schedule_service().get(schedule_id)  # 404 for an unknown schedule
    per_page = int_arg("per_page", HISTORY_PER_PAGE, maximum=HISTORY_PER_PAGE_MAX)
    page = int_arg("page", 1)
    result = build_schedule_history().page(schedule_id, limit=per_page, offset=(page - 1) * per_page)
    return _json({
        "items": [history_item_payload(item, retryable=item.occurrence.status in RETRYABLE_OCCURRENCE_STATUSES)
                  for item in result.items],
        "status_counts": result.status_counts,
        "page": page, "per_page": per_page, "total": result.total, "pages": max(1, -(-result.total // per_page)),
    })


@bp.route("/schedules/<int:schedule_id>/run-now", methods=["POST"])
@admin_api
def run_schedule_now(schedule_id: int):
    """A MANUAL occurrence now: same flow as a scheduled one (freshness, caps, review)."""
    occurrence = build_schedule_service().run_now(schedule_id)
    LOG.info("[ReportScheduler] run-now schedule=%s occurrence=%s by user=%s", schedule_id, occurrence.id,
             current_user.id)
    return _json(occurrence_payload(occurrence), 202)


@bp.route("/schedule-occurrences/<int:occurrence_id>/retry", methods=["POST"])
@admin_api
def retry_occurrence(occurrence_id: int):
    """Re-open an expired / blocked / failed occurrence (a failed one gets a NEW run: the UI confirms)."""
    occurrence = build_schedule_service().retry(occurrence_id)
    LOG.info("[ReportScheduler] retry occurrence=%s by user=%s", occurrence_id, current_user.id)
    return _json(occurrence_payload(occurrence), 202)


# ── page ──────────────────────────────────────────────────────────────────────────────────────

def schedules_ui_config() -> dict:
    """``window.KLARA_SCHEDULES``: URL templates (``{id}``) and vocabularies; the JS never hardcodes a path."""
    api = "ai_reporting_schedules."
    review_detail = None
    try:
        review_detail = template_url("ai_reporting_review_pages.detail_page", run=True)
    except Exception:  # noqa: BLE001 - the review pages are optional for this screen
        review_detail = None
    def by_id(endpoint: str, param: str = "schedule_id") -> str:
        return url_for(api + endpoint, **{param: _ID_SENTINEL}).replace(str(_ID_SENTINEL), "{id}")

    return {
        "endpoints": {
            "list": url_for(api + "list_schedules"),
            "create": url_for(api + "create_schedule"),
            "preview": url_for(api + "preview_schedule"),
            "schedule": by_id("get_schedule"),
            "pause": by_id("pause_schedule"),
            "resume": by_id("resume_schedule"),
            "occurrences": by_id("list_occurrences"),
            "runNow": by_id("run_schedule_now"),
            "retry": by_id("retry_occurrence", "occurrence_id"),
        },
        "runUrl": url_for("ai_reporting.page") + "?run_id={run_id}",
        "reviewUrl": review_detail,
        "frequencies": [WEEKLY, MONTHLY],
        "freshnessPolicies": list(FRESHNESS_POLICIES),
        "misfirePolicies": list(MISFIRE_POLICIES),
        "defaultFreshnessPolicy": DEFAULT_FRESHNESS_POLICY,
        "defaultMisfirePolicy": DEFAULT_MISFIRE_POLICY,
        "occurrenceStatuses": list(OCCURRENCE_STATUSES),
        "definitions": build_schedule_history().definition_options(),
        "timezones": sorted(zoneinfo.available_timezones()),
        "defaultTimezone": "America/Argentina/Buenos_Aires",
        "perPage": HISTORY_PER_PAGE,
    }


@bp.app_context_processor
def schedules_navigation():
    """The AI console links here only when this blueprint is registered, and only for administrators."""
    try:
        visible = bool(current_user and current_user.is_authenticated and current_user.is_admin)
    except Exception:  # noqa: BLE001 - outside a login context (e.g. emails) there is no nav
        visible = False
    return {"klara_schedules_nav_url": url_for("ai_reporting_schedules.schedules_page") if visible else None}


@bp.route("/schedules/ui", methods=["GET"])
@login_required
@admin_required
def schedules_page():
    return render_template("admin/ai_reporting_schedules.html", schedules_config=schedules_ui_config())
