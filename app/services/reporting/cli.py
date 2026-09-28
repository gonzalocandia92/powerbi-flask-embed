"""Authenticated-by-host manual CLI entry point for Reporting V0."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
from flask import current_app

from app import db
from app.models import Report
from app.services.analytics import AnalyticsError, build_analytics_engine

from .contracts import ReportDefinition, ReportQuestion, ReportSection
from .generator import ReportGenerator
from .renderer import render_markdown
from .usage import record_section_usage as _record_section_usage


def weekly_retail_questions() -> list[ReportQuestion]:
    """Example definition; the analytical semantics remain in KLARA skills."""
    return [
        ReportQuestion("sales", "Ventas",
                       "¿Cuánto se vendió durante la última semana cerrada y cómo varió respecto de la semana anterior?", 1),
        ReportQuestion("branches", "Sucursales",
                       "¿Cómo se distribuyeron las ventas de la última semana cerrada por sucursal y cuáles fueron las principales variaciones?", 2),
        ReportQuestion("average_ticket", "Ticket promedio",
                       "¿Cuál fue el ticket promedio de la última semana cerrada y cómo varió respecto de la semana anterior?", 3),
        ReportQuestion("payment_methods", "Medios de pago",
                       "¿Cómo se distribuyeron las ventas de la última semana cerrada por medio de pago?", 4),
        ReportQuestion("stock", "Stock",
                       "¿Cuál es la situación actual del stock y qué productos presentan estado crítico?", 5),
    ]


def _load_questions(path: str | None) -> list[ReportQuestion]:
    if path is None:
        return weekly_retail_questions()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("questions file must contain a JSON array")
    return [ReportQuestion(**item) for item in payload]


def register_reporting_command(app) -> None:
    @app.cli.command("generate-klara-report")
    @click.option("--report-id", type=int, required=True, help="Internal Report.id")
    @click.option("--name", default=None, help="Markdown report title")
    @click.option("--analysis-model-key", default=None, help="Enabled internal model key")
    @click.option("--analysis-service-tier", default=None, help="Main-model tier: flex or standard")
    @click.option("--questions-file", type=click.Path(exists=True, dir_okay=False), default=None,
                  help="JSON array of {key,title,question,order} objects")
    def generate_klara_report(report_id, name, analysis_model_key, analysis_service_tier, questions_file):
        """Generate Markdown once from the host; no scheduler or ReportRun table."""
        report = db.session.get(Report, report_id)
        if report is None:
            raise click.ClickException(f"Report not found: {report_id}")
        try:
            definition = ReportDefinition(
                report_id=report_id, name=name or f"Informe semanal — {report.name}",
                questions=_load_questions(questions_file),
                analysis_model_key=analysis_model_key,
                analysis_service_tier=analysis_service_tier,
            )
            analytics = build_analytics_engine(dict(current_app.config))

            async def record_usage(section: ReportSection) -> None:
                await asyncio.to_thread(_record_section_usage, report_id, section)

            draft = asyncio.run(ReportGenerator(analytics, record_usage=record_usage).generate(definition))
        except (AnalyticsError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(render_markdown(draft), nl=False)
