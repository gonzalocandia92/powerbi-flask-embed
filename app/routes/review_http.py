"""HTTP face of the review workflow's errors: ONE table from error to status, one response shape.

Pure translation: no business rule lives here and nothing is imported from SQLAlchemy or the models. The
service raises ``ReviewError`` subclasses with a stable ``code`` and a public-safe message; this module decides
only how they look over HTTP. A new error code is one more row in ``STATUS_RULES``; the UI (stage 5) and any
future HTTP client rely on the ``{"error", "code", ...}`` shape.
"""
from __future__ import annotations

import logging
from typing import Callable

from flask import Response, jsonify

from app.services.reporting.pdf_artifacts import PdfNotFoundError, PdfSourceUnavailableError, PdfTooLargeError
from app.services.reporting.pdf_renderer import PdfBrowserUnavailableError
from app.services.reporting.review_contracts import (
    PREPARATION_IN_PROGRESS, RUN_NOT_FOUND, ReviewConflictError, ReviewError, ReviewNotEligibleError,
    ReviewNotFoundError, ReviewPdfError, ReviewPermissionError, ReviewStoreError, ReviewValidationError,
)

LOG = logging.getLogger(__name__)

INVALID_REQUEST = "invalid_request"
INTERNAL_ERROR = "review_failed"
RETRY_AFTER_SECONDS = 5

# ``ReviewPdfError`` only carries the code of the PDF failure behind it, so the rules below key on the codes
# owned by those classes (never on retyped strings): renaming one cannot silently turn its rule into a 500.
PDF_NOT_FOUND = PdfNotFoundError.code
PDF_TOO_LARGE = PdfTooLargeError.code
PDF_SOURCE_UNAVAILABLE = PdfSourceUnavailableError.code
BROWSER_UNAVAILABLE = PdfBrowserUnavailableError.code

Rule = tuple[Callable[[ReviewError], bool], int]


def _is(kind: type, *codes: str) -> Callable[[ReviewError], bool]:
    return lambda exc: isinstance(exc, kind) and (not codes or exc.code in codes)


# First match wins, so specific codes go before their class. Anything unmatched is a 500.
STATUS_RULES: tuple[Rule, ...] = (
    (_is(ReviewPermissionError), 403),
    (_is(ReviewNotFoundError), 404),
    (_is(ReviewNotEligibleError, RUN_NOT_FOUND), 404),
    (_is(ReviewNotEligibleError), 409),
    (_is(ReviewConflictError), 409),
    (_is(ReviewValidationError), 422),
    (_is(ReviewPdfError, PDF_NOT_FOUND), 404),
    (_is(ReviewPdfError, PDF_SOURCE_UNAVAILABLE), 409),
    (_is(ReviewPdfError, PDF_TOO_LARGE), 422),
    (_is(ReviewPdfError, BROWSER_UNAVAILABLE), 503),   # same status as /render-pdf
    (_is(ReviewPdfError), 500),
    (_is(ReviewStoreError), 500),
)


def status_for(exc: ReviewError) -> int:
    for matches, status in STATUS_RULES:
        if matches(exc):
            return status
    return 500


def _extras(exc: ReviewError) -> dict:
    extras: dict = {}
    if isinstance(exc, ReviewConflictError) and exc.current_status is not None:
        extras["current_status"] = exc.current_status
    if isinstance(exc, ReviewNotEligibleError):
        extras["eligibility"] = {"eligible": False, "code": exc.eligibility.code,
                                 "message": exc.eligibility.message, "run_status": exc.eligibility.run_status}
    return extras


def error_response(exc: ReviewError, *, context: str = "") -> tuple[Response, int]:
    """The JSON response for a workflow error. 5xx are logged with their cause; the body stays generic."""
    status = status_for(exc)
    code = getattr(exc, "code", ReviewError.code)
    if status >= 500:
        LOG.error("[ReportReview] %s failed code=%s", context or "request", code, exc_info=exc)
    else:
        LOG.info("[ReportReview] %s refused code=%s status=%s", context or "request", code, status)
    response = jsonify({"error": str(exc) or ReviewError.message, "code": code, **_extras(exc)})
    if isinstance(exc, ReviewConflictError) and code == PREPARATION_IN_PROGRESS:
        response.headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
    return response, status


def internal_error_response(*, context: str = "") -> tuple[Response, int]:
    """Generic JSON 500 for an exception that is not a ``ReviewError`` (database down, a bug).

    Must be called from inside the ``except`` block: the traceback goes to the log, never to the client, who
    keeps getting the same ``{"error", "code"}`` shape as for every other failure.
    """
    LOG.exception("[ReportReview] %s failed unexpectedly", context or "request")
    return jsonify({"error": ReviewError.message, "code": INTERNAL_ERROR}), 500


def invalid_request(message: str) -> tuple[Response, int]:
    return jsonify({"error": message, "code": INVALID_REQUEST}), 400
