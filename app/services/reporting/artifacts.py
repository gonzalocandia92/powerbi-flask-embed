"""Persistence-agnostic vocabulary of run artifacts: record shape, canonical JSON, hashing.

An artifact is an immutable output derived from a ``ReportRun`` (the structured
``FinalReport``, its HTML, later PDF/Markdown...). The store keeps bytes and
metadata without knowing how they were produced; this module has no SQLAlchemy,
Pydantic or renderer imports, so it is usable (and testable) anywhere.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

JSON_SEPARATORS = (",", ":")


def canonical_json(value: Any) -> str:
    """Stable JSON text: sorted keys, no insignificant whitespace, UTF-8 friendly.

    The same logical document always yields the same text, whatever the dict
    insertion order or database round-trip (PostgreSQL ``jsonb`` reorders keys),
    so the SHA-256 of this text is a reliable identity for a JSON artifact.
    """
    return json.dumps(value, sort_keys=True, separators=JSON_SEPARATORS, ensure_ascii=False,
                      allow_nan=False)


def sha256_hex(content: str | bytes) -> str:
    data = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ArtifactRecord:
    """Read model of one stored artifact (never an ORM object)."""

    id: int
    report_run_id: str
    artifact_type: str
    revision: int
    content_type: str
    sha256: str
    size_bytes: int
    schema_version: str | None = None
    renderer_version: str | None = None
    text: str | None = None
    data: bytes | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None

    @property
    def content(self) -> str | bytes:
        return self.text if self.text is not None else (self.data or b"")

    def is_intact(self) -> bool:
        """Whether the stored content still hashes to the recorded SHA-256."""
        return sha256_hex(self.content) == self.sha256
