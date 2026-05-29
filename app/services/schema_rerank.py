"""
Local schema reranking helpers for chat.
"""
from __future__ import annotations

import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any, Dict, List, Literal

SchemaKind = Literal["tabla", "medida", "otro"]
DEFAULT_RERANK_TIMEOUT_SECONDS = 10

_SCHEMA_DATA_PATH = Path(__file__).resolve().with_name("schema_data.py")
_SCHEMA_DATA_CACHE: tuple[float, List[str]] | None = None


def _load_tablas_schema() -> List[str]:
    if not _SCHEMA_DATA_PATH.exists():
        return []

    try:
        current_mtime = _SCHEMA_DATA_PATH.stat().st_mtime
    except OSError:
        return []

    global _SCHEMA_DATA_CACHE
    if _SCHEMA_DATA_CACHE is not None and _SCHEMA_DATA_CACHE[0] == current_mtime:
        return list(_SCHEMA_DATA_CACHE[1])

    spec = importlib.util.spec_from_file_location("mcp_schema_data_local", _SCHEMA_DATA_PATH)
    if spec is None or spec.loader is None:
        return []

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    tablas_schema = getattr(module, "TABLAS_SCHEMA", [])
    if not tablas_schema:
        _SCHEMA_DATA_CACHE = (current_mtime, [])
        return []
    if not isinstance(tablas_schema, list):
        raise RuntimeError("TABLAS_SCHEMA must be a list of strings")

    loaded = [str(item) for item in tablas_schema if str(item).strip()]
    _SCHEMA_DATA_CACHE = (current_mtime, loaded)
    return list(loaded)


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


def buscar_tablas_y_medidas_relevantes(
    pregunta: str,
    n_tablas: int = 3,
    n_medidas: int = 5,
) -> Dict[str, List[Dict[str, Any]]]:
    tablas_schema = _load_tablas_schema()
    todas_las_tablas = [doc for doc in tablas_schema if clasificar_schema_item(doc) == "tabla"]
    todas_las_medidas = [doc for doc in tablas_schema if clasificar_schema_item(doc) == "medida"]

    tablas_relevantes = buscar_elementos_relevantes_rerank(pregunta, todas_las_tablas, top_k=n_tablas)
    medidas_relevantes = buscar_elementos_relevantes_rerank(pregunta, todas_las_medidas, top_k=n_medidas)

    return {"tablas": tablas_relevantes, "medidas": medidas_relevantes}


def build_schema_context_json(pregunta: str, n_tablas: int = 3, n_medidas: int = 5) -> str:
    resultados = buscar_tablas_y_medidas_relevantes(pregunta, n_tablas=n_tablas, n_medidas=n_medidas)
    return json.dumps(resultados, ensure_ascii=False, separators=(",", ":"), default=str)

