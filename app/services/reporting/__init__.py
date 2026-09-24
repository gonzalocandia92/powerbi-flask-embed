"""Manual analytical reporting; writing is a separate future stage.

A future ReportWriter may consume ReportDraft and call an LLM directly, without
AnalyticsExecutor, Power BI, skills or DAX. No writer is implemented in V0.
"""

from .contracts import ReportDefinition, ReportDraft, ReportQuestion, ReportSection
from .generator import ReportGenerator
from .renderer import render_markdown

__all__ = ["ReportDefinition", "ReportDraft", "ReportGenerator", "ReportQuestion",
           "ReportSection", "render_markdown"]
