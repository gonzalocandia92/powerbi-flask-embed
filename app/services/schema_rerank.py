"""
Local schema reranking helpers for chat.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Any, Dict, List, Literal

SchemaKind = Literal["tabla", "medida", "otro"]
DEFAULT_RERANK_TIMEOUT_SECONDS = 10
DEFAULT_RERANK_MODEL = "rerank-2.5"


@dataclass(frozen=True)
class RankedDocument:
    document: str
    score: float
    index: int


@dataclass(frozen=True)
class RerankUsage:
    total_tokens: int = 0
    estimated: bool = False
    query_tokens: int = 0
    document_tokens: int = 0
    document_count: int = 0


def _get_voyage_client():
    api_key = os.getenv("VOYAGE_API_KEY")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY is required")

    try:
        import voyageai
    except ImportError as exc:  # pragma: no cover - dependency missing in test env
        raise RuntimeError("The 'voyageai' package is required to use schema reranking.") from exc

    return voyageai.Client()


def _get_rerank_timeout_seconds() -> int:
    raw_value = os.getenv("VOYAGE_RERANK_TIMEOUT_SECONDS", str(DEFAULT_RERANK_TIMEOUT_SECONDS))
    try:
        return max(1, int(raw_value))
    except Exception:
        return DEFAULT_RERANK_TIMEOUT_SECONDS


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 5)


def estimate_rerank_processed_tokens(query: str, documents: List[str]) -> RerankUsage:
    query_tokens = _estimate_tokens(query)
    document_tokens = sum(_estimate_tokens(document) for document in documents)
    document_count = len(documents)
    return RerankUsage(
        total_tokens=(query_tokens * document_count) + document_tokens,
        estimated=True,
        query_tokens=query_tokens,
        document_tokens=document_tokens,
        document_count=document_count,
    )


def clasificar_schema_item(texto: str) -> SchemaKind:
    if texto.startswith("Tabla:"):
        return "tabla"
    if texto.startswith("Medida:"):
        return "medida"
    return "otro"


def buscar_elementos_relevantes_rerank(
    pregunta: str,
    documentos_candidatos: List[str],
    top_k: int,
) -> List[Dict[str, Any]]:
    if not pregunta.strip() or not documentos_candidatos:
        return []

    cliente = _get_voyage_client()

    def _do_rerank():
        return cliente.rerank(
            query=pregunta,
            documents=documentos_candidatos,
            model=DEFAULT_RERANK_MODEL,
            top_k=top_k,
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_do_rerank)
            resultado_rerank = future.result(timeout=_get_rerank_timeout_seconds())
    except FuturesTimeoutError:
        return []
    except Exception:
        return []

    elementos_ordenados: List[Dict[str, Any]] = []
    for r in resultado_rerank.results:
        elementos_ordenados.append(
            {
                "documento": r.document,
                "score": r.relevance_score,
                "indice_original": r.index,
            }
        )
    return elementos_ordenados


def rerank_documents(
    *,
    query: str,
    documents: List[str],
    model: str = DEFAULT_RERANK_MODEL,
    top_k: int | None = None,
) -> List[RankedDocument]:
    """Generic Voyage rerank helper used by schema and skill routing."""
    ranked, _usage = rerank_documents_with_usage(
        query=query,
        documents=documents,
        model=model,
        top_k=top_k,
    )
    return ranked


def rerank_documents_with_usage(
    *,
    query: str,
    documents: List[str],
    model: str = DEFAULT_RERANK_MODEL,
    top_k: int | None = None,
) -> tuple[List[RankedDocument], RerankUsage]:
    """Rerank documents and return Voyage token accounting metadata."""
    if not query.strip() or not documents:
        return [], RerankUsage()

    cliente = _get_voyage_client()
    resolved_top_k = top_k or len(documents)

    def _do_rerank():
        return cliente.rerank(
            query=query,
            documents=documents,
            model=model,
            top_k=resolved_top_k,
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_do_rerank)
            resultado_rerank = future.result(timeout=_get_rerank_timeout_seconds())
    except FuturesTimeoutError:
        return [], RerankUsage()
    except Exception:
        return [], RerankUsage()

    total_tokens = int(getattr(resultado_rerank, "total_tokens", None) or 0)
    usage = (
        RerankUsage(
            total_tokens=total_tokens,
            estimated=False,
            document_count=len(documents),
        )
        if total_tokens > 0
        else estimate_rerank_processed_tokens(query, documents)
    )

    ranked: List[RankedDocument] = []
    for result in resultado_rerank.results:
        ranked.append(
            RankedDocument(
                document=result.document,
                score=float(result.relevance_score),
                index=int(result.index),
            )
        )
    return ranked, usage


def build_schema_items_from_live_schema(mcp_schema_json: str) -> List[str]:
    """Convert get_semantic_model_schema output to reranker-compatible strings."""
    try:
        schema = json.loads(mcp_schema_json)
    except Exception:
        return []

    items: List[str] = []

    for table_name, columns in schema.get("Tables", {}).items():
        col_list = ", ".join(str(c) for c in columns[:25])
        items.append(f"Tabla: {table_name}. Columnas: {col_list}")

    for measure_str in schema.get("Measures", []):
        items.append(f"Medida: {measure_str}")

    return items


def buscar_tablas_y_medidas_relevantes(
    pregunta: str,
    esquema_dinamico: List[str],
    n_tablas: int = 3,
    n_medidas: int = 5,
) -> Dict[str, List[Dict[str, Any]]]:
    todas_las_tablas = [doc for doc in esquema_dinamico if clasificar_schema_item(doc) == "tabla"]
    todas_las_medidas = [doc for doc in esquema_dinamico if clasificar_schema_item(doc) == "medida"]

    tablas_relevantes = buscar_elementos_relevantes_rerank(pregunta, todas_las_tablas, top_k=n_tablas)
    medidas_relevantes = buscar_elementos_relevantes_rerank(pregunta, todas_las_medidas, top_k=n_medidas)

    return {"tablas": tablas_relevantes, "medidas": medidas_relevantes}


def build_schema_context_json(
    pregunta: str,
    esquema_dinamico: List[str],
    n_tablas: int = 3,
    n_medidas: int = 5,
) -> str:
    resultados = buscar_tablas_y_medidas_relevantes(
        pregunta,
        esquema_dinamico,
        n_tablas=n_tablas,
        n_medidas=n_medidas,
    )
    return json.dumps(resultados, ensure_ascii=False, separators=(",", ":"), default=str)
