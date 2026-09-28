"""Read-only projection of McpInteraction into the common InteractionRow shape.

MCP interactions never have an answer, cost, model or latency in this
backend — see ``app.models.McpInteraction`` for why — so those fields are
always None/"—" for this source. This mirrors ``chat_provider`` so
``InteractionQueryService`` can union both without knowing anything about
either physical table.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import literal, select

from app import db
from app.models import Empresa, McpInteraction, Report

from .contracts import InteractionFilters

SOURCE_KEY = "mcp"


def base_query(filters: InteractionFilters):
    if filters.channel and filters.channel not in (None, "", "mcp"):
        return None

    query = (
        select(
            McpInteraction.id.label("raw_id"),
            McpInteraction.created_at.label("occurred_at"),
            McpInteraction.empresa_id.label("empresa_id"),
            Empresa.nombre.label("empresa_name"),
            McpInteraction.report_id_fk.label("report_id"),
            Report.name.label("report_name"),
            literal("mcp").label("channel"),
            McpInteraction.question.label("question"),
            literal(None).label("answer"),
            literal(None).label("total_cost_usd"),
            literal(None).label("model_key"),
            literal(None).label("latency_ms"),
            literal(None).label("had_error"),
        )
        .select_from(McpInteraction)
        .outerjoin(Empresa, Empresa.id == McpInteraction.empresa_id)
        .outerjoin(Report, Report.id == McpInteraction.report_id_fk)
    )

    if filters.date_from is not None:
        query = query.where(McpInteraction.created_at >= filters.date_from)
    if filters.date_to is not None:
        query = query.where(McpInteraction.created_at < filters.date_to)
    if filters.empresa_id is not None:
        query = query.where(McpInteraction.empresa_id == filters.empresa_id)
    if filters.report_id is not None:
        query = query.where(McpInteraction.report_id_fk == filters.report_id)
    if filters.search:
        query = query.where(McpInteraction.question.ilike(f"%{filters.search}%"))

    return query


def get_detail(raw_id: int) -> Optional[dict]:
    interaction = db.session.get(McpInteraction, raw_id)
    if interaction is None:
        return None
    return {
        "id": f"{SOURCE_KEY}:{interaction.id}",
        "occurred_at": interaction.created_at,
        "empresa_id": interaction.empresa_id,
        "empresa_name": interaction.empresa.nombre if interaction.empresa else None,
        "report_id": interaction.report_id_fk,
        "report_name": interaction.report.name if interaction.report else None,
        "channel": "mcp",
        "question": interaction.question,
        "answer": None,
        "total_cost_usd": None,
        "model_key": None,
        "latency_ms": None,
        "had_error": None,
        "user_id": interaction.user_id,
        "user_username": interaction.user.username if interaction.user else None,
        "mcp_session_public_id": interaction.mcp_session_public_id,
        "grant_public_id": interaction.grant_public_id,
        "source_tool": interaction.source_tool,
    }
