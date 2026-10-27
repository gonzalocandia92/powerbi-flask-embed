"""Analytical reporting: analysis (ReportGenerator) -> [coordination] -> writing -> rendering.

Stages are independent and replaceable: ``ReportGenerator`` produces a
``ReportDraft`` through ``AnalyticsExecutor``; an optional ``ReportCoordinator``
(V1.1, see ``coordinator.py``/``coordination.py``) may enrich that draft with up
to two coordinator-requested analyses, executed through the same
``AnalyticsExecutor`` via ``extra_analysis.run_analysis``; a ``ReportWriter``
turns the (possibly enriched) draft into a validated ``FinalReport`` without
touching Power BI, DAX or skills; a renderer (``HtmlReportRenderer``) presents
it. ``ReportPipeline`` composes them all. PDF export (``pdf_renderer``) is a separate,
optional presentation step over the rendered HTML; the pipeline knows nothing about it. The review workflow
(``review_service``: staff approve/reject a persisted PDF) is likewise independent of the pipeline; only its
pure contracts and service are exported here, the SQL adapter and the wiring live in ``review_store`` /
``review_factory`` so importing this package never pulls in the ORM.
"""

from .contracts import ReportDefinition, ReportDraft, ReportQuestion, ReportSection
from .coordination import CoordinationOutcome, CoordinationRunner
from .coordinator import (
    COORDINATOR_ROLE, CoordinatorAttempt, CoordinatorConfigurationError, CoordinatorError,
    CoordinatorExecutionError, CoordinatorInvalidDecisionError, LLMReportCoordinator, ReportCoordinator,
    build_coordinator_input,
)
from .coordinator_contracts import (
    MAX_COORDINATOR_ROUNDS, MAX_EXTRA_ANALYSES, CoordinatorConstraints, CoordinatorDecision, CoordinatorInput,
    CoordinatorSection, CoordinatorSemanticNote, RequestedAnalysis,
)
from .cost import ReportCostService, ReportCostSummary, format_usd, summarize_events
from .extra_analysis import run_analysis
from .final_report import FinalReport
from .generator import ReportGenerator
from .html_renderer import HtmlReportRenderer
from .pdf_artifacts import (
    LoadedPdf, PdfArtifactError, PdfIntegrityError, PdfInvalidOutputError, PdfNotFoundError, PdfPersistenceError,
    PdfRenderFailedArtifactError, PdfSourceUnavailableError, PdfTooLargeError, SavedPdf,
)
from .pdf_export import report_pdf_filename
from .pdf_renderer import (
    PdfBrowserUnavailableError, PdfRenderer, PdfRenderError, PdfRenderFailedError, WeasyPrintPdfRenderer,
    build_pdf_renderer,
)
from .pipeline import ReportPipeline, ReportPipelineResult
from .renderer import render_markdown
from .review_contracts import (
    ReviewActor, ReviewConflictError, ReviewEligibility, ReviewError, ReviewNotEligibleError, ReviewNotFoundError,
    ReviewPdfError, ReviewPermissionError, ReviewStoreError, ReviewValidationError,
)
from .review_service import ReviewService
from .writer import (
    LLMReportWriter, ReportWriter, ReportWriterConfigurationError, ReportWriterError,
    ReportWriterExecutionError, ReportWriterInvalidOutputError,
)

__all__ = [
    "COORDINATOR_ROLE", "CoordinationOutcome", "CoordinationRunner", "CoordinatorAttempt",
    "CoordinatorConfigurationError", "CoordinatorConstraints", "CoordinatorDecision", "CoordinatorError",
    "CoordinatorExecutionError", "CoordinatorInput", "CoordinatorInvalidDecisionError", "CoordinatorSection",
    "CoordinatorSemanticNote", "FinalReport", "HtmlReportRenderer", "LLMReportCoordinator", "LLMReportWriter",
    "MAX_COORDINATOR_ROUNDS", "MAX_EXTRA_ANALYSES", "PdfBrowserUnavailableError", "PdfRenderError",
    "PdfRenderFailedError", "PdfRenderer", "WeasyPrintPdfRenderer", "ReportCoordinator", "ReportDefinition", "ReportDraft",
    "ReportCostService", "ReportCostSummary", "ReportGenerator", "ReportPipeline", "ReportPipelineResult", "ReportQuestion", "RequestedAnalysis",
    "ReportSection", "ReportWriter", "ReportWriterConfigurationError", "ReportWriterError",
    "ReportWriterExecutionError", "ReportWriterInvalidOutputError", "build_coordinator_input",
    "LoadedPdf", "PdfArtifactError", "PdfIntegrityError", "PdfInvalidOutputError", "PdfNotFoundError",
    "PdfPersistenceError", "PdfRenderFailedArtifactError", "PdfSourceUnavailableError", "PdfTooLargeError", "SavedPdf",
    "ReviewActor", "ReviewConflictError", "ReviewEligibility", "ReviewError", "ReviewNotEligibleError",
    "ReviewNotFoundError", "ReviewPdfError", "ReviewPermissionError", "ReviewService", "ReviewStoreError",
    "ReviewValidationError",
    "build_pdf_renderer", "format_usd", "render_markdown", "report_pdf_filename", "run_analysis", "summarize_events",
]
