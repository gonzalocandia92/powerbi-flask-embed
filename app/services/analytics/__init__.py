"""Consumer-agnostic analytical execution API."""

from .contracts import (
    AnalyticsBillingLimitExceededError,
    AnalyticsConfigurationError,
    AnalyticsError,
    AnalyticsExecutor,
    AnalyticsModelError,
    AnalyticsReportNotFoundError,
    AnalyticsRequest,
    AnalyticsResult,
)
from .engine import KlaraAnalyticsEngine, build_analytics_engine

__all__ = [
    "AnalyticsBillingLimitExceededError",
    "AnalyticsConfigurationError",
    "AnalyticsError",
    "AnalyticsExecutor",
    "AnalyticsModelError",
    "AnalyticsReportNotFoundError",
    "AnalyticsRequest",
    "AnalyticsResult",
    "KlaraAnalyticsEngine",
    "build_analytics_engine",
]
