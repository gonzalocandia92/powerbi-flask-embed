"""InteractionQueryService: the single read entrypoint for the admin screen.

    ChatInteractionProvider
              |
              v
    InteractionQueryService  <-- admin route depends on this, and only this
              ^
              |
    McpInteractionProvider

Each provider exposes ``base_query(filters)`` (a filtered, not-yet-paginated
SQLAlchemy ``Select`` in one common column shape, tagged with its own source
key) and ``get_detail(raw_id)``. This service decides which provider(s) a
given channel filter needs, unions them at the SQL level when more than one
applies, and only then orders and paginates — so pagination is always
server-side and the result set is never materialized in full, regardless of
how much history accumulates. Adding a future channel (Telegram, Slack, an
API key integration, ...) means adding one more provider module with the
same two functions and one entry in ``_PROVIDERS``; nothing about the route,
the pagination or the template changes.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, literal, select, union_all

from app import db

from . import chat_provider, mcp_provider
from .contracts import InteractionFilters, InteractionPage, InteractionRow, UNKNOWN_CHANNEL

_PROVIDERS = {
    chat_provider.SOURCE_KEY: chat_provider,
    mcp_provider.SOURCE_KEY: mcp_provider,
}


def _row_from_result_row(row) -> InteractionRow:
    return InteractionRow(
        id=f"{row.row_source}:{row.raw_id}",
        occurred_at=row.occurred_at,
        empresa_id=row.empresa_id,
        empresa_name=row.empresa_name,
        report_id=row.report_id,
        report_name=row.report_name,
        channel=row.channel or UNKNOWN_CHANNEL,
        question=row.question,
        answer=row.answer,
        total_cost_usd=row.total_cost_usd,
        model_key=row.model_key,
        latency_ms=row.latency_ms,
        had_error=row.had_error,
    )


class InteractionQueryService:
    """Source-agnostic, paginated read layer over every interaction channel."""

    def query(self, filters: InteractionFilters) -> InteractionPage:
        branches = []
        for source, provider in _PROVIDERS.items():
            branch = provider.base_query(filters)
            if branch is not None:
                branches.append(branch.add_columns(literal(source).label("row_source")))

        if not branches:
            return InteractionPage(rows=[], total=0, page=filters.page, page_size=filters.page_size)

        combined_subquery = (
            branches[0].subquery("interactions")
            if len(branches) == 1
            else union_all(*branches).subquery("interactions")
        )

        total = db.session.execute(
            select(func.count()).select_from(combined_subquery)
        ).scalar_one()

        page_query = (
            select(combined_subquery)
            .order_by(combined_subquery.c.occurred_at.desc(), combined_subquery.c.raw_id.desc())
            .limit(filters.page_size)
            .offset((filters.page - 1) * filters.page_size)
        )
        rows = [_row_from_result_row(row) for row in db.session.execute(page_query)]
        return InteractionPage(rows=rows, total=int(total), page=filters.page, page_size=filters.page_size)

    def get_detail(self, composite_id: str) -> Optional[dict]:
        if not composite_id or ":" not in composite_id:
            return None
        source, _, raw_id = composite_id.partition(":")
        provider = _PROVIDERS.get(source)
        if provider is None or not raw_id.isdigit():
            return None
        return provider.get_detail(int(raw_id))

    def list_channel_options(self) -> list[str]:
        return ["klara_chat", "whatsapp", "mcp", UNKNOWN_CHANNEL]


interaction_query_service = InteractionQueryService()
