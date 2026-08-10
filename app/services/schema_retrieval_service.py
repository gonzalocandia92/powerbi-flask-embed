"""Shared vector retrieval for persisted semantic-model schema embeddings."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.models import SchemaEmbedding


VOYAGE_QUERY_EMBEDDING_MODEL = "voyage-4"
DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS = 20
DEFAULT_TABLE_CONTEXT_LIMIT = 6
DEFAULT_MEASURE_CONTEXT_LIMIT = 10
MAX_TABLE_CONTEXT_LIMIT = 20
MAX_MEASURE_CONTEXT_LIMIT = 40


@dataclass(frozen=True)
class SchemaRetrievalUsage:
    input_tokens: int
    estimated: bool = False


@dataclass(frozen=True)
class RelevantSchemaItem:
    item_type: str
    name: str
    content: str


@dataclass(frozen=True)
class RelevantSchemaResult:
    tables: list[RelevantSchemaItem]
    measures: list[RelevantSchemaItem]
    missing_required_items: list[dict[str, str]] = field(default_factory=list)
    usage: SchemaRetrievalUsage = field(default_factory=lambda: SchemaRetrievalUsage(0))
    truncated: bool = False


class SchemaEmbeddingsUnavailable(RuntimeError):
    """Raised when the authorized model has no persisted schema index."""


class RelevantSchemaProviderError(RuntimeError):
    """Raised when the query embedding provider cannot complete retrieval."""

    def __init__(self, message: str, *, usage: SchemaRetrievalUsage):
        super().__init__(message)
        self.usage = usage


def normalize_schema_item_name(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    if text.startswith("[") and text.endswith("]") and len(text) > 2:
        text = text[1:-1].strip()
    return text.casefold()


def _get_voyage_client():
    api_key = os.getenv("VOYAGE_API_KEY")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY is required")
    try:
        import voyageai
    except ImportError as exc:  # pragma: no cover - dependency missing in test env
        raise RuntimeError(
            "The 'voyageai' package is required to retrieve schema context."
        ) from exc
    return voyageai.Client(api_key=api_key)


def _estimate_tokens(text: str) -> int:
    return max(1, len(str(text or "")) // 5)


def _normalized_names(values: Optional[Iterable[str]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        display = " ".join(str(value or "").strip().split())
        normalized = normalize_schema_item_name(display)
        if display and normalized not in seen:
            seen.add(normalized)
            result.append(display)
    return result


def _normalized_required_items(
    values: Optional[Iterable[dict[str, Any]]],
) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for raw in values or []:
        if not isinstance(raw, dict):
            continue
        item_type = str(raw.get("item_type") or "").strip().lower()
        name = " ".join(str(raw.get("item_name") or "").strip().split())
        key = (item_type, normalize_schema_item_name(name))
        if item_type in {"table", "measure"} and name and key not in seen:
            seen.add(key)
            result.append({"item_type": item_type, "item_name": name})
    return result


def _base_query(dataset_id: str, report_id: Optional[int], item_type: str):
    query = SchemaEmbedding.query.filter(
        SchemaEmbedding.dataset_id == dataset_id,
        SchemaEmbedding.item_type == item_type,
    )
    if report_id is not None:
        query = query.filter(SchemaEmbedding.report_id_fk == report_id)
    return query


def _nearest_rows(
    *,
    dataset_id: str,
    report_id: Optional[int],
    item_type: str,
    query_vector: list[float],
    limit: int,
) -> list[SchemaEmbedding]:
    distance = SchemaEmbedding.embedding.cosine_distance(query_vector)
    return (
        _base_query(dataset_id, report_id, item_type)
        .order_by(
            distance,
            SchemaEmbedding.item_name.asc(),
            SchemaEmbedding.id.desc(),
        )
        .limit(limit)
        .all()
    )


def _dedupe_rows(rows: Iterable[SchemaEmbedding]) -> list[SchemaEmbedding]:
    seen: set[tuple[str, str]] = set()
    result: list[SchemaEmbedding] = []
    for row in rows:
        key = (str(row.item_type), normalize_schema_item_name(row.item_name))
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def _rows_for_names(
    rows: Iterable[SchemaEmbedding], names: Iterable[str]
) -> tuple[list[SchemaEmbedding], list[str]]:
    by_name: dict[str, SchemaEmbedding] = {}
    for row in rows:
        by_name.setdefault(normalize_schema_item_name(row.item_name), row)

    matched: list[SchemaEmbedding] = []
    missing: list[str] = []
    for name in names:
        row = by_name.get(normalize_schema_item_name(name))
        if row is None:
            missing.append(name)
        else:
            matched.append(row)
    return matched, missing


def _effective_limit(
    configured: Optional[int], default: int, hard_max: int, required: int
) -> int:
    try:
        resolved = int(configured or default)
    except (TypeError, ValueError):
        resolved = default
    if resolved <= 0:
        resolved = default
    return min(hard_max, max(1, resolved, required))


def _serialize_row(row: SchemaEmbedding) -> RelevantSchemaItem:
    return RelevantSchemaItem(
        item_type=str(row.item_type),
        name=str(row.item_name),
        content=str(row.content_text),
    )


async def retrieve_relevant_schema(
    *,
    dataset_id: str,
    question: str,
    report_id: Optional[int] = None,
    table_limit: Optional[int] = None,
    measure_limit: Optional[int] = None,
    required_schema_items: Optional[Iterable[dict[str, Any]]] = None,
    preferred_measures: Optional[Iterable[str]] = None,
    preferred_tables: Optional[Iterable[str]] = None,
    timeout_seconds: int = DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS,
) -> RelevantSchemaResult:
    """Retrieve bounded schema context with exact skill anchors before vector matches."""

    normalized_dataset_id = str(dataset_id or "").strip()
    normalized_question = str(question or "").strip()
    if not normalized_dataset_id:
        raise ValueError("dataset_id is required")
    if not normalized_question:
        raise ValueError("question is required")

    exact_order = (SchemaEmbedding.last_updated.desc(), SchemaEmbedding.id.desc())
    all_tables = (
        _base_query(normalized_dataset_id, report_id, "table")
        .order_by(*exact_order)
        .all()
    )
    all_measures = (
        _base_query(normalized_dataset_id, report_id, "measure")
        .order_by(*exact_order)
        .all()
    )
    if not all_tables and not all_measures:
        raise SchemaEmbeddingsUnavailable(
            "No hay embeddings de schema disponibles para el modelo."
        )

    estimated_usage = SchemaRetrievalUsage(
        input_tokens=_estimate_tokens(normalized_question), estimated=True
    )
    try:
        client = _get_voyage_client()
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: client.embed(
                    [normalized_question],
                    model=VOYAGE_QUERY_EMBEDDING_MODEL,
                    input_type="query",
                )
            ),
            timeout=max(1, int(timeout_seconds or DEFAULT_SCHEMA_CONTEXT_TIMEOUT_SECONDS)),
        )
        query_vector = list(response.embeddings[0])
        total_tokens = int(getattr(response, "total_tokens", None) or 0)
        usage = SchemaRetrievalUsage(
            input_tokens=total_tokens or estimated_usage.input_tokens,
            estimated=total_tokens <= 0,
        )
    except Exception as exc:
        raise RelevantSchemaProviderError(
            "No se pudo generar el embedding de consulta.", usage=estimated_usage
        ) from exc

    required_items = _normalized_required_items(required_schema_items)
    required_table_names = [
        item["item_name"] for item in required_items if item["item_type"] == "table"
    ]
    required_measure_names = [
        item["item_name"] for item in required_items if item["item_type"] == "measure"
    ]
    preferred_table_names = _normalized_names(preferred_tables)
    preferred_measure_names = _normalized_names(preferred_measures)

    resolved_table_limit = _effective_limit(
        table_limit,
        DEFAULT_TABLE_CONTEXT_LIMIT,
        MAX_TABLE_CONTEXT_LIMIT,
        len(required_table_names),
    )
    resolved_measure_limit = _effective_limit(
        measure_limit,
        DEFAULT_MEASURE_CONTEXT_LIMIT,
        MAX_MEASURE_CONTEXT_LIMIT,
        len(required_measure_names),
    )

    required_tables, _ = _rows_for_names(all_tables, required_table_names)
    required_measures, _ = _rows_for_names(
        all_measures, required_measure_names
    )
    preferred_table_rows, _ = _rows_for_names(all_tables, preferred_table_names)
    preferred_measure_rows, _ = _rows_for_names(
        all_measures, preferred_measure_names
    )

    vector_tables = _nearest_rows(
        dataset_id=normalized_dataset_id,
        report_id=report_id,
        item_type="table",
        query_vector=query_vector,
        limit=resolved_table_limit,
    )
    vector_measures = _nearest_rows(
        dataset_id=normalized_dataset_id,
        report_id=report_id,
        item_type="measure",
        query_vector=query_vector,
        limit=resolved_measure_limit,
    )

    selected_tables = _dedupe_rows(
        required_tables + preferred_table_rows + vector_tables
    )[:resolved_table_limit]
    selected_measures = _dedupe_rows(
        required_measures + preferred_measure_rows + vector_measures
    )[:resolved_measure_limit]

    returned_table_names = {
        normalize_schema_item_name(row.item_name) for row in selected_tables
    }
    returned_measure_names = {
        normalize_schema_item_name(row.item_name) for row in selected_measures
    }
    missing_required = [
        {"type": "table", "name": name}
        for name in required_table_names
        if normalize_schema_item_name(name) not in returned_table_names
    ] + [
        {"type": "measure", "name": name}
        for name in required_measure_names
        if normalize_schema_item_name(name) not in returned_measure_names
    ]
    truncated = (
        len(required_table_names) > MAX_TABLE_CONTEXT_LIMIT
        or len(required_measure_names) > MAX_MEASURE_CONTEXT_LIMIT
    )

    return RelevantSchemaResult(
        tables=[_serialize_row(row) for row in selected_tables],
        measures=[_serialize_row(row) for row in selected_measures],
        missing_required_items=missing_required,
        usage=usage,
        truncated=truncated,
    )
