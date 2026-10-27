"""URL templates of the review endpoints, for scripts that fill them in the browser.

One helper so every screen that needs a review URL (the review pages and the generation screen's entry point)
builds them the same way: ``{id}`` stands for a review id and ``{run_id}`` for a run id.
"""
from __future__ import annotations

from typing import Any

from flask import url_for

_REVIEW_SENTINEL = 987654321   # an int (the route converter is ``int``) that cannot collide with a path segment
_RUN_SENTINEL = "RUNID"


def template_url(endpoint: str, *, review: bool = False, run: bool = False) -> str:
    """URL of ``endpoint`` with ``{id}`` (review) / ``{run_id}`` placeholders instead of real values."""
    params: dict[str, Any] = {}
    if review:
        params["review_id"] = _REVIEW_SENTINEL
    if run:
        params["run_id"] = _RUN_SENTINEL
    return url_for(endpoint, **params).replace(str(_REVIEW_SENTINEL), "{id}").replace(_RUN_SENTINEL, "{run_id}")
