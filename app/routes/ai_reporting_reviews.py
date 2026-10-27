"""Review and approval of generated reports (JSON API): staff approve or reject ONE persisted PDF.

A thin adapter over ``ReviewService``: it authenticates (``staff_required``, JSON refusals), parses the request,
calls the service and translates the answer. Rules live in the service, error->status in ``review_http``,
shapes in ``review_payloads``, and the SQL in the factory's adapters; nothing here imports SQLAlchemy or the
models. It lives in its own blueprint (same URL prefix as AI Reporting) so reviewing never depends on the
report-generation routes, nor they on it.

Every view is wrapped by ``staff_required`` (a test fails if one is not). Reads and writes go through two
named guards so changing who may look without being able to decide is a one-line change.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, Response, jsonify, request, url_for
from flask_login import current_user
from werkzeug.exceptions import HTTPException

from app.persistence.review_states import STATUSES
from app.services.reporting.review_contracts import (
    MAX_PAGE_SIZE, RUN_NOT_FOUND, ReviewActor, ReviewEligibility, ReviewError, ReviewNotEligibleError,
    ReviewPermissionError, ReviewQuery,
)
from app.services.reporting.review_factory import build_review_inbox, build_review_service
from app.utils.decorators import staff_required
from app.utils.request_args import date_arg, int_arg, list_arg, valid_run_id

from .review_http import error_response, internal_error_response, invalid_request
from .review_payloads import (
    available_actions, eligibility_payload, event_payload, inbox_item_payload, inbox_page_payload, needs_eligibility,
    review_payload, run_info_payload,
)

bp = Blueprint("ai_reporting_reviews", __name__, url_prefix="/admin/ai-reporting")


@bp.errorhandler(403)
def forbidden_review_api(error):
    """Keep the JSON contract when the application's backoffice boundary aborts before the view."""
    return error_response(ReviewPermissionError(code="staff_required"), context="backoffice access")

REVIEWS_PER_PAGE_DEFAULT = 20

# Who may look / who may act. Both are "staff" today; relaxing reads (e.g. admins in read-only mode) is
# changing the first line. The service applies its own policy to every action anyway.
review_reader = staff_required(json=True)
review_writer = staff_required(json=True)

# The PDF is previewed in a same-origin iframe: framing is allowed for this origin only.
PDF_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Content-Security-Policy": "frame-ancestors 'self'",
}


def _service():
    return build_review_service()


def _inbox():
    return build_review_inbox()


def _actor() -> ReviewActor:
    return ReviewActor.from_user(current_user)


def _handles_review_errors(context: str):
    """Every failure leaves as JSON: typed workflow errors by their table, anything else as a generic 500."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            try:
                return view(*args, **kwargs)
            except ReviewError as exc:
                return error_response(exc, context=context)
            except HTTPException:
                raise  # a deliberate abort()/redirect keeps its own response
            except Exception:
                return internal_error_response(context=context)
        return wrapper
    return decorator


def _run_not_found() -> ReviewNotEligibleError:
    return ReviewNotEligibleError(ReviewEligibility.blocked(RUN_NOT_FOUND, "La ejecución no existe."))


def _json(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _pdf_url(review_id: int) -> str:
    return url_for("ai_reporting_reviews.review_pdf", review_id=review_id)


def _body_text(field: str, *, allow_blank: bool = True):
    """(value, error_response): the string ``field`` of the JSON body, exactly as sent.

    ``allow_blank=False`` is for identifiers (a hash): an empty one is a malformed request (400). Free text
    keeps ``allow_blank=True`` because the service owns its rules ("reason required", "comment required").
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, invalid_request("Se requiere un objeto JSON.")
    value = data.get(field)
    if not isinstance(value, str):
        return None, invalid_request(f"El campo «{field}» debe ser un texto.")
    if not allow_blank and not value.strip():
        return None, invalid_request(f"El campo «{field}» no puede estar vacío.")
    return value, None


# ── inbox / detail ────────────────────────────────────────────────────────────────────────────

@bp.route("/reviews", methods=["GET"])
@review_reader
@_handles_review_errors("list")
def list_reviews():
    """Paginated inbox. Query: ``status`` (repeatable / comma separated), ``report_id``, ``date_from``,
    ``date_to`` (on the submission date), ``page``, ``per_page``."""
    statuses = list_arg("status")
    unknown = [status for status in statuses if status not in STATUSES]
    if unknown:
        return invalid_request("Estado de revisión desconocido: " + ", ".join(sorted(set(unknown))))
    per_page = int_arg("per_page", REVIEWS_PER_PAGE_DEFAULT, maximum=MAX_PAGE_SIZE)
    page = int_arg("page", 1)
    query = ReviewQuery(
        statuses=tuple(statuses), report_id=request.args.get("report_id", type=int),
        submitted_from=date_arg("date_from"), submitted_to=date_arg("date_to", end_of_day=True),
        limit=per_page, offset=(page - 1) * per_page)
    result = _inbox().list_items(query)
    items = [inbox_item_payload(item, pdf_url=_pdf_url(item.review.id) if item.review.has_pdf else None)
             for item in result.items]
    return _json(inbox_page_payload(result, items))


