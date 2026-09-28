"""Analytical reporting: analysis (ReportGenerator) -> [coordination] -> writing -> rendering.

Stages are independent and replaceable: ``ReportGenerator`` produces a
``ReportDraft`` through ``AnalyticsExecutor``; an optional ``ReportCoordinator``
(V1.1, see ``coordinator.py``/``coordination.py``) may enrich that draft with up
to two coordinator-requested analyses, executed through the same
``AnalyticsExecutor`` via ``extra_analysis.run_analysis``; a ``ReportWriter``
turns the (possibly enriched) draft into a validated ``FinalReport`` without
touching Power BI, DAX or skills; a renderer (``HtmlReportRenderer``) presents
it. ``ReportPipeline`` composes them all.
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
from .extra_analysis import run_analysis
from .final_report import FinalReport
from .generator import ReportGenerator
from .html_renderer import HtmlReportRenderer
from .pipeline import ReportPipeline, ReportPipelineResult
from .renderer import render_markdown
from .writer import (
    LLMReportWriter, ReportWriter, ReportWriterConfigurationError, ReportWriterError,
    ReportWriterExecutionError, ReportWriterInvalidOutputError,
)

__all__ = [
    "COORDINATOR_ROLE", "CoordinationOutcome", "CoordinationRunner", "CoordinatorAttempt",
    "CoordinatorConfigurationError", "CoordinatorConstraints", "CoordinatorDecision", "CoordinatorError",
    "CoordinatorExecutionError", "CoordinatorInput", "CoordinatorInvalidDecisionError", "CoordinatorSection",
    "CoordinatorSemanticNote", "FinalReport", "HtmlReportRenderer", "LLMReportCoordinator", "LLMReportWriter",
    "MAX_COORDINATOR_ROUNDS", "MAX_EXTRA_ANALYSES", "ReportCoordinator", "ReportDefinition", "ReportDraft",
    "ReportGenerator", "ReportPipeline", "ReportPipelineResult", "ReportQuestion", "RequestedAnalysis",
    "ReportSection", "ReportWriter", "ReportWriterConfigurationError", "ReportWriterError",
    "ReportWriterExecutionError", "ReportWriterInvalidOutputError", "build_coordinator_input",
    "render_markdown", "run_analysis",
]
