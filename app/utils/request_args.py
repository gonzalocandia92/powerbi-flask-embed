"""Small, framework-light parsers for query-string arguments and path identifiers.

Shared by the AI Reporting blueprints so none of them has to import another route module for these.
Invalid input never raises: it falls back to the default (or ``None``), like the history listing always did.
"""
from __future__ import annotations

import re
from datetime import datetime

from flask import request

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def int_arg(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    value = max(minimum, value)
    return min(value, maximum) if maximum else value


def date_arg(name: str, *, end_of_day: bool = False) -> datetime | None:
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    return parsed.replace(hour=23, minute=59, second=59, microsecond=999999) if end_of_day else parsed


def list_arg(name: str) -> list[str]:
    """A repeatable and/or comma separated argument (``?status=a&status=b,c``) as a clean list."""
    values: list[str] = []
    for raw in request.args.getlist(name):
        values.extend(part.strip() for part in raw.split(",") if part.strip())
    return values


def valid_run_id(value) -> str | None:
    """``report_run_id`` as a safe identifier, or ``None`` for anything unexpected."""
    return value if isinstance(value, str) and _RUN_ID_RE.match(value) else None
