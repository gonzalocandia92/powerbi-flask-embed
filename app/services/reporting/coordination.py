"""CoordinationRunner: the ONLY place that wires ReportCoordinator + extra analyses together.

Kept apart from ``ReportPipeline`` so the pipeline stays a thin composer (see its
module docstring): this module owns the single allowed extra round
(``max_coordinator_rounds=1``, ``max_extra_analyses=2``), the fact that a
coordinator failure of any kind never invalidates the draft (V1.1 spec §18),
and turning an initial ``ReportDraft`` into an enriched one when the coordinator
asks for extra analyses (V1.1 spec §12).

``ReportPipeline`` composes this in only when the report's strategy is
"coordinated"; in "fixed" mode ``ReportPipeline`` never even imports a
coordinator, so a broken/unassigned ``report_coordinator`` role cannot affect a
fixed run (V1.1 spec §7/§17).
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.services.analytics import AnalyticsExecutor

from .contracts import ReportDefinition, ReportDraft, ReportSection
from .coordinator import CoordinatorAttempt, CoordinatorError, ReportCoordinator, build_coordinator_input
from .progress import ReportProgress, notify_section
from .coordinator_contracts import MAX_EXTRA_ANALYSES, CoordinatorDecision
from .extra_analysis import run_analysis

LOG = logging.getLogger(__name__)

CoordinatorUsageRecorder = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ExtraAnalysisUsageRecorder = Callable[[ReportSection], Awaitable[None] | None]
CoordinatorResolver = Callable[[], ReportCoordinator]


@dataclass
class CoordinationOutcome:
    """Everything the admin UI needs to show about one coordination round.

    ``draft`` is always usable by ``ReportWriter``: on any coordinator failure it
    is exactly the draft this runner was given (never partially mutated).
    """
    draft: ReportDraft
    ran: bool = False
    decision: CoordinatorDecision | None = None
    # {"code", "message"} when coordinator resolution or execution failed safely.
    coordinator_error: dict[str, Any] | None = None
    # One summary() dict per LLM call (initial + repair), same shape as writer_attempts.
    attempts: list[dict[str, Any]] = field(default_factory=list)
    extra_sections: list[ReportSection] = field(default_factory=list)
    extra_usage_record_failures: int = 0

    @property
    def enriched(self) -> bool:
        return bool(self.extra_sections)


class CoordinationRunner:
    """Runs at most one coordination round for a single ``ReportDraft``."""

    def __init__(
        self, resolve_coordinator: CoordinatorResolver, analytics: AnalyticsExecutor, *,
        record_coordinator_usage: CoordinatorUsageRecorder | None = None,
        record_extra_usage: ExtraAnalysisUsageRecorder | None = None,
    ):
        self._resolve_coordinator = resolve_coordinator
        self.analytics = analytics
        self.record_coordinator_usage = record_coordinator_usage
        self.record_extra_usage = record_extra_usage

    async def run(self, draft: ReportDraft, definition: ReportDefinition,
                  *, progress: ReportProgress | None = None) -> CoordinationOutcome:
        outcome = CoordinationOutcome(draft=draft)

        try:
            coordinator = self._resolve_coordinator()
        except CoordinatorError as exc:
            outcome.coordinator_error = {"code": exc.code, "message": str(exc)}
            LOG.warning("Report coordinator could not be resolved run=%s code=%s",
                       draft.report_run_id, exc.code)
            return outcome

        coordinator_input = build_coordinator_input(draft)

        async def on_attempt(attempt: CoordinatorAttempt) -> None:
            outcome.attempts.append(attempt.summary())
            if self.record_coordinator_usage is None:
                return
            try:
                recorded = self.record_coordinator_usage(draft.report_run_id, attempt.usage_event)
                if inspect.isawaitable(recorded):
                    await recorded
            except Exception:
                LOG.exception("Report coordinator usage could not be recorded run=%s", draft.report_run_id)

        try:
            decision = await coordinator.coordinate(coordinator_input, on_attempt=on_attempt)
        except CoordinatorError as exc:
            outcome.ran = True
            outcome.coordinator_error = {"code": exc.code, "message": str(exc)}
            LOG.warning("Report coordinator failed safely run=%s code=%s", draft.report_run_id, exc.code)
            return outcome

        outcome.ran = True
        outcome.decision = decision
        if decision.action != "run_analysis" or not decision.analyses:
            return outcome

        analyses = decision.analyses[:MAX_EXTRA_ANALYSES]
        enriched_sections = list(draft.sections)
        for index, requested in enumerate(analyses, start=1):
            section = await run_analysis(
                requested, definition=definition, analytics=self.analytics,
                report_run_id=draft.report_run_id, section_key=f"extra_{index:03}",
            )
            enriched_sections.append(section)
            outcome.extra_sections.append(section)
            await notify_section(progress, section)
            if self.record_extra_usage and section.ai_usage_events:
                try:
                    recorded = self.record_extra_usage(section)
                    if inspect.isawaitable(recorded):
                        await recorded
                except Exception:
                    outcome.extra_usage_record_failures += 1
                    LOG.exception("Extra analysis usage could not be recorded run=%s key=%s",
                                  draft.report_run_id, section.key)

        outcome.draft = ReportDraft(
            report_run_id=draft.report_run_id, report_id=draft.report_id, name=draft.name,
            analysis_model_key=draft.analysis_model_key, analysis_service_tier=draft.analysis_service_tier,
            sections=enriched_sections,
        )
        return outcome
