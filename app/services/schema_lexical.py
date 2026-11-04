"""Embedding-free schema ranking: the degraded mode used when the query embedding provider is down.

Pure functions over rows that expose ``item_name`` and ``content_text`` (no database, no Flask), so the
retrieval service can rank the schema rows it already loaded in memory.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Sequence, TypeVar

Row = TypeVar("Row")

MIN_KEYWORD_LENGTH = 3
NAME_MATCH_WEIGHT = 3
CONTENT_MATCH_WEIGHT = 1

_STOPWORDS = frozenset({
    "a", "al", "ante", "como", "con", "cual", "cuales", "cuanto", "cuantos", "cuanta", "cuantas", "de", "del",
    "desde", "donde", "el", "ella", "en", "entre", "es", "esa", "ese", "eso", "esta", "este", "esto", "hasta",
    "la", "las", "lo", "los", "mas", "me", "mi", "mis", "muy", "no", "o", "para", "per", "por", "que", "quiero",
    "se", "segun", "ser", "si", "sin", "sobre", "son", "su", "sus", "tambien", "tiene", "todo", "todos", "u",
    "un", "una", "uno", "unos", "unas", "y", "ya", "dame", "mostrame", "decime", "cual", "fue", "fueron",
    "and", "the", "of", "for", "by", "in", "on", "to", "with",
})


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _stem(token: str) -> str:
    # Crude Spanish plural folding so "ventas" matches "Venta" and "sucursales" matches "Sucursal".
    if len(token) > 5 and token.endswith("es"):
        return token[:-2]
    if len(token) > 4 and token.endswith("s"):
        return token[:-1]
    return token


def query_keywords(text: str) -> list[str]:
    """Distinct, accent-folded keyword stems of a (rewritten) question, in order of appearance."""
    seen: set[str] = set()
    result: list[str] = []
    for token in re.split(r"[^0-9a-z]+", _fold(text)):
        if len(token) < MIN_KEYWORD_LENGTH or token in _STOPWORDS:
            continue
        stem = _stem(token)
        if stem not in seen:
            seen.add(stem)
            result.append(stem)
    return result


def lexical_rank(rows: Iterable[Row], keywords: Sequence[str], *, limit: int) -> list[Row]:
    """Rows sharing at least one keyword, best first (name matches weigh more than description matches)."""
    if not keywords or limit <= 0:
        return []
    scored = []
    for index, row in enumerate(rows):
        name = _fold(getattr(row, "item_name", ""))
        content = _fold(getattr(row, "content_text", ""))
        score = sum(
            NAME_MATCH_WEIGHT if keyword in name else CONTENT_MATCH_WEIGHT if keyword in content else 0
            for keyword in keywords
        )
        if score > 0:
            scored.append((-score, index, row))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in scored[:limit]]
