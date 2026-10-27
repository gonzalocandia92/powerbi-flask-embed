"""Vocabulary of PDF run artifacts: errors, read models, output checks and the sync bridge.

Pure module: no Flask, SQLAlchemy, models or review/persistence imports. It is shared by
``ArtifactService`` (which does the persisting) and by whoever reacts to its failures
(review service, HTTP layer), so each of them depends on these names and on the stable
``code`` of every error, never on the service internals.

Every failure is a ``PdfArtifactError`` with a public-safe ``code``; the message is generic
on purpose (technical detail lives in ``__cause__`` and the logs), like ``PdfRenderError``.
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Mapping

from .artifacts import ArtifactRecord
from .pdf_renderer import PdfRenderer

DEFAULT_PDF_MAX_BYTES = 20 * 1024 * 1024
PDF_MAGIC = b"%PDF-"
_MAX_ENGINE_TAG_CHARS = 40  # ``report_run_artifacts.renderer_version`` is String(40)


class PdfArtifactError(Exception):
    """Base error. ``code`` is a stable identifier safe to expose."""

    code = "pdf_artifact_failed"
    message = "No se pudo completar la operación con el PDF."  # public-safe; never built from internals

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        super().__init__(message or self.message)
        if code is not None:
            self.code = code


class PdfSourceUnavailableError(PdfArtifactError):
    """The persisted HTML / FinalReport the PDF must come from is missing, corrupt or not stored."""

    code = "html_unavailable"
    message = "El informe persistido no está disponible para generar el PDF."


class PdfRenderFailedArtifactError(PdfArtifactError):
    """The PDF engine failed; the instance ``code`` is the renderer's (browser_unavailable / pdf_render_failed)."""

    code = "pdf_render_failed"
    message = "No se pudo generar el PDF."


class PdfInvalidOutputError(PdfArtifactError):
    """The engine returned something that is not a PDF."""

    code = "pdf_render_failed"
    message = "El generador de PDF devolvió un resultado no válido."


class PdfTooLargeError(PdfArtifactError):
    """The PDF exceeds the configured size limit and was not stored."""

    code = "pdf_too_large"
    message = "El PDF supera el tamaño permitido."


class PdfPersistenceError(PdfArtifactError):
    """The PDF could not be written to storage."""

    code = "pdf_persist_failed"
    message = "No se pudo guardar el PDF."


class PdfNotFoundError(PdfArtifactError):
    """No PDF artifact with that id exists for that run."""

    code = "pdf_not_found"
    message = "El PDF solicitado no existe."


class PdfIntegrityError(PdfArtifactError):
    """The stored bytes no longer hash to the recorded SHA-256."""

    code = "pdf_integrity_failed"
    message = "El PDF almacenado no superó la verificación de integridad."


@dataclass(frozen=True)
class LoadedPdf:
    """Exact stored bytes of a PDF whose integrity was verified on this read."""

    data: bytes
    sha256: str
    size_bytes: int
    artifact: ArtifactRecord

    @property
    def source_html_artifact_id(self) -> int | None:
        return self.artifact.metadata.get("source_html_artifact_id")

    @property
    def source_final_report_artifact_id(self) -> int | None:
        return self.artifact.metadata.get("source_final_report_artifact_id")


@dataclass(frozen=True)
class SavedPdf:
    """A freshly persisted PDF plus the source artifacts it was resolved from.

    The source ids are returned so the caller can pin them (e.g. in a review) without re-reading.
    """

    artifact: ArtifactRecord
    source_html_artifact_id: int
    source_final_report_artifact_id: int


def pdf_max_bytes_from_env(env: Mapping[str, str] | None = None) -> int:
    """``REPORT_PDF_MAX_BYTES``: largest PDF that is persisted (default 20 MiB; invalid values fall back)."""
    env = os.environ if env is None else env
    try:
        value = int(env.get("REPORT_PDF_MAX_BYTES") or DEFAULT_PDF_MAX_BYTES)
    except ValueError:
        return DEFAULT_PDF_MAX_BYTES
    return value if value > 0 else DEFAULT_PDF_MAX_BYTES


def validate_pdf_output(pdf: Any, max_bytes: int) -> bytes:
    """Return ``pdf`` when it is a non-empty PDF within the limit; otherwise raise (nothing is stored)."""
    if not isinstance(pdf, (bytes, bytearray)) or not pdf.startswith(PDF_MAGIC):
        raise PdfInvalidOutputError()
    if len(pdf) > max_bytes:
        raise PdfTooLargeError()
    return bytes(pdf)


def describe_engine(renderer: PdfRenderer) -> tuple[str, str | None]:
    """``(name, version)`` of a renderer. Both are optional attributes, so any ``PdfRenderer`` works."""
    return getattr(renderer, "engine_name", None) or "pdf", getattr(renderer, "engine_version", None)


def engine_tag(name: str, version: str | None) -> str:
    """Compact engine identity stored in the artifact's ``renderer_version`` column."""
    return (f"{name}-{version}" if version else name)[:_MAX_ENGINE_TAG_CHARS]


def run_pdf_render(renderer: PdfRenderer, html: str) -> bytes:
    """Run the async ``PdfRenderer`` port from synchronous code (the only place that knows about the bridge).

    Works from a plain thread and from a thread that already has a running event loop
    (the render then runs in a worker thread with its own loop). The renderer keeps its own timeout.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(renderer.render(html))
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(renderer.render(html))).result()
