"""Small, transport-agnostic helpers for exporting a ``FinalReport`` as a PDF file.

Kept apart from ``pdf_renderer`` (HTML -> bytes) so the same naming rules serve
an HTTP download today and email/WhatsApp attachments later.
"""
from __future__ import annotations

import re
import unicodedata

from .final_report import FinalReport

_MAX_SLUG_CHARS = 60


def safe_slug(text: str, fallback: str = "klara") -> str:
    """ASCII, lowercase, ``[a-z0-9-]`` only: safe in a header, a path or an email attachment."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:_MAX_SLUG_CHARS].strip("-")
    return slug or fallback


def report_pdf_filename(report: FinalReport) -> str:
    """``informe-<slug>.pdf`` from the (untrusted, model-written) title."""
    slug = safe_slug(report.title)
    if slug.startswith("informe-"):
        slug = slug[len("informe-"):] or "klara"
    return f"informe-{slug}.pdf"
