"""Vector indexing and retrieval helpers for analytics skills."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from flask import current_app

from app import db
from app.models import AnalyticsSkill, Report
from app.services import ai_billing
from app.services.observability import hash_identifier, start_observation

LOG = logging.getLogger(__name__)
VOYAGE_SKILL_MODEL = "voyage-4"
_REINDEX_ALL_LOCK = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_voyage_client():
    api_key = os.getenv("VOYAGE_API_KEY")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY is required")

    try:
        import voyageai
    except ImportError as exc:  # pragma: no cover - dependency missing in some environments
        raise RuntimeError("The 'voyageai' package is required for skill embeddings.") from exc

    return voyageai.Client(api_key=api_key)


def _estimate_voyage_tokens(texts: Iterable[str]) -> int:
    return max(1, sum(len(text or "") for text in texts) // 5)


def _metadata_list(metadata: Dict[str, Any], key: str) -> List[str]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _json_list(payload: Dict[str, Any], key: str) -> List[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _routing_document_hash(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def build_skill_routing_document(skill: AnalyticsSkill) -> str:
    """Build the short document embedded for routing a skill."""
    metadata = skill.metadata_json if isinstance(skill.metadata_json, dict) else {}
    parts = [
        f"Skill key: {skill.skill_key}",
        f"Dominio: {skill.domain_key}",
        f"Titulo: {skill.title}",
        f"Prioridad: {skill.priority or 'normal'}",
        f"Descripcion: {skill.description or ''}",
        f"Routing: {skill.routing_text}",
    ]
    routing = skill.routing_json if isinstance(skill.routing_json, dict) else {}
    canonical_measures = _metadata_list(metadata, "canonical_measures")
    allowed_dimensions = _metadata_list(metadata, "allowed_dimensions")
    preferred_tables = _metadata_list(metadata, "preferred_tables")
    trigger_terms = _json_list(routing, "trigger_terms")
    example_questions = _json_list(routing, "example_questions")
    intents = _json_list(routing, "intents")
    if canonical_measures:
        parts.append("Metricas canonicas: " + ", ".join(canonical_measures))
    if allowed_dimensions:
        parts.append("Dimensiones compatibles: " + ", ".join(allowed_dimensions))
    if preferred_tables:
        parts.append("Tablas preferidas: " + ", ".join(preferred_tables))
    if trigger_terms:
        parts.append("Disparadores: " + ", ".join(trigger_terms))
    if intents:
        parts.append("Intenciones: " + ", ".join(intents))
    if example_questions:
        parts.append("Preguntas ejemplo: " + " | ".join(example_questions))
    return "\n".join(part.strip() for part in parts if part and part.strip())


def embed_skill(skill: AnalyticsSkill, *, force: bool = False, report: Optional[Report] = None) -> bool:
    """Embed one skill if its routing document changed."""
    document = build_skill_routing_document(skill)
    document_hash = _routing_document_hash(document)
    if not force and skill.embedding is not None and skill.routing_document_hash == document_hash:
        return False

    client = _get_voyage_client()
    with start_observation(
        name="skill-document-embedding",
        as_type="embedding",
        input=[document],
    ) as observation:
        if observation is not None:
            observation.update(
                model=VOYAGE_SKILL_MODEL,
                metadata={
                    "provider": "voyageai",
                    "inputtype": "document",
                    "skillkey": skill.skill_key,
                    "scope": skill.scope,
                },
            )
        try:
            response = client.embed([document], model=VOYAGE_SKILL_MODEL, input_type="document")
        except Exception:
            if report is not None:
                ai_billing.record_ai_usage_event(
                    report=report,
                    provider="voyageai",
                    model=VOYAGE_SKILL_MODEL,
                    event_type="embedding",
                    source_type="skill_embedding_indexing",
                    trigger_type="admin_action",
                    operation_name="skill-document-embedding",
                    status="error",
                    input_tokens=_estimate_voyage_tokens([document]),
                    output_tokens=0,
                    metadata_json={
                        "skill_key": skill.skill_key,
                        "scope": skill.scope,
                        "estimated_usage": True,
                        "error_type": "voyage_provider_error",
                    },
                )
            raise

        embedding = list(response.embeddings[0])
        total_tokens = int(getattr(response, "total_tokens", None) or 0)
        skill.embedding = embedding
        skill.embedding_model = VOYAGE_SKILL_MODEL
        skill.embedded_at = _utcnow()
        skill.routing_document_hash = document_hash
        if observation is not None:
            observation.update(
                output={
                    "embedding_dimensions": len(embedding),
                    "vector_count": 1,
                },
                usage_details={"input": total_tokens} if total_tokens else None,
            )
        if report is not None:
            ai_billing.record_ai_usage_event(
                report=report,
                provider="voyageai",
                model=VOYAGE_SKILL_MODEL,
                event_type="embedding",
                source_type="skill_embedding_indexing",
                trigger_type="admin_action",
                operation_name="skill-document-embedding",
                status="success",
                input_tokens=total_tokens,
                output_tokens=0,
                total_tokens=total_tokens,
                metadata_json={"skill_key": skill.skill_key, "scope": skill.scope},
            )
        return True


def trigger_skill_embedding_update(skill_id: int) -> None:
    """Launch a best-effort background embedding update for one skill."""
    app = current_app._get_current_object()
    thread_name = f"skill-embedding-{skill_id}"

    def _runner():
        with app.app_context():
            try:
                skill = db.session.get(AnalyticsSkill, skill_id)
                if skill is None:
                    LOG.warning("[SkillVector] Skill not found for embedding: %s", skill_id)
                    return
                report = skill.report if skill.report_id_fk else None
                embed_skill(skill, report=report)
                db.session.commit()
            except Exception:
                db.session.rollback()
                LOG.exception("[SkillVector] Failed to update embedding for skill_id=%s", skill_id)

    threading.Thread(target=_runner, name=thread_name, daemon=True).start()


def reindex_active_skills(*, force: bool = False, report_id: Optional[int] = None) -> Dict[str, int]:
    """Reindex active skills manually from a Flask shell or admin script."""
    query = AnalyticsSkill.query.filter(AnalyticsSkill.is_active.is_(True))
    if report_id is not None:
        query = query.filter(AnalyticsSkill.report_id_fk == report_id)
    stats = {"seen": 0, "updated": 0, "failed": 0}
    for skill in query.order_by(AnalyticsSkill.id).all():
        stats["seen"] += 1
        try:
            report = skill.report if skill.report_id_fk else None
            if embed_skill(skill, force=force, report=report):
                stats["updated"] += 1
            db.session.commit()
        except Exception:
            db.session.rollback()
            stats["failed"] += 1
            LOG.exception("[SkillVector] Failed to reindex skill_id=%s", skill.id)
    return stats


def trigger_all_skill_reindex_update(*, force: bool = False) -> bool:
    """Launch a background reindex for active skills if one is not already running."""
    if not _REINDEX_ALL_LOCK.acquire(blocking=False):
        return False

    app = current_app._get_current_object()
    thread_name = "skill-reindex-all"

    def _runner():
        with app.app_context():
            try:
                LOG.info("[SkillVector] Starting global skill reindex force=%s", force)
                stats = reindex_active_skills(force=force)
                LOG.info(
                    "[SkillVector] Finished global skill reindex seen=%s updated=%s failed=%s",
                    stats.get("seen", 0),
                    stats.get("updated", 0),
                    stats.get("failed", 0),
                )
            except Exception:
                db.session.rollback()
                LOG.exception("[SkillVector] Global skill reindex failed")
            finally:
                _REINDEX_ALL_LOCK.release()

    threading.Thread(target=_runner, name=thread_name, daemon=True).start()
    return True


def _scope_filter(report_id: int, empresa_id: Optional[int], dataset_id: Optional[str]):
    filters = [
        db.and_(
            AnalyticsSkill.report_id_fk.is_(None),
            AnalyticsSkill.empresa_id_fk.is_(None),
            AnalyticsSkill.dataset_id.is_(None),
        ),
        AnalyticsSkill.report_id_fk == report_id,
    ]
    if empresa_id is not None:
        filters.append(AnalyticsSkill.empresa_id_fk == empresa_id)
    if dataset_id:
        filters.append(AnalyticsSkill.dataset_id == str(dataset_id))
    return db.or_(*filters)


def search_skill_candidates(
    *,
    query_embedding: List[float],
    report_id: int,
    empresa_id: Optional[int],
    dataset_id: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """Search active skill candidates compatible with the current scope."""
    bind = db.session.get_bind()
    base_query = AnalyticsSkill.query.filter(
        AnalyticsSkill.is_active.is_(True),
        AnalyticsSkill.embedding.isnot(None),
        _scope_filter(report_id, empresa_id, dataset_id),
    )
    if bind is not None and bind.dialect.name == "sqlite":
        rows = base_query.order_by(AnalyticsSkill.id).limit(limit).all()
        return [
            {"skill": row, "cosine_distance": None, "vector_similarity": None}
            for row in rows
        ]

    distance = AnalyticsSkill.embedding.cosine_distance(query_embedding).label("cosine_distance")
    rows = (
        db.session.query(AnalyticsSkill, distance)
        .filter(
            AnalyticsSkill.is_active.is_(True),
            AnalyticsSkill.embedding.isnot(None),
            _scope_filter(report_id, empresa_id, dataset_id),
        )
        .order_by(distance)
        .limit(limit)
        .all()
    )
    candidates: List[Dict[str, Any]] = []
    for skill, cosine_distance in rows:
        distance_value = float(cosine_distance) if cosine_distance is not None else None
        candidates.append(
            {
                "skill": skill,
                "cosine_distance": distance_value,
                "vector_similarity": 1.0 - distance_value if distance_value is not None else None,
            }
        )
    return candidates


def safe_route_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Return metadata safe for traces and AI usage events."""
    return json.loads(json.dumps(metadata, ensure_ascii=False, default=str))
