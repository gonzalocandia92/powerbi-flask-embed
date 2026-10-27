"""Default wiring of the review workflow (the only place that picks concrete collaborators).

Same role as ``pipeline_factory``/``writer_factory``: callers ask for a ready service; tests and the
scheduler can build ``ReviewService`` themselves with other collaborators.

``REPORT_REVIEW_PREPARATION_LEASE_SECONDS`` is how long a preparer owns a draft before another may take
over. The default is the PDF timeout plus a margin, so a render that is still allowed to run is never
duplicated; invalid values fall back to the default.
"""
from __future__ import annotations

import os
from typing import Mapping, Sequence

from .artifact_service import ArtifactService
from .pdf_renderer import DEFAULT_TIMEOUT_MS
from .review_contracts import EligibilityRule, ReviewArtifacts, ReviewInbox, ReviewPolicy, ReviewStore
from .review_eligibility import EligibilityChecker
from .review_inbox import SqlReviewInbox
from .review_policy import StaffReviewPolicy
from .review_service import ReviewService
from .review_store import SqlReviewStore

LEASE_MARGIN_SECONDS = 60.0


def preparation_lease_seconds(env: Mapping[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    try:
        explicit = float(env.get("REPORT_REVIEW_PREPARATION_LEASE_SECONDS") or 0)
    except ValueError:
        explicit = 0
    if explicit > 0:
        return explicit
    try:
        timeout_ms = float(env.get("REPORT_PDF_TIMEOUT_MS") or DEFAULT_TIMEOUT_MS)
    except ValueError:
        timeout_ms = DEFAULT_TIMEOUT_MS
    return (timeout_ms if timeout_ms > 0 else DEFAULT_TIMEOUT_MS) / 1000 + LEASE_MARGIN_SECONDS


def build_review_inbox() -> ReviewInbox:
    """Read model of the inbox (see ``review_inbox``); stateless, so building one per request is free."""
    return SqlReviewInbox()


def build_review_service(*, store: ReviewStore | None = None, artifacts: ReviewArtifacts | None = None,
                         policy: ReviewPolicy | None = None, rules: Sequence[EligibilityRule] | None = None,
                         lease_seconds: float | None = None) -> ReviewService:
    artifacts = artifacts or ArtifactService()
    return ReviewService(
        store or SqlReviewStore(), artifacts, EligibilityChecker(rules), policy or StaffReviewPolicy(),
        lease_seconds=lease_seconds if lease_seconds is not None else preparation_lease_seconds())
