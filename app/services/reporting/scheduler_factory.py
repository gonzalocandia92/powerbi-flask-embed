"""Default wiring of the report scheduler (the only place that picks its concrete collaborators).

Same role as ``review_factory``/``pipeline_factory``: ``scheduler_main`` and the web layer ask for ready
objects; tests build ``ReportScheduler`` themselves with other collaborators.

``build_scheduler_phases`` is the ordered list of what one scheduler poll does. Stage 3 has the calendar
only; the freshness dispatcher and the run finalizer (stages 4-5) are appended here, nowhere else.
The process liveness (stage 7) is recorded by ``build_scheduler_heartbeat`` and read by ``scheduler_health``.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Mapping

from .concurrency import ConcurrencyCaps
from .dispatcher import DispatcherSettings, OccurrenceDispatcher
from .finalizer import FinalizerSettings, OccurrenceFinalizer
from .review_contracts import ReviewActor
from .review_factory import build_review_service
from .freshness import DataFreshnessService
from .freshness_sources import DatasetRefreshLogHistory, PowerBiRefreshHistory
from .heartbeat import (
    HEALTH_UNKNOWN, PROCESS_REPORT_SCHEDULER, HealthSettings, HeartbeatRecorder, ProcessIdentity, SchedulerHealth,
    assess_health,
)
from .heartbeat_store import HeartbeatStore
from .run_store import DefinitionNotRunnableError, ReportRunStore
from .schedule_store import ScheduleStore
from .scheduler import DefinitionUnavailable, ReportScheduler, SchedulerSettings

LOG = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def run_store_snapshot_builder(store: ReportRunStore | None = None) -> Callable[[int], dict]:
    """``ReportRunStore.build_snapshot`` with its errors translated into the scheduler's own."""
    store = store or ReportRunStore()

    def build(definition_id: int) -> dict:
        try:
            return store.build_snapshot(definition_id)
        except (DefinitionNotRunnableError, ValueError) as exc:
            raise DefinitionUnavailable(str(exc)) from exc

    return build


def build_report_scheduler(*, env: Mapping[str, str] | None = None, store: ScheduleStore | None = None,
                           clock: Clock | None = None) -> ReportScheduler:
    return ReportScheduler(store or ScheduleStore(), run_store_snapshot_builder(),
                           settings=SchedulerSettings.from_env(env), clock=clock)


def build_freshness_service() -> DataFreshnessService:
    """Persisted refresh log first (free), live Power BI history only when the log is not enough."""
    return DataFreshnessService([DatasetRefreshLogHistory(), PowerBiRefreshHistory()])


def build_dispatcher(*, env: Mapping[str, str] | None = None, store: ScheduleStore | None = None,
                     runs: ReportRunStore | None = None, clock: Clock | None = None) -> OccurrenceDispatcher:
    return OccurrenceDispatcher(store or ScheduleStore(), build_freshness_service(), runs=runs or ReportRunStore(),
                                caps=ConcurrencyCaps.from_env(env), settings=DispatcherSettings.from_env(env),
                                clock=clock)


def system_review_submitter() -> Callable[[str], object]:
    """``submit_review(run_id)`` as the scheduler: the system actor may prepare and submit, never decide."""
    actor = ReviewActor.system()
    return lambda run_id: build_review_service().submit_review(run_id, actor)


def build_finalizer(*, env: Mapping[str, str] | None = None, store: ScheduleStore | None = None,
                    runs: ReportRunStore | None = None, clock: Clock | None = None) -> OccurrenceFinalizer:
    return OccurrenceFinalizer(store or ScheduleStore(), runs or ReportRunStore(), system_review_submitter(),
                               settings=FinalizerSettings.from_env(env), clock=clock)


def approved_pdf_loader() -> Callable[[int], bytes]:
    """``load(review_id) -> bytes``: the review's exact PDF, with its SHA-256 verified by the review service."""
    return lambda review_id: build_review_service().get_review_pdf(review_id).data


def build_delivery_service(*, env: Mapping[str, str] | None = None, clock: Clock | None = None):
    """Distribution of approved scheduled reports. Inactive (a no-op phase) until it is enabled and configured."""
    from .delivery_service import DeliverySettings, ReportDeliveryService
    from .delivery_store import DeliveryStore
    from .whatsapp_channel import WhatsAppTemplateChannel
    settings = DeliverySettings.from_env(env)
    channel = WhatsAppTemplateChannel(settings.template_name, settings.template_language) if settings.active else None
    return ReportDeliveryService(DeliveryStore(), channel, approved_pdf_loader(), settings=settings, clock=clock)


def build_schedule_service(*, env: Mapping[str, str] | None = None):
    """What the admin UI uses to configure schedules (shares the scheduler's calendar and defaults)."""
    from .schedule_service import ScheduleService
    store = ScheduleStore()
    return ScheduleService(store, build_report_scheduler(env=env, store=store))


def build_schedule_history():
    from .schedule_history import ScheduleHistory, ledger_cost
    return ScheduleHistory(cost_of=ledger_cost)


def build_scheduler_phases(*, env: Mapping[str, str] | None = None,
                           clock: Clock | None = None) -> list[tuple[str, Callable[[], object]]]:
    """``(name, callable)`` pairs run in order by every poll; each one is independent of the others.
    Calendar first (a just-due occurrence is checked in the same poll), then freshness + enqueue, then the
    finalizer (closes the occurrences whose run finished in the worker), then the delivery of approved reports.
    ``clock`` is every phase's "now" (end-to-end scenarios drive time with it; the process uses the real one)."""
    store, runs = ScheduleStore(), ReportRunStore()
    return [
        ("calendar", build_report_scheduler(env=env, store=store, clock=clock).tick),
        ("dispatch", build_dispatcher(env=env, store=store, runs=runs, clock=clock).tick),
        ("finalize", build_finalizer(env=env, store=store, runs=runs, clock=clock).tick),
        ("deliver", build_delivery_service(env=env, clock=clock).tick),
    ]


def build_scheduler_heartbeat(*, interval_seconds: int | None, clock: Clock | None = None) -> HeartbeatRecorder:
    """The liveness recorder of THIS process (one identity per process: build it once)."""
    return HeartbeatRecorder(HeartbeatStore(), ProcessIdentity.current(PROCESS_REPORT_SCHEDULER),
                             interval_seconds=interval_seconds, clock=clock)


def scheduler_health(now: datetime, *, env: Mapping[str, str] | None = None) -> SchedulerHealth:
    """What the admin UI shows about the report-scheduler process. Never raises: unreadable -> ``unknown``."""
    try:
        latest = HeartbeatStore().latest(PROCESS_REPORT_SCHEDULER)
    except Exception:  # noqa: BLE001 - e.g. the heartbeat migration is not applied yet
        LOG.warning("[ReportScheduler] heartbeat could not be read", exc_info=True)
        return SchedulerHealth(status=HEALTH_UNKNOWN)
    return assess_health(latest, now, HealthSettings.from_env(env))
