"""Analytical reporting: analysis (ReportGenerator) -> draft -> writing -> rendering.

Stages are independent and replaceable: ``ReportGenerator`` produces a
``ReportDraft`` through ``AnalyticsExecutor``; a ``ReportWriter`` turns the draft
into a validated ``FinalReport`` without touching Power BI, DAX or skills; a
renderer (``HtmlReportRenderer``) presents it. ``ReportPipeline`` composes them.
"""

from .contracts import ReportDefinition, ReportDraft, ReportQuestion, ReportSection
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
    "FinalReport", "HtmlReportRenderer", "LLMReportWriter", "ReportDefinition", "ReportDraft",
    "ReportGenerator", "ReportPipeline", "ReportPipelineResult", "ReportQuestion", "ReportSection",
    "ReportWriter", "ReportWriterConfigurationError", "ReportWriterError",
    "ReportWriterExecutionError", "ReportWriterInvalidOutputError", "render_markdown",
]
