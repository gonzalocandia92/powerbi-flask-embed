"""Stable, source-agnostic contracts for the admin interactions screen.

The admin UI and its route depend only on what is defined here
(``InteractionRow``, ``InteractionFilters``, ``InteractionPage``) and on
``InteractionQueryService`` — never on ``ChatSession``/``ChatMessage``/
``McpInteraction`` directly. Adding a new channel (Telegram, Slack, an API
key integration, ...) means writing one more provider that fills this same
DTO; the route, the query service's pagination/ordering and the template do
not change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Union

# The channels this screen ships with today. New channels do not require
# touching this tuple — it only drives the filter dropdown's stable options;
# any string value can be stored and displayed, including ones not listed
# here (they simply render using ``format_channel_label``'s fallback).
KNOWN_CHANNELS = ("klara_chat", "whatsapp", "mcp")
UNKNOWN_CHANNEL = "unknown"

_CHANNEL_LABELS = {
    "klara_chat": "Klara Chat",
    "whatsapp": "WhatsApp",
    "mcp": "MCP",
    UNKNOWN_CHANNEL: "Sin identificar",
}


def format_channel_label(channel: Optional[str]) -> str:
    key = channel or UNKNOWN_CHANNEL
    return _CHANNEL_LABELS.get(key, key)


@dataclass
class InteractionRow:
    """One row in the admin interactions table/detail — the common contract
    every provider (chat, MCP, and any future channel) must produce."""

    id: str
    occurred_at: datetime

    empresa_id: Optional[int]
    empresa_name: Optional[str]

    report_id: Optional[int]
    report_name: Optional[str]

    channel: str  # one of KNOWN_CHANNELS, or UNKNOWN_CHANNEL, or a future channel key

    question: str
    answer: Optional[str]

    total_cost_usd: Optional[Union[Decimal, float]]

    model_key: Optional[str] = None
    latency_ms: Optional[int] = None
    had_error: Optional[bool] = None


@dataclass
class InteractionFilters:
    """Server-side filters for listing interactions. All fields optional."""

    date_from: Optional[date] = None
    date_to: Optional[date] = None
    empresa_id: Optional[int] = None
    report_id: Optional[int] = None
    channel: Optional[str] = None  # None/'' = Todos
    search: Optional[str] = None
    page: int = 1
    page_size: int = 25

    def __post_init__(self) -> None:
        self.page = max(1, int(self.page or 1))
        self.page_size = min(max(1, int(self.page_size or 25)), 200)
        if self.search:
            self.search = self.search.strip() or None
        if self.channel:
            self.channel = self.channel.strip().lower() or None


@dataclass
class InteractionPage:
    rows: list = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 25

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, (self.total + self.page_size - 1) // self.page_size)
