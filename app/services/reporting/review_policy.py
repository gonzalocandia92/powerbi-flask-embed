"""Who may do what in the review workflow.

Defence in depth: the web layer checks ``is_staff`` too, but the service asks this policy on every
action so a caller that forgets (a script, the future scheduler) cannot bypass it. ``is_admin`` is
deliberately not consulted: administrators who are not staff do not review.

Swap or extend the policy (e.g. a dedicated ``reports.review`` permission) without touching the service.
"""
from __future__ import annotations

from .review_contracts import (
    ACTION_COMMENT, ACTION_DECIDE, ACTION_PREPARE, ACTION_SUBMIT, KIND_SYSTEM, KIND_USER, ReviewActor,
)


class StaffReviewPolicy:
    """Humans need ``is_staff`` and an active account; automation may prepare and submit, never decide."""

    _SYSTEM_ACTIONS = frozenset({ACTION_PREPARE, ACTION_SUBMIT})
    _STAFF_ACTIONS = frozenset({ACTION_PREPARE, ACTION_SUBMIT, ACTION_DECIDE, ACTION_COMMENT})

    def allows(self, actor: ReviewActor, action: str) -> bool:
        if actor.kind == KIND_SYSTEM:
            return action in self._SYSTEM_ACTIONS
        if actor.kind == KIND_USER:
            return bool(actor.is_staff and actor.is_active and actor.id is not None
                        and action in self._STAFF_ACTIONS)
        return False
