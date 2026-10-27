"""Executes one claimed ``ReportRun`` through the existing ``ReportPipeline``.

Orchestration only: it rebuilds the ``ReportDefinition`` from the run's snapshot,
builds the pipeline, injects the run id and a durable ``ReportProgress`` and maps the
outcome back to the run row. It knows nothing about Flask routes, the scheduler or
the renderer's internals, so it can be driven by any worker process. Outputs go through
``ArtifactService`` (FinalReport first, then its HTML, each committed on its own), so a render
or storage failure never discards the already-paid FinalReport.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from app.services import ai_billing
from app.services.analytics import (
    AnalyticsBillingLimitExceededError, AnalyticsConfigurationError, AnalyticsModelError,
    AnalyticsReportNotFoundError,
)

from .artifact_service import ArtifactPersistenceError, ArtifactRenderError, ArtifactService
from .contracts import ReportDefinition, ReportSection
from .db_scope import isolated_db_scope
from .payloads import coordination_payload, writer_payload
from .pipeline import ReportPipeline, ReportPipelineResult
from .run_store import (
    DEFAULT_LEASE_SECONDS, STATUS_CANCELLED, STATUS_COMPLETED, STATUS_COMPLETED_WITH_ERRORS, STATUS_FAILED,
    LeaseLostError, ReportRunStore, RunCancelledError,
)
from .snapshot import definition_from_snapshot
from .writer import ReportWriterConfigurationError

LOG = logging.getLogger(__name__)

PipelineBuilder = Callable[[dict, ReportDefinition], ReportPipeline]


def report_observability(final_report, renderer_version: str | None, generation_mode: str | None = None) -> dict | None:
    """Schema / renderer / visual-component counts of a produced report (``None`` when there is none)."""
    if final_report is None:
        return None
    info = {"final_report_schema": getattr(final_report, "schema_version", None), "renderer": renderer_version,
            "generation_mode": generation_mode}
    for name in ("visual_component_count", "chart_count"):
        counter = getattr(final_report, name, None)
        if callable(counter):
            info[name] = counter()
    LOG.info("[ReportRun] final_report_schema=%s renderer=%s generation_mode=%s visual_component_count=%s chart_count=%s",
             info["final_report_schema"], renderer_version, generation_mode, info.get("visual_component_count"),
             info.get("chart_count"))
    return info


class DurableProgress:
    """``ReportProgress`` backed by the run store: stage, sections, heartbeat and cancellation."""

    def __init__(self, store: ReportRunStore, run_id: str, worker_id: str, definition: ReportDefinition):
        self.store, self.run_id, self.worker_id = store, run_id, worker_id
        # Logical position = rank in the definition's order (stable, unique), NOT completion order.
        ranked = sorted(definition.questions, key=lambda item: item.order)
        self._positions = {item.key: index for index, item in enumerate(ranked, start=1)}
        self._extra_position = len(ranked)

    def checkpoint(self) -> None:
        """Called before an analysis starts: stop (cancellation) instead of starting it."""
        self.store.raise_if_cancel_requested(self.run_id)

    def stage(self, stage: str) -> None:
        self.store.raise_if_cancel_requested(self.run_id)
        self.store.set_stage(self.run_id, self.worker_id, stage)

    def section(self, section: ReportSection) -> None:
        position = self._positions.get(section.key)
        if position is None:  # coordinator-requested analysis: appended after the definition's questions
            self._extra_position += 1
            position = self._extra_position
        self.store.save_section(self.run_id, self.worker_id, section, position=position)
        self.store.raise_if_cancel_requested(self.run_id)


def default_heartbeat_seconds(lease_seconds: float) -> float:
    """Renew well inside the lease: a third of it, at most a minute."""
    return max(0.05, min(60.0, lease_seconds / 3))


def _failure(exc: Exception) -> tuple[str, str]:
    """Public (code, message) for a global failure; details stay in the log."""
    if isinstance(exc, (AnalyticsBillingLimitExceededError, ai_billing.BillingLimitExceeded)):
        return "billing_limit_exceeded", "Se alcanzó el límite de consumo de IA para este Report."
    if isinstance(exc, AnalyticsReportNotFoundError):
        return "report_not_found", "El Report seleccionado ya no existe."
    if isinstance(exc, AnalyticsModelError):
        return "model_unavailable", "El modelo o tier elegido no está disponible para esta ejecución."
    if isinstance(exc, AnalyticsConfigurationError):
        return "configuration_error", "La configuración o el pricing del análisis no permiten generar el informe."
    if isinstance(exc, ReportWriterConfigurationError):
        return exc.code, str(exc)
    return "execution_failed", "No se pudo generar el informe. Revisá los logs del worker."


def _outcome(result: ReportPipelineResult, *, degraded: bool = False) -> tuple[str, str | None, str | None]:
    """(status, error_code, error_message) for a pipeline that returned normally."""
    ok_sections = [s for s in result.draft.sections if not s.had_error]
    if not ok_sections and result.writer_error is not None:
        return STATUS_FAILED, result.writer_error.code, "Ningún análisis finalizó correctamente."
    degraded = (degraded or len(ok_sections) < len(result.draft.sections) or result.writer_error is not None
                or result.render_error is not None)
    return (STATUS_COMPLETED_WITH_ERRORS if degraded else STATUS_COMPLETED), None, None


class ReportRunExecutor:
    def __init__(self, store: ReportRunStore, build_pipeline: PipelineBuilder, *, config: dict,
                 artifacts: ArtifactService | None = None, heartbeat_seconds: float | None = None):
        self.store, self.build_pipeline, self.config = store, build_pipeline, config
        self.artifacts = artifacts or ArtifactService()
        self.heartbeat_seconds = heartbeat_seconds if heartbeat_seconds is not None else (
            default_heartbeat_seconds(getattr(store, "lease_seconds", DEFAULT_LEASE_SECONDS)))

    def execute(self, run_id: str, worker_id: str) -> None:
        run = self.store.get(run_id)
        if run is None:
            return
        pipeline = None
        try:
            definition = definition_from_snapshot(run.definition_snapshot_json)
            progress = DurableProgress(self.store, run_id, worker_id, definition)
            pipeline = self.build_pipeline(self.config, definition)  # preflight: writer resolution
            already_done = self.store.load_sections(run_id, origin="definition")
            result = asyncio.run(self._run_pipeline(
                pipeline, definition, run_id, worker_id, progress, already_done))
        except LeaseLostError:
            LOG.warning("Report run %s lease lost; leaving it to the new owner", run_id)
            return
        except RunCancelledError:
            self._finish(run_id, worker_id, status=STATUS_CANCELLED,
                         result=self._execution_metadata(pipeline) or None)
            return
        except Exception as exc:  # noqa: BLE001 - every failure must end in a terminal state
            code, message = _failure(exc)
            LOG.exception("[ReportRun] run=%s failed code=%s", run_id, code)
            self._finish(run_id, worker_id, status=STATUS_FAILED, error_code=code, error_message=message,
                         result=self._execution_metadata(pipeline) or None)
            return
        observed: dict = {}
        artifact_errors = self._persist_artifacts(run_id, result, observed)
        status, code, message = _outcome(result, degraded=bool(artifact_errors))
        payload = {
            "writer": writer_payload(result),
            # DEPRECATED mirror: the FinalReport artifact is the source of truth. Kept (dual
            # write) so runs stay readable by code that predates artifacts and so a storage
            # failure of the artifact does not lose paid writer output. Readers use artifacts first.
            "final_report": result.final_report.model_dump(mode="json") if result.final_report else None,
            "artifact_errors": artifact_errors,
            # Operational facts about WHAT was produced (never its content): schema, renderer, visual counts.
            "report": report_observability(result.final_report, observed.get("renderer_version"),
                                           result.generation_mode),
            "coordination": coordination_payload(result.coordination),
            "usage_record_failures": result.usage_record_failures
            + (result.coordination.extra_usage_record_failures if result.coordination else 0),
            **self._execution_metadata(pipeline),
        }
        self._finish(run_id, worker_id, status=status, result=payload, error_code=code, error_message=message)

    async def _run_pipeline(self, pipeline: ReportPipeline, definition: ReportDefinition, run_id: str,
                            worker_id: str, progress: DurableProgress, already_done) -> ReportPipelineResult:
        """Run the pipeline next to a periodic lease heartbeat.

        The heartbeat is independent from analysis completion: with several long analyses in
        flight no section may finish for a long time, yet the worker still owns the run. If the
        lease is lost the heartbeat ends with ``LeaseLostError``: the pipeline is cancelled (its
        in-flight analyses with it) and the error is propagated, never hidden.
        """
        work = asyncio.ensure_future(pipeline.run(
            definition, report_run_id=run_id, progress=progress, completed_sections=already_done))
        beat = asyncio.ensure_future(self._heartbeat_loop(run_id, worker_id))
        try:
            await asyncio.wait({work, beat}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                return work.result()
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            beat.result()  # raises LeaseLostError
            raise RuntimeError("heartbeat stopped unexpectedly")
        finally:
            pending = [task for task in (work, beat) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _heartbeat_loop(self, run_id: str, worker_id: str) -> None:
        # Own DB session: the pipeline's worker threads may be using the run's session right now.
        with isolated_db_scope():
            while True:
                await asyncio.sleep(self.heartbeat_seconds)
                try:
                    self.store.heartbeat(run_id, worker_id)
                except LeaseLostError:
                    LOG.warning("Report run %s heartbeat: lease lost", run_id)
                    raise
                except Exception:  # noqa: BLE001 - a transient DB error must not kill a healthy run
                    LOG.warning("Report run %s heartbeat failed; will retry", run_id, exc_info=True)

    @staticmethod
    def _execution_metadata(pipeline) -> dict:
        """Operational facts of the analysis stage (concurrency, peak, duration); never report content."""
        stats = getattr(getattr(pipeline, "generator", None), "last_analysis_stats", None)
        return {"analysis": stats.to_payload()} if stats is not None else {}

    def _persist_artifacts(self, run_id: str, result: ReportPipelineResult, observed: dict | None = None) -> list[str]:
        """FinalReport first, then its HTML; each step is independent and committed on its own.

        A failure never discards the previous step (the paid FinalReport survives a render
        or HTML-storage failure) and never raises: it returns public error codes.
        """
        if result.final_report is None:
            return []
        errors: list[str] = []
        source = None
        # Only the non-default way of producing a report is recorded on the artifacts (no special artifact type).
        extra = {"generation_mode": result.generation_mode} if result.generation_mode == "fallback" else None
        try:
            source = self.artifacts.save_final_report(run_id, result.final_report, extra_metadata=extra)
        except ArtifactPersistenceError:
            errors.append("final_report_artifact_failed")  # result_json still carries the report
        try:
            html = self.artifacts.render_and_save_html(run_id, result.final_report, source=source,
                                                       extra_metadata=extra)
            if observed is not None:
                observed["renderer_version"] = html.renderer_version
        except ArtifactRenderError:
            result.render_error = "report_render_failed"
            errors.append("html_render_failed")
        except ArtifactPersistenceError:
            errors.append("html_artifact_failed")
        return errors

    def _finish(self, run_id: str, worker_id: str, **kwargs) -> None:
        try:
            self.store.finish_run(run_id, worker_id, **kwargs)
        except LeaseLostError:
            LOG.warning("Report run %s lease lost before it could be finished", run_id)
