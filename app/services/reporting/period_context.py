"""How a run's FROZEN analytical period reaches the analyses and the report (Phase 3).

Pure functions. Only scheduled runs carry a period (``ReportDefinition.period``); without one every function
is the identity, so manual runs keep behaving exactly as before.

* ``analysis_question`` prefixes the text SENT to the analytics agent with a deterministic block of explicit
  dates, so "last week" means the frozen week even if Power BI refreshed a day late. The question STORED in
  the section stays the one the user wrote.
* ``report_period`` is the ``ReportPeriod`` the final report shows; it replaces whatever the writer put there,
  because the frozen period is the source of truth.
"""
from __future__ import annotations

from .contracts import ReportDefinition
from .period_resolver import ResolvedPeriod
from .report_content import ReportPeriod


def _fmt(day) -> str:
    return day.strftime("%d/%m/%Y")


def period_preamble(period: ResolvedPeriod) -> str:
    return (
        f"Período analizado: del {_fmt(period.local_start)} al {_fmt(period.local_end)} inclusive "
        f"(zona horaria {period.timezone}). "
        f"Período de comparación: {period.comparison_label}. "
        "Usá exactamente estas fechas para toda referencia temporal relativa "
        "(por ejemplo «la semana pasada», «el mes anterior» o «este período»)."
    )


def analysis_question(definition: ReportDefinition, question: str) -> str:
    period = getattr(definition, "period", None)
    if period is None:
        return question
    return f"{period_preamble(period)}\n\nPregunta: {question}"


def report_period(period: ResolvedPeriod | None) -> ReportPeriod | None:
    if period is None:
        return None
    return ReportPeriod(label=period.label, start=_fmt(period.local_start), end=_fmt(period.local_end),
                        comparison_label=period.comparison_label)
