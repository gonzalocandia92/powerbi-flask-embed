"""Immutable, versioned snapshot of what a ``ReportRun`` actually executes.

Pure functions over plain dicts: no SQLAlchemy, no Flask. A run stores this JSON
instead of pointing at the (mutable) definition tables, so editing a definition
tomorrow never changes the meaning of yesterday's run. Bump
``SNAPSHOT_SCHEMA_VERSION`` and add a branch in ``definition_from_snapshot`` when
the shape changes in an incompatible way.

V1.3 adds an OPTIONAL ``structure`` block (frozen prompt + compiled ``ReportStructureSpec``): purely
additive, so the version stays 1; snapshots without it are legacy and read as "no frozen structure".
The block carries its own spec ``schema_version`` and is read back through ``structure_schemas``,
so a run keeps meaning what it meant even when the spec schema moves on.

Phase 3 adds, the same way, an OPTIONAL ``period`` block (``period_resolver.ResolvedPeriod`` payload, with
its own ``version``): the analytical period frozen when a scheduled occurrence was materialised. Snapshots
without it are runs without an explicit period.
"""
from __future__ import annotations

from typing import Any

from .contracts import ReportDefinition, ReportQuestion
from .period_resolver import ResolvedPeriod
from .structure_contracts import FrozenStructure, StructureWarning
from .structure_spec import ReportStructureSpec, structure_schemas

SNAPSHOT_SCHEMA_VERSION = 1


def structure_block(*, prompt: str | None, input_hash: str | None, spec: ReportStructureSpec, source: str,
                    status: str, warnings: list[StructureWarning] | tuple[StructureWarning, ...]) -> dict[str, Any]:
    return {
        "prompt": prompt, "input_hash": input_hash, "schema_version": spec.schema_version,
        "source": source, "status": status, "spec": spec.to_json(),
        "warnings": [warning.model_dump(mode="json") for warning in warnings],
    }


def structure_from_snapshot(snapshot: dict[str, Any]) -> FrozenStructure | None:
    """The run's frozen structure, or ``None`` for runs created before V1.3 (use the default then)."""
    block = snapshot.get("structure") if isinstance(snapshot, dict) else None
    if not isinstance(block, dict):
        return None
    spec = structure_schemas.validate(block["spec"], block.get("schema_version"))
    return FrozenStructure(
        prompt=block.get("prompt"), input_hash=block.get("input_hash"), schema_version=spec.schema_version,
        source=block.get("source", "default"), status=block.get("status", "not_required"), spec=spec,
        warnings=tuple(StructureWarning.model_validate(item) for item in block.get("warnings") or ()))


def period_from_snapshot(snapshot: dict[str, Any]) -> ResolvedPeriod | None:
    """The run's frozen analytical period, or ``None`` when the snapshot has none."""
    block = snapshot.get("period") if isinstance(snapshot, dict) else None
    return ResolvedPeriod.from_payload(block) if isinstance(block, dict) else None


def with_period(snapshot: dict[str, Any], period: ResolvedPeriod) -> dict[str, Any]:
    """A copy of ``snapshot`` that also freezes ``period`` (the snapshot passed in is not modified)."""
    return {**snapshot, "period": period.to_payload()}


def snapshot_from_definition(definition: ReportDefinition, *, definition_id: int | None = None,
                             structure: dict[str, Any] | None = None,
                             period: ResolvedPeriod | None = None) -> dict[str, Any]:
    snapshot = _definition_snapshot(definition, definition_id)
    if structure is not None:
        snapshot["structure"] = structure
    if period is not None:
        snapshot["period"] = period.to_payload()
    return snapshot


def _definition_snapshot(definition: ReportDefinition, definition_id: int | None) -> dict[str, Any]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "definition_id": definition_id,
        "report_id": definition.report_id,
        "name": definition.name,
        "strategy": "coordinated" if definition.coordination_enabled else "fixed",
        "analysis_model_key": definition.analysis_model_key,
        "analysis_service_tier": definition.analysis_service_tier,
        "structure_prompt": definition.structure_prompt,
        "questions": [
            {
                "key": item.key, "title": item.title, "question": item.question,
                "order": item.order, "required_skill_keys": list(item.required_skill_keys),
            }
            for item in sorted(definition.questions, key=lambda question: question.order)
        ],
    }


def definition_from_snapshot(snapshot: dict[str, Any]) -> ReportDefinition:
    version = snapshot.get("schema_version") if isinstance(snapshot, dict) else None
    if version != 1:
        raise ValueError(f"Unsupported report run snapshot schema_version: {version!r}")
    return ReportDefinition(
        report_id=snapshot["report_id"],
        name=snapshot["name"],
        questions=[
            ReportQuestion(
                key=item["key"], title=item["title"], question=item["question"],
                order=item["order"], required_skill_keys=tuple(item.get("required_skill_keys") or ()),
            )
            for item in snapshot["questions"]
        ],
        analysis_model_key=snapshot.get("analysis_model_key"),
        analysis_service_tier=snapshot.get("analysis_service_tier"),
        coordination_enabled=snapshot.get("strategy") == "coordinated",
        structure_prompt=snapshot.get("structure_prompt"),
        # The run's frozen structure travels with the definition it was created with; ``None`` for runs
        # that predate V1.3 (the composer then uses the default structure of the questions).
        structure=structure_from_snapshot(snapshot),
        period=period_from_snapshot(snapshot),
    )
