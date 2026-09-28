"""Decoupled read layer for the admin "Interacciones" screen.

Public surface: ``InteractionQueryService`` (via the ``interaction_query_service``
singleton), the ``InteractionRow``/``InteractionFilters``/``InteractionPage``
contracts, and small channel-label helpers. The admin route imports only
from this package — never ``app.models.ChatSession``/``ChatMessage``/
``McpInteraction`` directly — so a future channel provider (Telegram, Slack,
an API integration, ...) can be added without touching the route or the
template.
"""
from .contracts import (
    KNOWN_CHANNELS,
    UNKNOWN_CHANNEL,
    InteractionFilters,
    InteractionPage,
    InteractionRow,
    format_channel_label,
)
from .query_service import InteractionQueryService, interaction_query_service

__all__ = [
    "KNOWN_CHANNELS",
    "UNKNOWN_CHANNEL",
    "InteractionFilters",
    "InteractionPage",
    "InteractionRow",
    "InteractionQueryService",
    "interaction_query_service",
    "format_channel_label",
]
