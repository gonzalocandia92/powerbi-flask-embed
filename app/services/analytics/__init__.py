"""Consumer-agnostic analytical execution API."""

from .contracts import (
    AnalyticsBillingLimitExceededError,
    AnalyticsConfigurationError,
    AnalyticsError,
    AnalyticsExecutor,
    AnalyticsFailure,
    AnalyticsModelError,
    AnalyticsReportNotFoundError,
    AnalyticsRequest,
    AnalyticsResult,
    PreparedAnalyticsExecution,
)
from .engine import KlaraAnalyticsEngine, build_analytics_engine
from .retry import (
    AnalyticsRetryPolicy,
    RetryingAnalyticsExecutor,
    TransientFailureRetryPolicy,
    build_retry_policy,
)
from .report_skills import ReportSkillCatalog, list_effective_skills_for_report

__all__ = [
    "AnalyticsBillingLimitExceededError",
    "AnalyticsConfigurationError",
    "AnalyticsError",
    "AnalyticsExecutor",
    "AnalyticsFailure",
    "AnalyticsRetryPolicy",
    "RetryingAnalyticsExecutor",
    "TransientFailureRetryPolicy",
    "build_retry_policy",
    "AnalyticsModelError",
    "AnalyticsReportNotFoundError",
    "AnalyticsRequest",
    "AnalyticsResult",
    "PreparedAnalyticsExecution",
    "KlaraAnalyticsEngine",
    "build_analytics_engine",
    "ReportSkillCatalog",
    "list_effective_skills_for_report",
]
