"""SQLAlchemy persistence of ``ReportRunArtifact`` rows.

Insert-only on purpose: there is no update or overwrite, so an artifact that was
generated stays exactly as it was. The store keeps content and metadata; it does
not know FinalReport, renderers or versions' meaning (those are the registries').
Like ``ReportRunStore`` it owns its commits and rolls back on failure.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import ReportRun, ReportRunArtifact

from .artifacts import ArtifactRecord, sha256_hex

LOG = logging.getLogger(__name__)

_REVISION_ATTEMPTS = 3


class ArtifactStoreError(Exception):
    """The artifact could not be stored (database failure)."""


class ArtifactStore:
    def add(self, report_run_id: str, artifact_type: str, *, content: str | bytes, content_type: str,
            schema_version: str | None = None, renderer_version: str | None = None,
            metadata: dict[str, Any] | None = None) -> ArtifactRecord:
        """Append a new artifact generation and return it. Never touches existing rows."""
        is_text = isinstance(content, str)
        data = content.encode("utf-8") if is_text else content
        digest = sha256_hex(data)
        for attempt in range(1, _REVISION_ATTEMPTS + 1):
            try:
                revision = (db.session.execute(
                    select(func.max(ReportRunArtifact.revision)).where(
                        ReportRunArtifact.report_run_id == report_run_id,
                        ReportRunArtifact.artifact_type == artifact_type)).scalar_one() or 0) + 1
                row = ReportRunArtifact(
                    report_run_id=report_run_id, artifact_type=artifact_type, revision=revision,
                    schema_version=schema_version, renderer_version=renderer_version,
                    content_type=content_type,
                    content_text=content if is_text else None, content_bytes=None if is_text else content,
                    sha256=digest, size_bytes=len(data), metadata_json=dict(metadata or {}) or None)
                db.session.add(row)
                db.session.commit()
                return self._to_record(row)
            except IntegrityError as exc:
                db.session.rollback()
                # Two writers picked the same revision (or the run is gone): retry, then give up.
                if attempt == _REVISION_ATTEMPTS:
                    raise ArtifactStoreError("Could not allocate an artifact revision") from exc
            except Exception as exc:
                db.session.rollback()
                raise ArtifactStoreError("Could not persist the artifact") from exc
        raise ArtifactStoreError("Could not persist the artifact")  # pragma: no cover

    def get_latest(self, report_run_id: str, artifact_type: str, *, schema_version: str | None = None,
                   renderer_version: str | None = None) -> ArtifactRecord | None:
        """Highest revision of a type, optionally narrowed to a schema/renderer version."""
        query = select(ReportRunArtifact).where(
            ReportRunArtifact.report_run_id == report_run_id, ReportRunArtifact.artifact_type == artifact_type)
        if schema_version is not None:
            query = query.where(ReportRunArtifact.schema_version == schema_version)
        if renderer_version is not None:
            query = query.where(ReportRunArtifact.renderer_version == renderer_version)
        row = db.session.execute(query.order_by(ReportRunArtifact.revision.desc()).limit(1)).scalar_one_or_none()
        return None if row is None else self._to_record(row)

    def get_revision(self, report_run_id: str, artifact_type: str, revision: int) -> ArtifactRecord | None:
        row = db.session.execute(select(ReportRunArtifact).where(
            ReportRunArtifact.report_run_id == report_run_id, ReportRunArtifact.artifact_type == artifact_type,
            ReportRunArtifact.revision == revision)).scalar_one_or_none()
        return None if row is None else self._to_record(row)

    def list(self, report_run_id: str, artifact_type: str | None = None) -> list[ArtifactRecord]:
        query = select(ReportRunArtifact).where(ReportRunArtifact.report_run_id == report_run_id)
        if artifact_type is not None:
            query = query.where(ReportRunArtifact.artifact_type == artifact_type)
        rows = db.session.execute(query.order_by(ReportRunArtifact.artifact_type, ReportRunArtifact.revision)).scalars()
        return [self._to_record(row) for row in rows]

    def legacy_final_report_json(self, report_run_id: str) -> dict | None:
        """``result_json["final_report"]`` of runs created before artifacts existed (read-only fallback)."""
        result = db.session.execute(
            select(ReportRun.result_json).where(ReportRun.id == report_run_id)).scalar_one_or_none()
        value = result.get("final_report") if isinstance(result, dict) else None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _to_record(row: ReportRunArtifact) -> ArtifactRecord:
        return ArtifactRecord(
            id=row.id, report_run_id=row.report_run_id, artifact_type=row.artifact_type, revision=row.revision,
            content_type=row.content_type, sha256=row.sha256, size_bytes=row.size_bytes,
            schema_version=row.schema_version, renderer_version=row.renderer_version,
            text=row.content_text, data=bytes(row.content_bytes) if row.content_bytes is not None else None,
            metadata=dict(row.metadata_json or {}), created_at=row.created_at)
