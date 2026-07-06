"""
Local schema reranking helpers for chat.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Dict, List, Literal

SchemaKind = Literal["tabla", "medida", "otro"]
DEFAULT_RERANK_TIMEOUT_SECONDS = 10


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
            model="rerank-2.5-lite",
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
