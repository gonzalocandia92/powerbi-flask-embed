"""Compatibility facade for legacy imports.

The real agent implementation now lives in :mod:`app.services.agent_core`.
"""
from __future__ import annotations

from .agent_core import (
    AgentOrchestrator,
    PromptManager,
    RuntimeSettings,
    ToolRegistry,
    build_runtime_settings,
    calcular_tokens_turno,
    run_chat_turn,
)

__all__ = [
    "AgentOrchestrator",
    "PromptManager",
    "RuntimeSettings",
    "ToolRegistry",
    "build_runtime_settings",
    "calcular_tokens_turno",
    "run_chat_turn",
]
