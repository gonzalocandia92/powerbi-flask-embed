"""Orchestrates validate / render / persist / read for run artifacts.

Boundaries (all injectable, so each piece is testable alone):

    FinalReportSchemaRegistry   version -> Pydantic model        (no DB)
    HtmlRendererRegistry        version -> deterministic renderer (no DB)
    ArtifactStore               bytes + metadata <-> SQLAlchemy   (no FinalReport/HTML knowledge)

Source of truth
    FinalReport artifact  = the structured semantics of a run.
    HTML artifact         = a derived presentation, FROZEN once persisted: reads return
                            the stored bytes and never call a renderer. A renderer runs
                            only in ``render_and_save_html`` (new run, or an explicit
                            regeneration that appends a new revision) and for
                            legacy runs that have no artifact (in memory, never written).

    PDF artifact          = a derived, FROZEN copy of a PERSISTED html artifact (``render_and_save_pdf``
                            appends it; ``get_pdf`` returns the exact bytes after verifying their SHA-256).
                            PDF bytes are not reproducible, so a PDF is generated once and reused.

Consumers (HTTP, PDF, later scheduler/review/email) call ``get_final_report`` /
``get_html`` and never see tables, artifact ids, versions or the legacy fallback.
Read methods never write.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import BaseModel

from .artifact_store import ArtifactStore, ArtifactStoreError
from .artifacts import ArtifactRecord, canonical_json
from .pdf_artifacts import (
    LoadedPdf, PdfArtifactError, PdfIntegrityError, PdfNotFoundError, PdfPersistenceError,
    PdfRenderFailedArtifactError, PdfSourceUnavailableError, SavedPdf, describe_engine, engine_tag,
    pdf_max_bytes_from_env, run_pdf_render, validate_pdf_output,
)
from .pdf_renderer import PdfRenderer, PdfRenderError, build_pdf_renderer
from .registries import (
    FinalReportSchemaRegistry, HtmlRendererRegistry, VersionResolutionError, final_report_schemas, html_renderers,
)
from .versions import (
    ARTIFACT_FINAL_REPORT, ARTIFACT_HTML, ARTIFACT_PDF, CONTENT_TYPE_HTML, CONTENT_TYPE_JSON, CONTENT_TYPE_PDF,
)

LOG = logging.getLogger(__name__)

ORIGIN_ARTIFACT = "artifact"
ORIGIN_LEGACY = "legacy"


class ArtifactRenderError(Exception):
    """The renderer failed (or the version could not be resolved); nothing was persisted."""


class ArtifactPersistenceError(Exception):
    """The artifact could not be written to storage."""


@dataclass(frozen=True)
class LoadedFinalReport:
    report: BaseModel          # instance of the model registered for ``schema_version``
    schema_version: str
    origin: str                # ORIGIN_ARTIFACT | ORIGIN_LEGACY
    artifact: ArtifactRecord | None = None

    def to_json(self) -> dict:
        return self.report.model_dump(mode="json")


@dataclass(frozen=True)
class LoadedHtml:
    html: str
    schema_version: str | None
    renderer_version: str | None
    origin: str                # ORIGIN_ARTIFACT (stored bytes) | ORIGIN_LEGACY (rendered in memory, not stored)
    artifact: ArtifactRecord | None = None


@dataclass(frozen=True)
class ExportSource:
    """What the PDF export needs: a validated report and the HTML it was versioned with."""
    report: BaseModel
    html: str
    schema_version: str
    renderer_version: str | None
    origin: str                # ORIGIN_ARTIFACT | ORIGIN_LEGACY | "request" (client JSON, validated + rendered)


def _release() -> str | None:
    return os.getenv("RELEASE_VERSION") or os.getenv("GIT_SHA") or None


class ArtifactService:
    def __init__(self, store: ArtifactStore | None = None, *,
                 schemas: FinalReportSchemaRegistry | None = None,
                 renderers: HtmlRendererRegistry | None = None,
                 pdf_renderer: PdfRenderer | None = None, pdf_max_bytes: int | None = None):
        self.store = store or ArtifactStore()
        self.schemas = schemas or final_report_schemas
        self.renderers = renderers or html_renderers
        # PDF collaborators are optional and resolved when used: this service is built in many
        # places that never touch PDFs, and the production defaults read the environment.
        self._pdf_renderer = pdf_renderer
        self._pdf_max_bytes = pdf_max_bytes

    # ── writes ────────────────────────────────────────────────────────────

    def save_final_report(self, report_run_id: str, report: BaseModel, *,
                          extra_metadata: Mapping[str, Any] | None = None) -> ArtifactRecord:
        """Persist the FinalReport exactly as validated, under the schema version it declares."""
        schema_version = getattr(report, "schema_version", None)
        try:
            self.schemas.get(schema_version)  # only registered contracts are persisted
            content = canonical_json(report.model_dump(mode="json"))
        except (VersionResolutionError, ValueError, TypeError) as exc:
            self._log("final_report", report_run_id, schema_version, None, ok=False)
            raise ArtifactPersistenceError(f"FinalReport cannot be serialized: {exc}") from exc
        return self._add(report_run_id, ARTIFACT_FINAL_REPORT, content, CONTENT_TYPE_JSON,
                         schema_version=schema_version, metadata={**self._metadata(), **(extra_metadata or {})})

    def render_and_save_html(self, report_run_id: str, report: BaseModel, *, renderer_version: str | None = None,
                             source: ArtifactRecord | None = None,
                             extra_metadata: Mapping[str, Any] | None = None) -> ArtifactRecord:
        """Render ``report`` with a registered renderer and APPEND the result as a new HTML artifact.

        ``renderer_version=None`` uses the schema's default renderer. An existing HTML
        artifact is never replaced: a second call adds a new revision next to it.
        Raises ``ArtifactRenderError`` (nothing stored) or ``ArtifactPersistenceError``.
        """
        schema_version = getattr(report, "schema_version", None)
        try:
            version, renderer = self.renderers.get_for_schema(renderer_version, schema_version)
            html = renderer.render(report)
        except Exception as exc:  # unknown/incompatible version or a renderer bug: same contract
            self._log("html", report_run_id, schema_version, renderer_version, ok=False)
            LOG.exception("[ReportArtifact] HTML render failed run=%s", report_run_id)
            raise ArtifactRenderError("HTML rendering failed") from exc
        metadata = {**self._metadata(), **(extra_metadata or {})}
        if source is not None:
            metadata["source_artifact_id"] = source.id
        return self._add(report_run_id, ARTIFACT_HTML, html, CONTENT_TYPE_HTML,
                         schema_version=schema_version, renderer_version=version, metadata=metadata)

    def render_and_save_pdf(self, report_run_id: str, *, html_artifact_id: int | None = None,
                            final_report_artifact_id: int | None = None,
                            extra_metadata: Mapping[str, Any] | None = None) -> SavedPdf:
        """Convert a PERSISTED HTML artifact to PDF and APPEND it as a new ``pdf`` artifact.

        The HTML is never taken from a client nor re-rendered: ``html_artifact_id`` pins the exact
        stored artifact (``None`` = the original revision, same policy as ``get_html``), and likewise
        ``final_report_artifact_id`` for the lineage (``None`` = the FinalReport the chosen HTML was rendered
        from, i.e. its ``source_artifact_id``; the latest one only for HTML that does not record it). A run whose HTML/FinalReport exist only as legacy
        in-memory renders has no artifact row to point at and is rejected (``html_unavailable``).

        Nothing is stored on any failure. Not atomic by itself: two concurrent calls append two PDFs;
        callers that need "once" serialize before calling (the review draft lock) and use ``find_pdf``.
        Raises ``PdfArtifactError`` subclasses only.
        """
        html_record = self._source_html(report_run_id, html_artifact_id)
        report_record = self._source_final_report(report_run_id, final_report_artifact_id, html_record)
        renderer = self._pdf_renderer or build_pdf_renderer()
        engine, engine_version = describe_engine(renderer)
        self.store.end_read_transaction()  # do not hold a connection "idle in transaction" while rendering
        try:
            raw = run_pdf_render(renderer, html_record.text)
        except PdfRenderError as exc:
            self._log("pdf", report_run_id, html_record.schema_version, None, ok=False)
            raise PdfRenderFailedArtifactError(code=exc.code) from exc
        except Exception as exc:
            self._log("pdf", report_run_id, html_record.schema_version, None, ok=False)
            LOG.exception("[ReportArtifact] PDF render failed run=%s", report_run_id)
            raise PdfRenderFailedArtifactError() from exc
        max_bytes = self._pdf_max_bytes if self._pdf_max_bytes is not None else pdf_max_bytes_from_env()
        try:
            pdf = validate_pdf_output(raw, max_bytes)
        except PdfArtifactError:
            self._log("pdf", report_run_id, html_record.schema_version, None, ok=False)
            raise
        metadata = {
            **self._metadata(), **(extra_metadata or {}),
            "source_html_artifact_id": html_record.id, "source_html_revision": html_record.revision,
            "source_final_report_artifact_id": report_record.id,
            "html_renderer_version": html_record.renderer_version,
            "pdf_engine": engine, "pdf_engine_version": engine_version,
        }
        try:
            record = self.store.add(report_run_id, ARTIFACT_PDF, content=pdf, content_type=CONTENT_TYPE_PDF,
                                    schema_version=html_record.schema_version,
                                    renderer_version=engine_tag(engine, engine_version), metadata=metadata)
        except ArtifactStoreError as exc:
            self._log("pdf", report_run_id, html_record.schema_version, None, ok=False)
            LOG.exception("[ReportArtifact] Persist failed run=%s type=%s", report_run_id, ARTIFACT_PDF)
            raise PdfPersistenceError() from exc
        self._log(ARTIFACT_PDF, report_run_id, record.schema_version, record.renderer_version, ok=True, record=record)
        return SavedPdf(record, html_record.id, report_record.id)

    # ── reads ─────────────────────────────────────────────────────────────

    def get_pdf(self, report_run_id: str, artifact_id: int) -> LoadedPdf:
        """The exact stored PDF ``artifact_id`` of a run, with its SHA-256 verified on every read.

        Raises ``PdfNotFoundError`` (no such PDF for that run, including ids of other runs/types) or
        ``PdfIntegrityError`` (stored bytes no longer match the recorded hash). Never renders or writes.
        """
        record = self.store.get_by_id(report_run_id, artifact_id, ARTIFACT_PDF)
        if record is None:
            raise PdfNotFoundError()
        return self._verified_pdf(record)

    def find_pdf(self, report_run_id: str, *, html_artifact_id: int) -> LoadedPdf | None:
        """Newest intact PDF generated from that exact HTML artifact, or ``None`` (pure read).

        Lets a caller reuse a PDF instead of generating another one, which matters because PDF bytes
        are not reproducible: a second render of the same HTML may hash differently.
        """
        # Candidates are chosen from metadata alone; only the matching PDFs' bytes are ever loaded.
        for artifact_id, _, metadata in reversed(self.store.list_metadata(report_run_id, ARTIFACT_PDF)):
            if metadata.get("source_html_artifact_id") != html_artifact_id:
                continue
            record = self.store.get_by_id(report_run_id, artifact_id, ARTIFACT_PDF)
            if record is None:
                continue
            try:
                return self._verified_pdf(record)
            except PdfIntegrityError:
                continue  # already logged; a corrupt copy is never offered for reuse
        return None

    def get_final_report(self, report_run_id: str) -> LoadedFinalReport | None:
        """Validated FinalReport of a run, or ``None`` when it has none (or it cannot be read).

        Prefers the persisted artifact and validates it with the model of ITS schema
        version; only a run with no artifact falls back to ``result_json`` (legacy).
        """
        record = self.store.get_latest(report_run_id, ARTIFACT_FINAL_REPORT)
        if record is not None:
            return self._load_artifact_report(record)
        raw = self.store.legacy_final_report_json(report_run_id)
        if raw is None:
            return None
        try:
            report = self.schemas.validate(raw)
        except Exception:
            LOG.exception("[ReportArtifact] Legacy FinalReport unreadable run=%s", report_run_id)
            return None
        return LoadedFinalReport(report, report.schema_version, ORIGIN_LEGACY)

    def get_html(self, report_run_id: str, *, revision: int | None = None,
                 persisted_only: bool = False) -> LoadedHtml | None:
        """The persisted HTML as stored, with no renderer involved.

        Reading policy (the only place it lives): without ``revision`` the ORIGINAL presentation
        is returned, i.e. the first intact revision, never the latest; regenerations are
        append-only extras and never become the historical default. ``revision=N`` selects one
        explicitly (``None`` if it does not exist or is corrupt; no fallback to another one).

        A run with a FinalReport but no HTML artifact (created before artifacts existed, or whose
        rendering failed) is rendered in memory with the schema's ORIGINAL renderer (fixed in the
        registry, unaffected by newer recommended renderers) and NOT persisted. ``persisted_only=True``
        disables that fallback (``None`` instead), for callers that need a stored artifact to point at.
        """
        if revision is not None:
            record = self.store.get_revision(report_run_id, ARTIFACT_HTML, revision)
            records = [] if record is None else [record]
        else:
            records = self.store.list(report_run_id, ARTIFACT_HTML)  # ordered by revision ascending
        for record in records:
            if record.is_intact() and record.text is not None:
                return LoadedHtml(record.text, record.schema_version, record.renderer_version, ORIGIN_ARTIFACT, record)
            LOG.error("[ReportArtifact] HTML integrity check failed run=%s artifact=%s revision=%s",
                      report_run_id, record.id, record.revision)
        if records or revision is not None or persisted_only:
            return None  # stored HTML exists but is unusable (or the requested revision does not): never re-render
        loaded = self.get_final_report(report_run_id)
        if loaded is None:
            return None
        try:
            version, renderer = self.renderers.get_original_for_schema(loaded.schema_version)
            return LoadedHtml(renderer.render(loaded.report), loaded.schema_version, version, ORIGIN_LEGACY)
        except Exception:
            LOG.exception("[ReportArtifact] Legacy HTML render failed run=%s", report_run_id)
            return None

    def export_source(self, report_run_id: str | None, raw_final_report: Mapping[str, Any] | None) -> ExportSource:
        """Trusted inputs for a PDF: HTML is never taken from the client.

        A run that has stored artifacts is authoritative (its own schema/renderer, exact
        stored HTML). Otherwise the client's FinalReport JSON is validated with the model
        of its declared version and rendered by that schema's default renderer.
        Raises ``VersionResolutionError`` / ``pydantic.ValidationError`` for an unusable report.
        """
        if report_run_id:
            html = self.get_html(report_run_id)
            report = self.get_final_report(report_run_id)
            if html is not None and report is not None:
                return ExportSource(report.report, html.html, report.schema_version, html.renderer_version, html.origin)
        if not isinstance(raw_final_report, Mapping):
            raise VersionResolutionError("No FinalReport available to export")
        report_model = self.schemas.validate(raw_final_report)
        version, renderer = self.renderers.get_for_schema(None, report_model.schema_version)
        return ExportSource(report_model, renderer.render(report_model), report_model.schema_version, version, "request")

    # ── internals ─────────────────────────────────────────────────────────

    def _source_html(self, report_run_id: str, artifact_id: int | None) -> ArtifactRecord:
        """The stored, intact HTML artifact a PDF is built from (never a legacy in-memory render)."""
        if artifact_id is None:
            loaded = self.get_html(report_run_id, persisted_only=True)
            record = loaded.artifact if loaded is not None else None
        else:
            record = self.store.get_by_id(report_run_id, artifact_id, ARTIFACT_HTML)
            if record is not None and not (record.is_intact() and record.text is not None):
                LOG.error("[ReportArtifact] HTML integrity check failed run=%s artifact=%s", report_run_id, record.id)
                record = None
        if record is None:
            raise PdfSourceUnavailableError("The persisted HTML is not available")
        return record

    def _source_final_report(self, report_run_id: str, artifact_id: int | None,
                             html_record: ArtifactRecord) -> ArtifactRecord:
        """The stored, valid FinalReport artifact the PDF's lineage points to.

        Default = the one the HTML was rendered from (its recorded ``source_artifact_id``), so the lineage
        is coherent even when a run has several FinalReports; the latest only when the HTML records none.
        """
        if artifact_id is None:
            artifact_id = html_record.metadata.get("source_artifact_id")
        if artifact_id is None:
            loaded = self.get_final_report(report_run_id)
        else:
            record = self.store.get_by_id(report_run_id, artifact_id, ARTIFACT_FINAL_REPORT)
            loaded = None if record is None else self._load_artifact_report(record)
        if loaded is None or loaded.artifact is None:  # missing, unreadable, or legacy (no row to reference)
            raise PdfSourceUnavailableError("The persisted FinalReport is not available")
        return loaded.artifact

    @staticmethod
    def _verified_pdf(record: ArtifactRecord) -> LoadedPdf:
        if record.data is None or not record.is_intact():
            LOG.error("[ReportArtifact] PDF integrity check failed run=%s artifact=%s",
                      record.report_run_id, record.id)
            raise PdfIntegrityError()
        return LoadedPdf(record.data, record.sha256, record.size_bytes, record)

    def _load_artifact_report(self, record: ArtifactRecord) -> LoadedFinalReport | None:
        if not record.is_intact() or record.text is None:
            LOG.error("[ReportArtifact] FinalReport integrity check failed run=%s artifact=%s",
                      record.report_run_id, record.id)
            return None
        try:
            report = self.schemas.validate(json.loads(record.text), record.schema_version)
        except Exception:
            LOG.exception("[ReportArtifact] FinalReport artifact unreadable run=%s artifact=%s schema=%s",
                          record.report_run_id, record.id, record.schema_version)
            return None
        return LoadedFinalReport(report, record.schema_version or report.schema_version, ORIGIN_ARTIFACT, record)

    def _add(self, report_run_id: str, artifact_type: str, content: str, content_type: str, *,
             schema_version: str | None, renderer_version: str | None = None,
             metadata: dict[str, Any]) -> ArtifactRecord:
        try:
            record = self.store.add(report_run_id, artifact_type, content=content, content_type=content_type,
                                    schema_version=schema_version, renderer_version=renderer_version,
                                    metadata=metadata)
        except ArtifactStoreError as exc:
            self._log(artifact_type, report_run_id, schema_version, renderer_version, ok=False)
            LOG.exception("[ReportArtifact] Persist failed run=%s type=%s", report_run_id, artifact_type)
            raise ArtifactPersistenceError(str(exc)) from exc
        self._log(artifact_type, report_run_id, schema_version, renderer_version, ok=True, record=record)
        return record

    @staticmethod
    def _metadata() -> dict[str, Any]:
        release = _release()
        return {"release": release} if release else {}

    @staticmethod
    def _log(artifact_type, run_id, schema_version, renderer_version, *, ok, record: ArtifactRecord | None = None):
        # Identifiers only: never the report/HTML content.
        LOG.log(logging.INFO if ok else logging.WARNING,
                "[ReportArtifact] run=%s type=%s schema=%s renderer=%s outcome=%s%s",
                run_id, artifact_type, schema_version, renderer_version, "ok" if ok else "failed",
                f" revision={record.revision} size_bytes={record.size_bytes} sha256={record.sha256[:12]}" if record else "")
