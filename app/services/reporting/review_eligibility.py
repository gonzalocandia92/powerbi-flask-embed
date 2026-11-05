"""Can a run be put under review? An ordered chain of small rules (first objection wins).

The rules look at the FINAL outcome of the run, not at its history. Both ``completed`` and
``completed_with_errors`` are reviewable (``run_states.REVIEWABLE_STATUSES``): a degraded run still produced a
report, the reviewer sees its status and decides; one whose report was not persisted is blocked by
``PersistedSourcesRule``. Adding a requirement (e.g. "the report must have a definition") is one more
``EligibilityRule`` in the factory.

Reading is pure: nothing here creates a review, renders or writes.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from app.persistence.run_states import REVIEWABLE_STATUSES

from .review_contracts import (
    FINAL_REPORT_MISSING, HTML_UNAVAILABLE, ORIGIN_ARTIFACT, RUN_NOT_COMPLETED, RUN_NOT_FOUND, EligibilityRule,
    ReviewArtifacts, ReviewEligibility, RunView,
)


class RunCompletedRule:
    """Only finished runs with a reviewable status (``completed`` and ``completed_with_errors`` by default)."""

    def __init__(self, statuses: Iterable[str] = REVIEWABLE_STATUSES):
        self.statuses = frozenset(statuses)

    def check(self, run: RunView, artifacts: ReviewArtifacts) -> ReviewEligibility | None:
        if run.status in self.statuses:
            return None
        return ReviewEligibility.blocked(
            RUN_NOT_COMPLETED, "La ejecución todavía no terminó correctamente.", run.status)


class PersistedSourcesRule:
    """The FinalReport and its HTML must exist as intact stored artifacts (a review points at rows, not at memory).

    "Missing" and "stored but failing its SHA-256" are both reported as unavailable: neither can be put under
    review, and the reading code already logs which one it was.
    """

    def check(self, run: RunView, artifacts: ReviewArtifacts) -> ReviewEligibility | None:
        report = artifacts.get_final_report(run.id)
        if report is None:
            return ReviewEligibility.blocked(
                FINAL_REPORT_MISSING, "El informe estructurado no está disponible o no es legible.", run.status)
        if getattr(report, "origin", ORIGIN_ARTIFACT) != ORIGIN_ARTIFACT:
            return self._unavailable(run)  # legacy run: nothing persisted to point at
        html = artifacts.get_html(run.id, persisted_only=True)  # never renders: a stored, intact copy or nothing
        if html is None or html.artifact is None:
            return self._unavailable(run)
        return None

    @staticmethod
    def _unavailable(run: RunView) -> ReviewEligibility:
        return ReviewEligibility.blocked(
            HTML_UNAVAILABLE, "El informe persistido no está disponible para generar el PDF.", run.status)


def default_rules() -> tuple[EligibilityRule, ...]:
    return (RunCompletedRule(), PersistedSourcesRule())


class EligibilityChecker:
    def __init__(self, rules: Sequence[EligibilityRule] | None = None):
        self.rules = tuple(default_rules() if rules is None else rules)

    def check(self, run: RunView | None, artifacts: ReviewArtifacts) -> ReviewEligibility:
        if run is None:
            return ReviewEligibility.blocked(RUN_NOT_FOUND, "La ejecución no existe.")
        for rule in self.rules:
            verdict = rule.check(run, artifacts)
            if verdict is not None:
                return verdict
        return ReviewEligibility.ok(run.status)