@bp.route("/report-runs/<run_id>/review", methods=["GET"])
@review_reader
@_handles_review_errors("run review")
def run_review(run_id: str):
    """Review state of a run: its review (``null`` until prepared), eligibility and what the caller may do.

    ``eligibility`` is only evaluated while it can still matter (no review yet, or a draft): checking it reads
    and hashes the stored HTML, which is wasted work for a review under review or decided (``null`` then; the
    service re-checks at decision time anyway).
    """
    run_id = valid_run_id(run_id)
    if run_id is None:
        raise _run_not_found()
    service = _service()
    review = service.find_review_for_run(run_id)
    eligibility = None
    if needs_eligibility(review):
        eligibility = service.check_eligibility(run_id)
        if eligibility.code == RUN_NOT_FOUND:
            raise ReviewNotEligibleError(eligibility)
    return _json({
        "run_id": run_id,
        "run": run_info_payload(_inbox().describe_run(run_id)),
        "review": None if review is None else review_payload(
            review, pdf_url=_pdf_url(review.id) if review.has_pdf else None),
        "eligibility": None if eligibility is None else eligibility_payload(eligibility),
        "actions": available_actions(review, eligibility, actor=_actor(), policy=service.policy),
    })


# ── preparation / submission ──────────────────────────────────────────────────────────────────

def _prepared(run_id: str, action):
    if valid_run_id(run_id) is None:
        raise _run_not_found()
    review = action(run_id, _actor())
    return _json({"review": review_payload(review, pdf_url=_pdf_url(review.id) if review.has_pdf else None)})


@bp.route("/report-runs/<run_id>/prepare-review", methods=["POST"])
@review_writer
@_handles_review_errors("prepare")
def prepare_review(run_id: str):
    """Generate (or reuse) and pin the run's PDF. Idempotent; runs the PDF engine, so it can take a while."""
    return _prepared(run_id, _service().prepare_review)


@bp.route("/report-runs/<run_id>/submit-review", methods=["POST"])
@review_writer
@_handles_review_errors("submit")
def submit_review(run_id: str):
    """``draft`` -> ``pending_review`` (preparing first when needed). Idempotent."""
    return _prepared(run_id, _service().submit_review)


# ── decisions / comments ──────────────────────────────────────────────────────────────────────

@bp.route("/reviews/<int:review_id>/approve", methods=["POST"])
@review_writer
@_handles_review_errors("approve")
def approve_review(review_id: int):
    """Body: ``{"expected_pdf_sha256": "..."}``, the hash of the PDF the reviewer had in front."""
    expected, error = _body_text("expected_pdf_sha256", allow_blank=False)
    if error:
        return error
    review = _service().approve_review(review_id, _actor(), expected)
    return _json({"review": review_payload(review, pdf_url=_pdf_url(review.id))})


@bp.route("/reviews/<int:review_id>/reject", methods=["POST"])
@review_writer
@_handles_review_errors("reject")
def reject_review(review_id: int):
    """Body: ``{"reason": "...", "expected_pdf_sha256": "..."}``. The PDF is kept with the rejection."""
    expected, error = _body_text("expected_pdf_sha256", allow_blank=False)
    if error:
        return error
    reason, error = _body_text("reason")
    if error:
        return error
    review = _service().reject_review(review_id, _actor(), reason, expected)
    return _json({"review": review_payload(review, pdf_url=_pdf_url(review.id))})


@bp.route("/reviews/<int:review_id>/comments", methods=["POST"])
@review_writer
@_handles_review_errors("comment")
def add_review_comment(review_id: int):
    """Body: ``{"comment": "..."}``. An observation; it changes neither the status nor the PDF."""
    comment, error = _body_text("comment")
    if error:
        return error
    service = _service()
    service.add_comment(review_id, _actor(), comment)
    review = service.get_review(review_id)
    return _json({"review": review_payload(review, pdf_url=_pdf_url(review.id) if review.has_pdf else None)}, 201)


# ── PDF / audit trail ─────────────────────────────────────────────────────────────────────────

@bp.route("/reviews/<int:review_id>/pdf", methods=["GET"])
@review_reader
@_handles_review_errors("pdf")
def review_pdf(review_id: int):
    """The exact persisted PDF of the review, served only after its SHA-256 matches the one on record.

    ``?download=1`` asks for an attachment instead of an inline preview. Never regenerates anything.
    """
    pdf = _service().get_review_pdf(review_id)
    disposition = "attachment" if request.args.get("download") in ("1", "true") else "inline"
    filename = f"klara-revision-{review_id}-{pdf.sha256[:8]}.pdf"
    return Response(pdf.data, mimetype="application/pdf", headers={
        **PDF_HEADERS,
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        "X-Report-PDF-SHA256": pdf.sha256,
    })


@bp.route("/reviews/<int:review_id>/events", methods=["GET"])
@review_reader
@_handles_review_errors("events")
def review_events(review_id: int):
    events = _service().list_events(review_id)
    return _json({"review_id": review_id, "events": [event_payload(event) for event in events]})
