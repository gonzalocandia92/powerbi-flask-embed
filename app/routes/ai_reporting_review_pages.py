"""HTML pages of the report-review workflow (inbox and detail) for staff.

Presentation wiring only: it authenticates (``staff_required``, HTML refusals redirect to the login), gathers
the few things a template cannot know (URL templates, the status vocabulary, the report list) and renders. All
behavior lives in the browser modules (``klara_review_*.js``) over the JSON API of ``ai_reporting_reviews``; no
rule, query or SQL is here. It is a blueprint of its own because the JSON API promises JSON refusals on every
route (a test enforces it) and pages refuse with redirects.

The single contract with the browser is ``review_ui_config``: URL templates (``{id}`` is a review id,
``{run_id}`` a run id, filled client-side), so the JS never hardcodes a path.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from flask import Blueprint, abort, render_template, url_for

from app.persistence.review_states import STATUSES
from app.services.reporting.review_contracts import MAX_PAGE_SIZE
from app.services.reporting.review_factory import preparation_lease_seconds
from app.utils.decorators import staff_required
from app.utils.request_args import valid_run_id

from .review_urls import template_url as _template_url

bp = Blueprint("ai_reporting_review_pages", __name__, url_prefix="/admin/ai-reporting")

PER_PAGE = 20

# Both screens are staff-only today. Named separately so opening the inbox read-only to other roles (an open
# product question) is a change in one line, as in the JSON API.
page_reader = staff_required


def review_ui_config(*, endpoints: Mapping[str, str], detail_url: str, inbox_url: str,
                     reports: Iterable[Mapping[str, Any]], slow_timeout_ms: int,
                     run_id: str | None = None) -> dict[str, Any]:
    """The object the review scripts read as ``window.KLARA_REVIEW``. Pure: callers pass the URLs in."""
    return {
        "endpoints": dict(endpoints),
        "detailUrl": detail_url,
        "inboxUrl": inbox_url,
        "statuses": list(STATUSES),
        "reports": [{"id": report["id"], "name": report["name"]} for report in reports],
        "perPage": min(PER_PAGE, MAX_PAGE_SIZE),
        "slowTimeoutMs": int(slow_timeout_ms),
        "runId": run_id,
    }


def _endpoints() -> dict[str, str]:
    api = "ai_reporting_reviews."
    return {
        "inbox": url_for(api + "list_reviews"),
        "runReview": _template_url(api + "run_review", run=True),
        "prepare": _template_url(api + "prepare_review", run=True),
        "submit": _template_url(api + "submit_review", run=True),
        "approve": _template_url(api + "approve_review", review=True),
        "reject": _template_url(api + "reject_review", review=True),
        "comments": _template_url(api + "add_review_comment", review=True),
        "events": _template_url(api + "review_events", review=True),
    }


def _reports() -> list[dict[str, Any]]:
    from app.services import ai_console
    return ai_console.klara_enabled_reports()


def _config(run_id: str | None = None) -> dict[str, Any]:
    return review_ui_config(
        endpoints=_endpoints(), detail_url=_template_url("ai_reporting_review_pages.detail_page", run=True),
        inbox_url=url_for("ai_reporting_review_pages.inbox_page"), reports=_reports(),
        slow_timeout_ms=int(preparation_lease_seconds() * 1000), run_id=run_id)


@bp.route("/reviews/ui", methods=["GET"])
@page_reader
def inbox_page():
    return render_template("admin/ai_reporting_reviews.html", review_config=_config())


@bp.route("/report-runs/<run_id>/review/ui", methods=["GET"])
@page_reader
def detail_page(run_id: str):
    run_id = valid_run_id(run_id)
    if run_id is None:
        abort(404)
    return render_template("admin/ai_reporting_review.html", review_config=_config(run_id), run_id=run_id)
