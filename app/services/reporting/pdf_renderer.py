"""HTML -> PDF rendering, isolated from the report pipeline.

``PdfRenderer`` is a provider-neutral port: it turns an HTML document (string)
into PDF bytes and knows nothing about ``FinalReport``, Flask, the LLM or where
the bytes end up (HTTP download today; email/WhatsApp/storage later).

``PlaywrightPdfRenderer`` is the only implementation. It opens Chromium for each
call and always closes it (V1: isolation over throughput; no pooling). Playwright
is imported lazily so the application boots, and every other feature works, even
where Chromium is not installed.

Callers are expected to pass HTML produced by ``HtmlReportRenderer`` from a
validated ``FinalReport``, never client-supplied HTML. As defense in depth the
browser context runs with JavaScript disabled and blocks every network request:
the HTML is self-contained, so nothing legitimate needs the network.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 30_000
# /dev/shm is 64 MB in Docker by default, too small for Chromium.
_BASE_LAUNCH_ARGS = ("--disable-dev-shm-usage",)


class PdfRenderer(Protocol):
    async def render(self, html: str) -> bytes:
        ...


class PdfRenderError(Exception):
    """Base error. ``code`` is a stable, public-safe identifier; the message is
    generic on purpose (technical detail lives in ``__cause__`` and the logs)."""

    code = "pdf_render_failed"


class PdfBrowserUnavailableError(PdfRenderError):
    """Chromium/Playwright is missing or cannot be launched."""

    code = "browser_unavailable"


class PdfRenderFailedError(PdfRenderError):
    """The browser started but the PDF could not be produced."""

    code = "pdf_render_failed"


def _async_playwright():
    """Seam for tests. Raises ``ImportError`` when Playwright is not installed."""
    from playwright.async_api import async_playwright
    return async_playwright()


async def _allow_only_inline(route) -> None:
    # The document is self-contained (inline CSS, data: images). Anything that
    # would leave the page (http, https, file, ftp...) is refused.
    if route.request.url.startswith(("data:", "about:")):
        await route.continue_()
    else:
        await route.abort()


class PlaywrightPdfRenderer:
    """``html -> PDF bytes`` with headless Chromium (A4, print CSS, backgrounds)."""

    def __init__(self, *, timeout_ms: int = DEFAULT_TIMEOUT_MS, chromium_path: str | None = None,
                 no_sandbox: bool = False, extra_launch_args: Sequence[str] = ()) -> None:
        self._timeout_ms = timeout_ms
        self._chromium_path = chromium_path or None
        self._launch_args = [*_BASE_LAUNCH_ARGS, *(["--no-sandbox"] if no_sandbox else []), *extra_launch_args]

    async def render(self, html: str) -> bytes:
        try:
            playwright_cm = _async_playwright()
        except ImportError as exc:
            raise PdfBrowserUnavailableError("El generador de PDF no está disponible.") from exc
        try:
            playwright = await playwright_cm.start()
        except Exception as exc:  # driver missing/corrupt
            raise PdfBrowserUnavailableError("El generador de PDF no está disponible.") from exc
        try:
            try:
                launch_options: dict[str, Any] = {"headless": True, "args": self._launch_args,
                                                  "timeout": self._timeout_ms}
                if self._chromium_path:
                    launch_options["executable_path"] = self._chromium_path
                browser = await playwright.chromium.launch(**launch_options)
            except Exception as exc:  # binary not installed, missing system libs, ...
                raise PdfBrowserUnavailableError("El generador de PDF no está disponible.") from exc
            try:
                pdf = await self._print(browser, html)
            except PdfRenderError:
                raise
            except Exception as exc:
                raise PdfRenderFailedError("No se pudo generar el PDF.") from exc
            finally:
                try:
                    await browser.close()
                except Exception:
                    logger.warning("[PdfRenderer] Chromium did not close cleanly", exc_info=True)
        finally:
            try:
                await playwright.stop()
            except Exception:
                logger.warning("[PdfRenderer] Playwright did not stop cleanly", exc_info=True)
        if not pdf:
            raise PdfRenderFailedError("No se pudo generar el PDF.")
        return pdf

    async def _print(self, browser, html: str) -> bytes:
        context = await browser.new_context(java_script_enabled=False)
        try:
            context.set_default_timeout(self._timeout_ms)
            await context.route("**/*", _allow_only_inline)
            page = await context.new_page()
            await page.set_content(html, wait_until="networkidle", timeout=self._timeout_ms)
            # The @page rule in the report CSS (A4, margins) wins over these defaults.
            return await page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            await context.close()


def build_pdf_renderer(env: Mapping[str, str] | None = None) -> PdfRenderer:
    """Composition helper: builds the production renderer from environment settings.

    ``REPORT_PDF_CHROMIUM_PATH``       use this Chromium binary instead of Playwright's.
    ``REPORT_PDF_CHROMIUM_NO_SANDBOX`` "1"/"true": add ``--no-sandbox`` (needed when the
                                       container runs as root, as this project's image does).
    ``REPORT_PDF_TIMEOUT_MS``          per-step timeout (default 30000).
    """
    env = os.environ if env is None else env
    try:
        timeout_ms = int(env.get("REPORT_PDF_TIMEOUT_MS") or DEFAULT_TIMEOUT_MS)
    except ValueError:
        timeout_ms = DEFAULT_TIMEOUT_MS
    return PlaywrightPdfRenderer(
        timeout_ms=max(1_000, timeout_ms),
        chromium_path=env.get("REPORT_PDF_CHROMIUM_PATH") or None,
        no_sandbox=str(env.get("REPORT_PDF_CHROMIUM_NO_SANDBOX", "")).strip().lower() in {"1", "true", "yes"},
    )
