"""HTML -> PDF rendering, isolated from the report pipeline.

``PdfRenderer`` is a provider-neutral port: it turns an HTML document (string)
into PDF bytes and knows nothing about ``FinalReport``, Flask, the LLM or where
the bytes end up (HTTP download today; email/WhatsApp/storage later).

``WeasyPrintPdfRenderer`` is the only implementation. WeasyPrint is synchronous and
CPU bound, so each call runs it in a worker thread to keep the async port. It is
imported lazily so the application boots, and every other feature works, even where
WeasyPrint or its system libraries (Pango) are not installed.

Callers are expected to pass HTML produced by ``HtmlReportRenderer`` from a
validated ``FinalReport``, never client-supplied HTML. As defense in depth the URL
fetcher (WeasyPrint's own, limited to the ``data`` protocol) refuses everything else: the HTML is self-contained, so nothing
legitimate needs the network or the filesystem.

WeasyPrint implements less of CSS Grid/Flexbox than the browser the HTML was designed against.
The compensations are a separate, named stylesheet (``weasyprint_print_styles``) applied on top
of the canonical document; the HTML renderers stay untouched and another engine ignores it.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Mapping, Protocol

from .weasyprint_print_styles import weasyprint_print_css

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 30_000


class PdfRenderer(Protocol):
    async def render(self, html: str) -> bytes:
        ...


class PdfRenderError(Exception):
    """Base error. ``code`` is a stable, public-safe identifier; the message is
    generic on purpose (technical detail lives in ``__cause__`` and the logs)."""

    code = "pdf_render_failed"


class PdfBrowserUnavailableError(PdfRenderError):
    """WeasyPrint (or its system libraries) is missing. The name and ``code`` are kept
    from the browser-based implementation: they are part of the HTTP contract."""

    code = "browser_unavailable"


class PdfRenderFailedError(PdfRenderError):
    """The engine loaded but the PDF could not be produced."""

    code = "pdf_render_failed"


def _weasyprint():
    """Seam for tests. Raises ``ImportError``/``OSError`` when WeasyPrint or Pango are missing."""
    import weasyprint
    return weasyprint


def _inline_only_url_fetcher(weasyprint: Any):
    """Fetcher that serves ``data:`` URLs and refuses everything else (http, https, file,
    ftp, ...). A refused resource is skipped with a warning; the render itself goes on."""
    return weasyprint.URLFetcher(allowed_protocols=("data",))


class WeasyPrintPdfRenderer:
    """``html -> PDF bytes`` with WeasyPrint (A4 via the document's ``@page``, print CSS, backgrounds)."""

    def __init__(self, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self._timeout_ms = timeout_ms

    async def render(self, html: str) -> bytes:
        try:
            weasyprint = _weasyprint()
        except (ImportError, OSError) as exc:  # package or Pango/HarfBuzz missing
            raise PdfBrowserUnavailableError("El generador de PDF no está disponible.") from exc
        try:
            pdf = await asyncio.wait_for(asyncio.to_thread(self._print, weasyprint, html),
                                         timeout=self._timeout_ms / 1000)
        except PdfRenderError:
            raise
        except Exception as exc:  # includes asyncio.TimeoutError
            raise PdfRenderFailedError("No se pudo generar el PDF.") from exc
        if not pdf:
            raise PdfRenderFailedError("No se pudo generar el PDF.")
        return pdf

    @staticmethod
    def _print(weasyprint: Any, html: str) -> bytes:
        document = weasyprint.HTML(string=html, url_fetcher=_inline_only_url_fetcher(weasyprint))
        return document.write_pdf(stylesheets=[weasyprint.CSS(string=weasyprint_print_css())])


def build_pdf_renderer(env: Mapping[str, str] | None = None) -> PdfRenderer:
    """Composition helper: builds the production renderer from environment settings.

    ``REPORT_PDF_TIMEOUT_MS``  how long a request waits for the PDF (default 30000).
    """
    env = os.environ if env is None else env
    try:
        timeout_ms = int(env.get("REPORT_PDF_TIMEOUT_MS") or DEFAULT_TIMEOUT_MS)
    except ValueError:
        timeout_ms = DEFAULT_TIMEOUT_MS
    return WeasyPrintPdfRenderer(timeout_ms=max(1_000, timeout_ms))
