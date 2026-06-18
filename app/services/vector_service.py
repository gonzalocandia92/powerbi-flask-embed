"""Background pipeline for persisted semantic-model embeddings."""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from flask import current_app

from app import db
from app.models import Report, SchemaEmbedding, Tenant, Workspace
from app.services import ai_billing
from app.services.observability import hash_identifier, start_observation
from app.services.powerbi_tools import get_tables_and_measures_description

LOG = logging.getLogger(__name__)
VOYAGE_MODEL = "voyage-4"
VOYAGE_EMBED_DIMENSIONS = 1024
VOYAGE_BATCH_SIZE = 1000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_voyage_client():
    api_key = os.getenv("VOYAGE_API_KEY")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY is required")

    try:
        import voyageai
    except ImportError as exc:  # pragma: no cover - dependency missing in some environments
        raise RuntimeError("The 'voyageai' package is required for schema embeddings.") from exc

    return voyageai.Client(api_key=api_key)


def _batched(items: List[Dict[str, Any]], batch_size: int) -> Iterable[List[Dict[str, Any]]]:
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]


def _normalize_chunks(raw_chunks: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    normalized = []
    for raw_chunk in raw_chunks:
        item_type = str(raw_chunk.get("item_type") or "").strip().lower()
        item_name = str(raw_chunk.get("item_name") or "").strip()
        content_text = str(raw_chunk.get("content_text") or "").strip()

        if item_type not in {"table", "measure"} or not item_name or not content_text:
            LOG.warning("[VectorPipeline] Skipping malformed schema chunk: %s", raw_chunk)
            continue

        normalized.append(
            {
                "item_type": item_type,
                "item_name": item_name,
                "content_text": content_text,
            }
        )
    return normalized


def _build_powerbi_credentials(report: Report) -> Dict[str, str]:
    workspace = report.workspace
    tenant = workspace.tenant if workspace is not None else None
    if tenant is None or not tenant.tenant_id:
        raise RuntimeError(f"Report {report.id} is missing tenant information")
    if not workspace.workspace_id:
        raise RuntimeError(f"Report {report.id} is missing workspace information")

    client = tenant.client
    client_id = client.client_id if client is not None else None
    client_secret = client.get_secret() if client is not None else None
    if not client_id or not client_secret:
        raise RuntimeError(f"Report {report.id} is missing Azure client credentials")

    usuario_pbi = report.usuario_pbi
    username = usuario_pbi.username if usuario_pbi is not None else None
    password = usuario_pbi.get_password() if usuario_pbi is not None else None
    if not username or not password:
        raise RuntimeError(f"Report {report.id} is missing Power BI user credentials")

    return {
        "TENANT_ID": str(tenant.tenant_id),
        "WORKSPACE_ID": str(workspace.workspace_id),
        "CLIENT_ID": str(client_id),
        "CLIENT_SECRET": str(client_secret),
        "USER": str(username),
        "PASS": str(password),
    }


def trigger_schema_embedding_update(report_id: int, dataset_id: str) -> None:
    """Launch the embedding refresh pipeline in a background thread."""
    app = current_app._get_current_object()
    dataset_id = str(dataset_id).strip()
    thread_name = f"schema-embedding-report-{report_id}"

    def _runner():
        with app.app_context():
            LOG.info(
                "[VectorPipeline] Thread %s started for report_id=%s dataset_id=%s",
                thread_name,
                report_id,
                dataset_id,
            )
            try:
                _run_embedding_pipeline(report_id, dataset_id)
            except Exception:
                LOG.exception(
                    "[VectorPipeline] Thread %s failed for report_id=%s dataset_id=%s",
                    thread_name,
                    report_id,
                    dataset_id,
                )
            finally:
                LOG.info(
                    "[VectorPipeline] Thread %s finished for report_id=%s dataset_id=%s",
                    thread_name,
                    report_id,
                    dataset_id,
                )

    thread = threading.Thread(target=_runner, name=thread_name, daemon=True)
    thread.start()
    LOG.info(
        "[VectorPipeline] Background update queued for report_id=%s dataset_id=%s thread=%s",
        report_id,
        dataset_id,
        thread_name,
    )


def _run_embedding_pipeline(report_id: int, dataset_id: str) -> None:
    """Rebuild the persisted embedding snapshot for a report."""
    with start_observation(
        name="persist-schema-embeddings",
        as_type="span",
        input={"report_id": report_id, "dataset_id": hash_identifier(dataset_id, prefix="dataset")},
    ) as pipeline_observation:
        if pipeline_observation is not None:
            pipeline_observation.update(
                metadata={
                    "reportid": str(report_id),
                    "datasethash": hash_identifier(dataset_id, prefix="dataset"),
                    "embeddingmodel": VOYAGE_MODEL,
                }
            )

        LOG.info(
            "[VectorPipeline] Starting embedding pipeline for report_id=%s dataset_id=%s",
            report_id,
            dataset_id,
        )
        report = (
            Report.query
            .filter_by(id=report_id)
            .options(
                db.joinedload(Report.workspace)
                .joinedload(Workspace.tenant)
                .joinedload(Tenant.client),
                db.joinedload(Report.usuario_pbi),
            )
            .first()
        )

        if report is None:
            LOG.warning("[VectorPipeline] Report not found for report_id=%s", report_id)
            return
        if not report.chatbot_enabled:
            LOG.info("[VectorPipeline] Chatbot disabled for report_id=%s. Skipping pipeline.", report_id)
            return
        try:
            ai_billing.resolve_pricing(
                provider="voyageai",
                model=VOYAGE_MODEL,
                event_type="embedding",
            )
            ai_billing.enforce_limit_for_report(report)
        except ai_billing.BillingLimitExceeded:
            LOG.warning(
                "[VectorPipeline] Billing limit reached for report_id=%s. Skipping embedding pipeline.",
                report_id,
            )
            return

        credentials = _build_powerbi_credentials(report)
        LOG.info("[VectorPipeline] Fetching semantic schema for report_id=%s dataset_id=%s", report_id, dataset_id)
        raw_chunks = get_tables_and_measures_description(dataset_id, credentials)
        normalized_chunks = _normalize_chunks(raw_chunks)
        if not normalized_chunks:
            LOG.warning(
                "[VectorPipeline] No valid schema chunks generated for report_id=%s dataset_id=%s. "
                "Keeping existing embeddings snapshot unchanged.",
                report_id,
                dataset_id,
            )
            return

        voyage_client = _get_voyage_client()
        all_rows: List[SchemaEmbedding] = []
        total_chunks = len(normalized_chunks)

        for batch_index, batch in enumerate(_batched(normalized_chunks, VOYAGE_BATCH_SIZE), start=1):
            texts = [item["content_text"] for item in batch]
            LOG.info(
                "[VectorPipeline] Embedding batch %s for report_id=%s dataset_id=%s size=%s",
                batch_index,
                report_id,
                dataset_id,
                len(texts),
            )
            with start_observation(
                name="voyage-document-embedding",
                as_type="embedding",
                input=texts,
            ) as embedding_observation:
                if embedding_observation is not None:
                    embedding_observation.update(
                        model=VOYAGE_MODEL,
                        metadata={
                            "provider": "voyageai",
                            "inputtype": "document",
                            "batchindex": str(batch_index),
                            "chunkcount": str(len(texts)),
                            "reportid": str(report_id),
                            "datasethash": hash_identifier(dataset_id, prefix="dataset"),
                        },
                    )

                response = voyage_client.embed(
                    texts=texts,
                    model=VOYAGE_MODEL,
                    input_type="document",
                )
                embeddings = list(response.embeddings)
                total_tokens = getattr(response, "total_tokens", None)
                if embedding_observation is not None:
                    update_payload = {
                        "output": {
                            "embedding_dimensions": len(list(embeddings[0])) if embeddings else 0,
                            "vector_count": len(embeddings),
                        }
                    }
                    if total_tokens is not None:
                        update_payload["usage_details"] = {"input": int(total_tokens)}
                    embedding_observation.update(**update_payload)
                ai_billing.record_ai_usage_event(
                    report=report,
                    provider="voyageai",
                    model=VOYAGE_MODEL,
                    event_type="embedding",
                    source_type="schema_indexing",
                    trigger_type="background_pipeline",
                    operation_name="voyage-document-embedding",
                    status="success",
                    input_tokens=int(total_tokens or 0),
                    output_tokens=0,
                    total_tokens=int(total_tokens or 0),
                    metadata_json={
                        "input_type": "document",
                        "batch_index": batch_index,
                        "chunk_count": len(texts),
                        "dataset_hash": hash_identifier(dataset_id, prefix="dataset"),
                    },
                )
                # Persist billing immediately after the external provider call so
                # cost accounting survives later failures writing pgvector rows.
                db.session.commit()
            if len(embeddings) != len(batch):
                raise RuntimeError(
                    f"Voyage embedding count mismatch for report_id={report_id}: "
                    f"{len(embeddings)} embeddings for {len(batch)} chunks"
                )

            now = _utcnow()
            for item, embedding in zip(batch, embeddings):
                all_rows.append(
                    SchemaEmbedding(
                        report_id_fk=report.id,
                        dataset_id=dataset_id,
                        item_type=item["item_type"],
                        item_name=item["item_name"],
                        content_text=item["content_text"],
                        embedding=list(embedding),
                        last_updated=now,
                    )
                )

        try:
            deleted = SchemaEmbedding.query.filter_by(report_id_fk=report.id).delete(synchronize_session=False)
            LOG.info(
                "[VectorPipeline] Deleted %s previous embeddings for report_id=%s",
                deleted,
                report.id,
            )
            if all_rows:
                db.session.bulk_save_objects(all_rows)
            db.session.commit()
            if pipeline_observation is not None:
                pipeline_observation.update(
                    output={
                        "stored_embeddings": total_chunks,
                        "deleted_previous_embeddings": deleted,
                    }
                )
            LOG.info(
                "[VectorPipeline] Stored %s embeddings for report_id=%s dataset_id=%s",
                total_chunks,
                report.id,
                dataset_id,
            )
        except Exception:
            db.session.rollback()
            LOG.exception(
                "[VectorPipeline] Transaction failed for report_id=%s dataset_id=%s",
                report.id,
                dataset_id,
            )
            raise
