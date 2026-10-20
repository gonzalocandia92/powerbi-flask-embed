"""What the report-generation screen needs to know about the review workflow: three URLs and one flag.

The generation screen does not import the review screens or their rules; it only learns where to send staff
(``detailUrl``), where the persisted PDF lives (``pdfUrl``) and whether this user may see either
(``canReview``). When the review blueprints are not installed the answer is ``None`` and the screen keeps its
original draft-only behavior.
"""
from __future__ import annotations

from typing import Any

from werkzeug.routing import BuildError

from app.services.report_access import has_backoffice_access, is_staff_actor

from .review_urls import template_url


def review_entry_links(user: Any) -> dict[str, Any] | None:
    try:
        return {
            "canReview": bool(is_staff_actor(user) and has_backoffice_access(user)),
            "pdfUrl": template_url("ai_reporting_reviews.review_pdf", review=True),
            "detailUrl": template_url("ai_reporting_review_pages.detail_page", run=True),
        }
    except BuildError:
        return None
