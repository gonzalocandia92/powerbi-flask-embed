"""Semantic router for curated analytics skills."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app import db
from app.models import AnalyticsSkill
from app.services.observability import hash_identifier, observation_preview, start_observation
from app.services.skill_vector_service import VOYAGE_SKILL_MODEL, build_skill_routing_document, search_skill_candidates

LOG = logging.getLogger(__name__)


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cfg(config: Optional[Dict[str, Any]], name: str, default: Any) -> Any:
    if config and config.get(name) is not None:
        return config.get(name)
    return os.getenv(name, default)


@dataclass(frozen=True)
class SkillRouterSettings:
    enabled: bool = False
    mode: str = "shadow"
    candidate_limit: int = 8
    max_selected_skills: int = 2
    rerank_enabled: bool = False
    selector_enabled: bool = False
    selector_mode: str = "shadow"
    selector_model: str = "claude-haiku-4-5-20251001"
    selector_candidate_limit: int = 10
    selector_confidence_threshold: float = 0.70
    hard_enforcement_enabled: bool = False
    hard_score_threshold: float = 0.78
    hard_margin_threshold: float = 0.12
    soft_score_threshold: float = 0.60
    max_skill_chars: int = 8000
    timeout_seconds: int = 20


@dataclass
class RoutedSkill:
    skill_id: int
    skill_key: str
    domain_key: str
    scope: str
    priority: str
    enforcement_mode: str
    confidence_label: Optional[str]
    vector_similarity: Optional[float]
    rerank_score: Optional[float]
    metadata: Dict[str, Any]
    content: str
    routing: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteDecision:
    strategy: str
    confidence: float
    selected_skills: List[RoutedSkill] = field(default_factory=list)
    canonical_measures: List[str] = field(default_factory=list)
    required_schema_items: List[Dict[str, str]] = field(default_factory=list)
    preferred_tables: List[str] = field(default_factory=list)
    allowed_dimensions: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    is_hard_route: bool = False
    fallback_reason: Optional[str] = None
    candidate_skill_ids: List[int] = field(default_factory=list)
    candidate_skill_keys: List[str] = field(default_factory=list)
    vector_scores: List[float] = field(default_factory=list)
    selector_selected_skill_ids: List[int] = field(default_factory=list)
    selector_rejected_skill_ids: List[int] = field(default_factory=list)
    selector_confidence: Optional[float] = None
    selector_mode: Optional[str] = None
    selector_reason: Optional[str] = None
    selector_no_skill_match: Optional[bool] = None
    decision_source: Optional[str] = None
    required_companion_skill_keys: List[str] = field(default_factory=list)
    resolved_companion_skill_ids: List[int] = field(default_factory=list)
    resolved_companion_skill_keys: List[str] = field(default_factory=list)
    missing_companion_skill_keys: List[str] = field(default_factory=list)

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "confidence": round(float(self.confidence or 0.0), 4),
            "selected_skill_keys": [skill.skill_key for skill in self.selected_skills],
            "selected_domains": [skill.domain_key for skill in self.selected_skills],
            "selected_priorities": [skill.priority for skill in self.selected_skills],
            "selected_enforcement_modes": [skill.enforcement_mode for skill in self.selected_skills],
            "fallback_reason": self.fallback_reason,
            "canonical_measures": list(self.canonical_measures),
            "required_schema_items": list(self.required_schema_items),
            "is_hard_route": bool(self.is_hard_route),
            "candidate_skill_ids": list(self.candidate_skill_ids),
            "candidate_skill_keys": list(self.candidate_skill_keys),
            "vector_scores": list(self.vector_scores),
            "selector_selected_skill_ids": list(self.selector_selected_skill_ids),
            "selector_rejected_skill_ids": list(self.selector_rejected_skill_ids),
            "selector_confidence": (
                round(float(self.selector_confidence), 4)
                if self.selector_confidence is not None
                else None
            ),
            "selector_mode": self.selector_mode,
            "selector_reason": self.selector_reason,
            "selector_no_skill_match": self.selector_no_skill_match,
            "decision_source": self.decision_source,
            "required_companion_skill_keys": list(self.required_companion_skill_keys),
            "resolved_companion_skill_ids": list(self.resolved_companion_skill_ids),
            "resolved_companion_skill_keys": list(self.resolved_companion_skill_keys),
            "missing_companion_skill_keys": list(self.missing_companion_skill_keys),
        }


@dataclass
class SkillSelectorDecision:
    selected_skill_ids: List[int] = field(default_factory=list)
    rejected_skill_ids: List[int] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    no_skill_match: bool = False
    status: str = "success"
    error_type: Optional[str] = None

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "selected_skill_ids": list(self.selected_skill_ids),
            "rejected_skill_ids": list(self.rejected_skill_ids),
            "confidence": round(float(self.confidence or 0.0), 4),
            "reason": self.reason,
            "no_skill_match": bool(self.no_skill_match),
            "status": self.status,
            "error_type": self.error_type,
        }


@dataclass
class RouteValidationResult:
    warnings: List[str] = field(default_factory=list)
    canonical_measure_used: bool = False
    required_measure_missing: bool = False
    validation_skipped: bool = False

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "warnings": list(self.warnings),
            "canonical_measure_used": self.canonical_measure_used,
            "required_measure_missing": self.required_measure_missing,
            "validation_skipped": self.validation_skipped,
        }


def build_skill_router_settings(config: Optional[Dict[str, Any]] = None) -> SkillRouterSettings:
    mode = str(_cfg(config, "SKILL_ROUTER_MODE", "shadow") or "shadow").strip().lower()
    if mode not in {"shadow", "active"}:
        mode = "shadow"
    selector_mode = str(_cfg(config, "SKILL_ROUTER_SELECTOR_MODE", "shadow") or "shadow").strip().lower()
    if selector_mode not in {"shadow", "active"}:
        selector_mode = "shadow"
    return SkillRouterSettings(
        enabled=_parse_bool(_cfg(config, "SKILL_ROUTER_ENABLED", "false"), default=False),
        mode=mode,
        candidate_limit=_parse_int(_cfg(config, "SKILL_ROUTER_CANDIDATE_LIMIT", "8"), 8),
        max_selected_skills=_parse_int(_cfg(config, "SKILL_ROUTER_MAX_SELECTED_SKILLS", "2"), 2),
        rerank_enabled=_parse_bool(_cfg(config, "SKILL_ROUTER_RERANK_ENABLED", "false"), default=False),
        selector_enabled=_parse_bool(_cfg(config, "SKILL_ROUTER_SELECTOR_ENABLED", "false"), default=False),
        selector_mode=selector_mode,
        selector_model=str(
            _cfg(config, "SKILL_ROUTER_SELECTOR_MODEL", "claude-haiku-4-5-20251001")
            or "claude-haiku-4-5-20251001"
        ),
        selector_candidate_limit=_parse_int(_cfg(config, "SKILL_ROUTER_SELECTOR_CANDIDATE_LIMIT", "10"), 10),
        selector_confidence_threshold=_parse_float(
            _cfg(config, "SKILL_ROUTER_SELECTOR_CONFIDENCE_THRESHOLD", "0.70"),
            0.70,
        ),
        hard_enforcement_enabled=_parse_bool(
            _cfg(config, "SKILL_ROUTER_HARD_ENFORCEMENT_ENABLED", "false"),
            default=False,
        ),
        hard_score_threshold=_parse_float(_cfg(config, "SKILL_ROUTER_HARD_SCORE_THRESHOLD", "0.78"), 0.78),
        hard_margin_threshold=_parse_float(_cfg(config, "SKILL_ROUTER_HARD_MARGIN_THRESHOLD", "0.12"), 0.12),
        soft_score_threshold=_parse_float(_cfg(config, "SKILL_ROUTER_SOFT_SCORE_THRESHOLD", "0.60"), 0.60),
        max_skill_chars=_parse_int(_cfg(config, "SKILL_ROUTER_MAX_SKILL_CHARS", "8000"), 8000),
        timeout_seconds=_parse_int(_cfg(config, "CHAT_SCHEMA_CONTEXT_TIMEOUT_SECONDS", "20"), 20),
    )


def _get_voyage_client():
    api_key = os.getenv("VOYAGE_API_KEY")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY is required")
    try:
        import voyageai
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("The 'voyageai' package is required for skill routing.") from exc
    return voyageai.Client(api_key=api_key)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 5)


def _scope_priority(scope: str) -> int:
    return {"global": 0, "empresa": 1, "dataset": 2, "report": 3}.get(scope, 0)


def _operator_priority(priority: Optional[str]) -> int:
    return {"low": 0, "normal": 1, "high": 2}.get(str(priority or "normal").strip().lower(), 1)


def _score(candidate: Dict[str, Any]) -> float:
    rerank_score = candidate.get("rerank_score")
    if rerank_score is not None:
        return float(rerank_score)
    vector_similarity = candidate.get("vector_similarity")
    if vector_similarity is not None:
        return float(vector_similarity)
    return 0.0


def _usage_metric(usage: Any, field_name: str) -> int:
    if usage is None:
        return 0
    value = getattr(usage, field_name, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(field_name)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _anthropic_usage_metrics(usage: Any) -> Dict[str, int]:
    return {
        "input_tokens": _usage_metric(usage, "input_tokens"),
        "output_tokens": _usage_metric(usage, "output_tokens"),
        "cache_write_tokens": _usage_metric(usage, "cache_creation_input_tokens"),
        "cache_read_tokens": _usage_metric(usage, "cache_read_input_tokens"),
    }


def _anthropic_cost_details(model: str, usage_metrics: Dict[str, int]) -> Optional[Dict[str, float]]:
    try:
        from app.services import ai_billing

        pricing = ai_billing.resolve_pricing(provider="anthropic", model=model, event_type="generation")
        costs = ai_billing.calculate_cost_breakdown(
            pricing,
            input_tokens=usage_metrics["input_tokens"],
            output_tokens=usage_metrics["output_tokens"],
            cache_write_tokens=usage_metrics["cache_write_tokens"],
            cache_read_tokens=usage_metrics["cache_read_tokens"],
        )
    except Exception:
        return None

    return {
        "input": float(costs["input_cost_usd"] or 0.0),
        "output": float(costs["output_cost_usd"] or 0.0),
        "cache_creation_input_tokens": float(costs["cache_write_cost_usd"] or 0.0),
        "cache_read_input_tokens": float(costs["cache_read_cost_usd"] or 0.0),
        "total": float(costs["total_cost_usd"] or 0.0),
    }


def _unique_strings(values: List[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def validate_dax_against_route(dax_query: str, route: Optional[RouteDecision]) -> RouteValidationResult:
    """Conservative diagnostic validation for DAX generated under a route."""
    if route is None or not route.selected_skills or not route.canonical_measures:
        return RouteValidationResult(validation_skipped=True)

    normalized_dax = str(dax_query or "").casefold()
    used = False
    for measure in route.canonical_measures:
        measure_text = str(measure or "").strip()
        if not measure_text:
            continue
        bracketed = f"[{measure_text}]".casefold()
        if bracketed in normalized_dax or measure_text.casefold() in normalized_dax:
            used = True
            break

    warnings: List[str] = []
    if not used:
        warnings.append("No se detecto el uso de una medida canonica sugerida por la ruta.")
    return RouteValidationResult(
        warnings=warnings,
        canonical_measure_used=used,
        required_measure_missing=not used,
        validation_skipped=False,
    )


def _required_schema_items(metadata: Dict[str, Any]) -> List[Dict[str, str]]:
    raw_items = metadata.get("required_schema_items")
    if not isinstance(raw_items, list):
        return []
    result: List[Dict[str, str]] = []
    seen = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("item_type") or "").strip().lower()
        item_name = str(item.get("item_name") or "").strip()
        if item_type not in {"measure", "table"} or not item_name:
            continue
        key = (item_type, item_name.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append({"item_type": item_type, "item_name": item_name})
    return result


def _metadata_list(metadata: Dict[str, Any], key: str) -> List[str]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    return _unique_strings([str(item) for item in value])


def _routing_list(routing: Dict[str, Any], key: str) -> List[str]:
    value = routing.get(key)
    if not isinstance(value, list):
        return []
    return _unique_strings([str(item) for item in value])


def _dedupe_by_skill_key(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    best_by_key: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        skill = candidate["skill"]
        key = str(skill.skill_key).casefold()
        current = best_by_key.get(key)
        if current is None:
            best_by_key[key] = candidate
            continue
        current_skill = current["skill"]
        candidate_priority = _scope_priority(skill.scope)
        current_priority = _scope_priority(current_skill.scope)
        if candidate_priority > current_priority:
            best_by_key[key] = candidate
        elif candidate_priority == current_priority and _score(candidate) > _score(current):
            best_by_key[key] = candidate
    return list(best_by_key.values())


def _candidate_from_skill(skill: AnalyticsSkill) -> Dict[str, Any]:
    return {"skill": skill, "cosine_distance": None, "vector_similarity": None, "rerank_score": None}


def _routed_skill_from_candidate(candidate: Dict[str, Any]) -> RoutedSkill:
    skill: AnalyticsSkill = candidate["skill"]
    metadata = skill.metadata_json if isinstance(skill.metadata_json, dict) else {}
    routing = skill.routing_json if isinstance(skill.routing_json, dict) else {}
    return RoutedSkill(
        skill_id=int(skill.id),
        skill_key=skill.skill_key,
        domain_key=skill.domain_key,
        scope=skill.scope,
        priority=skill.priority or "normal",
        enforcement_mode=skill.enforcement_mode or "soft",
        confidence_label=skill.confidence_label,
        vector_similarity=candidate.get("vector_similarity"),
        rerank_score=candidate.get("rerank_score"),
        metadata=metadata,
        content=skill.content,
        routing=routing,
    )


def _append_candidate_context(
    decision: RouteDecision,
    candidate: Dict[str, Any],
    *,
    selected_skill_ids: set[int],
) -> bool:
    skill: AnalyticsSkill = candidate["skill"]
    skill_id = int(skill.id)
    if skill_id in selected_skill_ids:
        return False
    metadata = skill.metadata_json if isinstance(skill.metadata_json, dict) else {}
    decision.selected_skills.append(_routed_skill_from_candidate(candidate))
    decision.canonical_measures = _unique_strings(
        list(decision.canonical_measures) + _metadata_list(metadata, "canonical_measures")
    )
    decision.required_schema_items = _required_schema_items(
        {
            "required_schema_items": list(decision.required_schema_items)
            + _required_schema_items(metadata)
        }
    )
    decision.preferred_tables = _unique_strings(
        list(decision.preferred_tables) + _metadata_list(metadata, "preferred_tables")
    )
    decision.allowed_dimensions = _unique_strings(
        list(decision.allowed_dimensions) + _metadata_list(metadata, "allowed_dimensions")
    )
    decision.constraints = _unique_strings(
        list(decision.constraints) + _metadata_list(metadata, "constraints")
    )
    selected_skill_ids.add(skill_id)
    return True


def _scope_filter_conditions(report_id: int, empresa_id: Optional[int], dataset_id: Optional[str]) -> Any:
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


def _resolve_companion_candidates(
    *,
    skill_keys: List[str],
    report_id: int,
    empresa_id: Optional[int],
    dataset_id: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    if not skill_keys:
        return {}
    normalized_keys = {key.casefold(): key for key in skill_keys}
    rows = (
        AnalyticsSkill.query
        .filter(
            AnalyticsSkill.is_active.is_(True),
            db.func.lower(AnalyticsSkill.skill_key).in_(list(normalized_keys.keys())),
            _scope_filter_conditions(report_id, empresa_id, dataset_id),
        )
        .all()
    )
    best_by_key: Dict[str, Dict[str, Any]] = {}
    for skill in rows:
        key = str(skill.skill_key or "").casefold()
        current = best_by_key.get(key)
        candidate = _candidate_from_skill(skill)
        if current is None:
            best_by_key[key] = candidate
            continue
        current_skill = current["skill"]
        if _scope_priority(skill.scope) > _scope_priority(current_skill.scope):
            best_by_key[key] = candidate
    return best_by_key


def _expand_required_companions(
    decision: RouteDecision,
    *,
    report_id: int,
    empresa_id: Optional[int],
    dataset_id: Optional[str],
) -> RouteDecision:
    primary_skills = list(decision.selected_skills)
    if not primary_skills:
        return decision

    companion_keys: List[str] = []
    for routed_skill in primary_skills:
        companion_keys.extend(_routing_list(routed_skill.routing, "required_companion_skill_keys"))
    companion_keys = _unique_strings(companion_keys)
    decision.required_companion_skill_keys = companion_keys
    if not companion_keys:
        return decision

    selected_ids = {int(skill.skill_id) for skill in decision.selected_skills}
    selected_keys = {str(skill.skill_key or "").casefold() for skill in decision.selected_skills}
    selected_by_key = {
        str(skill.skill_key or "").casefold(): skill
        for skill in decision.selected_skills
    }
    companion_candidates = _resolve_companion_candidates(
        skill_keys=companion_keys,
        report_id=report_id,
        empresa_id=empresa_id,
        dataset_id=dataset_id,
    )

    missing: List[str] = []
    for companion_key in companion_keys:
        normalized_key = companion_key.casefold()
        if normalized_key in selected_keys:
            selected_skill = selected_by_key[normalized_key]
            decision.resolved_companion_skill_ids.append(int(selected_skill.skill_id))
            decision.resolved_companion_skill_keys.append(selected_skill.skill_key)
            continue
        candidate = companion_candidates.get(normalized_key)
        if candidate is None:
            missing.append(companion_key)
            continue
        if _append_candidate_context(decision, candidate, selected_skill_ids=selected_ids):
            companion_skill = candidate["skill"]
            decision.resolved_companion_skill_ids.append(int(companion_skill.id))
            decision.resolved_companion_skill_keys.append(companion_skill.skill_key)
            selected_keys.add(str(companion_skill.skill_key or "").casefold())
            selected_by_key[str(companion_skill.skill_key or "").casefold()] = decision.selected_skills[-1]
    decision.resolved_companion_skill_keys = _unique_strings(decision.resolved_companion_skill_keys)
    decision.missing_companion_skill_keys = _unique_strings(missing)
    return decision


def build_skill_selector_card(skill: AnalyticsSkill) -> Dict[str, Any]:
    """Build the minimal skill card visible to the LLM selector."""
    return {
        "skill_id": int(skill.id),
        "skill_key": skill.skill_key,
        "domain_key": skill.domain_key,
        "scope": skill.scope,
        "title": skill.title,
        "description": skill.description or "",
        "routing_text": skill.routing_text or "",
    }


def _selector_candidate_metadata(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "candidate_skill_ids": [int(candidate["skill"].id) for candidate in candidates],
        "candidate_skill_keys": [candidate["skill"].skill_key for candidate in candidates],
        "vector_scores": [
            round(float(candidate.get("vector_similarity") or 0.0), 4)
            for candidate in candidates
        ],
    }


def _apply_route_metadata(
    decision: RouteDecision,
    *,
    candidates: List[Dict[str, Any]],
    selector_decision: Optional[SkillSelectorDecision],
    selector_mode: Optional[str],
    decision_source: str,
) -> RouteDecision:
    candidate_metadata = _selector_candidate_metadata(candidates)
    decision.candidate_skill_ids = candidate_metadata["candidate_skill_ids"]
    decision.candidate_skill_keys = candidate_metadata["candidate_skill_keys"]
    decision.vector_scores = candidate_metadata["vector_scores"]
    decision.selector_mode = selector_mode
    decision.decision_source = decision_source
    if selector_decision is not None:
        decision.selector_selected_skill_ids = list(selector_decision.selected_skill_ids)
        decision.selector_rejected_skill_ids = list(selector_decision.rejected_skill_ids)
        decision.selector_confidence = selector_decision.confidence
        decision.selector_reason = selector_decision.reason
        decision.selector_no_skill_match = selector_decision.no_skill_match
    return decision


def _parse_selector_payload(payload: Any, valid_skill_ids: set[int]) -> SkillSelectorDecision:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError("Selector payload must be a JSON object")

    def _valid_ids(key: str) -> List[int]:
        values = payload.get(key)
        if not isinstance(values, list):
            return []
        result: List[int] = []
        seen = set()
        for raw_value in values:
            try:
                skill_id = int(raw_value)
            except (TypeError, ValueError):
                continue
            if skill_id not in valid_skill_ids or skill_id in seen:
                continue
            seen.add(skill_id)
            result.append(skill_id)
        return result

    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    return SkillSelectorDecision(
        selected_skill_ids=_valid_ids("selected_skill_ids"),
        rejected_skill_ids=_valid_ids("rejected_skill_ids"),
        confidence=confidence,
        reason=str(payload.get("reason") or "").strip()[:1000],
        no_skill_match=bool(payload.get("no_skill_match")),
    )


def _extract_selector_tool_input(response: Any) -> Optional[Dict[str, Any]]:
    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type is None and isinstance(block, dict):
            block_type = block.get("type")
        if block_type != "tool_use":
            continue
        name = getattr(block, "name", None)
        if name is None and isinstance(block, dict):
            name = block.get("name")
        if name != "submit_skill_selection":
            continue
        tool_input = getattr(block, "input", None)
        if tool_input is None and isinstance(block, dict):
            tool_input = block.get("input")
        if isinstance(tool_input, dict):
            return tool_input
    return None


def _extract_response_text(response: Any) -> str:
    parts: List[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()


def _selector_error_decision(error_type: str, reason: str = "") -> SkillSelectorDecision:
    return SkillSelectorDecision(
        status="error",
        error_type=error_type,
        reason=reason[:1000],
        no_skill_match=True,
    )


async def _select_skill_candidates(
    *,
    user_message: str,
    candidates: List[Dict[str, Any]],
    settings: SkillRouterSettings,
    usage_totals: Optional[Dict[str, int]] = None,
    ai_usage_events: Optional[List[Dict[str, Any]]] = None,
) -> SkillSelectorDecision:
    """Ask a small LLM selector to choose from already-authorized candidates."""
    if not candidates:
        return SkillSelectorDecision(no_skill_match=True, reason="No candidates available")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return _selector_error_decision("missing_anthropic_api_key", "ANTHROPIC_API_KEY is required")

    try:
        from anthropic import AsyncAnthropic  # type: ignore
    except ImportError:
        return _selector_error_decision("missing_anthropic_package", "The anthropic package is not available")

    cards = [build_skill_selector_card(candidate["skill"]) for candidate in candidates]
    valid_skill_ids = {int(card["skill_id"]) for card in cards}
    selector_input = {"user_message": user_message, "candidate_skills": cards}
    system_prompt = (
        "Eres un selector de skills analiticas. Tu unica tarea es elegir cuales skills del catalogo "
        "ayudan a responder la pregunta del usuario. No inventes skills ni uses IDs fuera del catalogo. "
        "Si ninguna skill corresponde, marca no_skill_match=true y deja selected_skill_ids vacio."
    )
    tool_schema = {
        "name": "submit_skill_selection",
        "description": "Devuelve la seleccion final de skills para la pregunta.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selected_skill_ids": {"type": "array", "items": {"type": "integer"}},
                "rejected_skill_ids": {"type": "array", "items": {"type": "integer"}},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
                "no_skill_match": {"type": "boolean"},
            },
            "required": [
                "selected_skill_ids",
                "rejected_skill_ids",
                "confidence",
                "reason",
                "no_skill_match",
            ],
        },
    }

    with start_observation(
        name="select-skill-candidates",
        as_type="generation",
        input=selector_input,
        model=settings.selector_model,
    ) as observation:
        try:
            async with AsyncAnthropic(api_key=api_key) as client:
                response = await asyncio.wait_for(
                    client.messages.create(
                        model=settings.selector_model,
                        max_tokens=500,
                        temperature=0.0,
                        system=system_prompt,
                        messages=[
                            {
                                "role": "user",
                                "content": json.dumps(selector_input, ensure_ascii=False),
                            }
                        ],
                        tools=[tool_schema],
                        tool_choice={"type": "tool", "name": "submit_skill_selection"},
                    ),
                    timeout=max(1, settings.timeout_seconds),
                )
            payload = _extract_selector_tool_input(response)
            if payload is None:
                payload = json.loads(_extract_response_text(response))
            decision = _parse_selector_payload(payload, valid_skill_ids)
            usage_metrics = _anthropic_usage_metrics(getattr(response, "usage", None))
            if usage_totals is not None:
                usage_totals["input_tokens"] = int(usage_totals.get("input_tokens", 0)) + usage_metrics["input_tokens"]
                usage_totals["output_tokens"] = int(usage_totals.get("output_tokens", 0)) + usage_metrics["output_tokens"]
            if ai_usage_events is not None:
                ai_usage_events.append(
                    {
                        "provider": "anthropic",
                        "model": settings.selector_model,
                        "event_type": "generation",
                        "source_type": "skill_router_selector",
                        "trigger_type": "user_request",
                        "operation_name": "select-skill-candidates",
                        "status": "success",
                        "input_tokens": usage_metrics["input_tokens"],
                        "output_tokens": usage_metrics["output_tokens"],
                        "total_tokens": usage_metrics["input_tokens"] + usage_metrics["output_tokens"],
                        "cache_write_tokens": usage_metrics["cache_write_tokens"],
                        "cache_read_tokens": usage_metrics["cache_read_tokens"],
                        "metadata_json": {
                            "selector_mode": settings.selector_mode,
                            "candidate_count": len(candidates),
                            **decision.to_metadata(),
                        },
                    }
                )
            if observation is not None:
                update_payload = {
                    "output": decision.to_metadata(),
                    "usage_details": {
                        "input": usage_metrics["input_tokens"],
                        "output": usage_metrics["output_tokens"],
                    },
                }
                cost_details = _anthropic_cost_details(settings.selector_model, usage_metrics)
                if cost_details is not None:
                    update_payload["cost_details"] = cost_details
                observation.update(**update_payload)
            return decision
        except Exception as exc:
            LOG.exception("[SkillRouter] LLM skill selector failed")
            estimated_tokens = _estimate_tokens(system_prompt + json.dumps(selector_input, ensure_ascii=False))
            if usage_totals is not None:
                usage_totals["input_tokens"] = int(usage_totals.get("input_tokens", 0)) + estimated_tokens
            if ai_usage_events is not None:
                ai_usage_events.append(
                    {
                        "provider": "anthropic",
                        "model": settings.selector_model,
                        "event_type": "generation",
                        "source_type": "skill_router_selector",
                        "trigger_type": "user_request",
                        "operation_name": "select-skill-candidates",
                        "status": "error",
                        "input_tokens": estimated_tokens,
                        "output_tokens": 0,
                        "total_tokens": estimated_tokens,
                        "metadata_json": {
                            "selector_mode": settings.selector_mode,
                            "candidate_count": len(candidates),
                            "estimated_usage": True,
                            "error_type": "anthropic_provider_error",
                        },
                    }
                )
            error_decision = _selector_error_decision("anthropic_provider_error", repr(exc))
            if observation is not None:
                observation.update(output=error_decision.to_metadata())
            return error_decision


def _append_rerank_usage_event(
    *,
    usage_totals: Optional[Dict[str, int]],
    ai_usage_events: Optional[List[Dict[str, Any]]],
    model: str,
    usage: Any,
    candidate_count: int,
    router_mode: str,
) -> None:
    total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
    if total_tokens <= 0:
        return
    if usage_totals is not None:
        usage_totals["input_tokens"] = int(usage_totals.get("input_tokens", 0)) + total_tokens
    if ai_usage_events is not None:
        ai_usage_events.append(
            {
                "provider": "voyageai",
                "model": model,
                "event_type": "rerank",
                "source_type": "skill_router_rerank",
                "trigger_type": "user_request",
                "operation_name": "rerank-skill-candidates",
                "status": "success",
                "input_tokens": total_tokens,
                "output_tokens": 0,
                "total_tokens": total_tokens,
                "metadata_json": {
                    "router_mode": router_mode,
                    "candidate_count": candidate_count,
                    "document_count": int(getattr(usage, "document_count", 0) or candidate_count),
                    "estimated_usage": bool(getattr(usage, "estimated", False)),
                    "query_tokens": int(getattr(usage, "query_tokens", 0) or 0),
                    "document_tokens": int(getattr(usage, "document_tokens", 0) or 0),
                    "pricing_formula": "(query_tokens * document_count) + document_tokens",
                },
            }
        )


def _apply_rerank(
    user_message: str,
    candidates: List[Dict[str, Any]],
    *,
    usage_totals: Optional[Dict[str, int]] = None,
    ai_usage_events: Optional[List[Dict[str, Any]]] = None,
    router_mode: str = "shadow",
) -> Dict[str, Any]:
    from app.services.schema_rerank import DEFAULT_RERANK_MODEL, rerank_documents_with_usage

    documents = [build_skill_routing_document(candidate["skill"]) for candidate in candidates]
    ranked, usage = rerank_documents_with_usage(
        query=user_message,
        documents=documents,
        model=DEFAULT_RERANK_MODEL,
        top_k=len(documents),
    )
    if not ranked:
        return {"ok": False, "usage": usage, "model": DEFAULT_RERANK_MODEL}
    _append_rerank_usage_event(
        usage_totals=usage_totals,
        ai_usage_events=ai_usage_events,
        model=DEFAULT_RERANK_MODEL,
        usage=usage,
        candidate_count=len(candidates),
        router_mode=router_mode,
    )
    score_by_index = {item.index: item.score for item in ranked}
    for index, candidate in enumerate(candidates):
        if index in score_by_index:
            candidate["rerank_score"] = score_by_index[index]
    return {"ok": True, "usage": usage, "model": DEFAULT_RERANK_MODEL}


def _build_decision(
    *,
    candidates: List[Dict[str, Any]],
    settings: SkillRouterSettings,
    fallback_reason: Optional[str] = None,
) -> RouteDecision:
    if fallback_reason:
        return RouteDecision(strategy="fallback", confidence=0.0, fallback_reason=fallback_reason)

    sorted_candidates = sorted(
        candidates,
        key=lambda item: (
            _score(item),
            _scope_priority(item["skill"].scope),
            _operator_priority(item["skill"].priority),
        ),
        reverse=True,
    )
    if not sorted_candidates:
        return RouteDecision(strategy="no_skill_match", confidence=0.0, fallback_reason="no_candidates")

    top_score = _score(sorted_candidates[0])
    second_score = _score(sorted_candidates[1]) if len(sorted_candidates) > 1 else 0.0
    margin = top_score - second_score
    if top_score < settings.soft_score_threshold:
        return RouteDecision(
            strategy="no_skill_match",
            confidence=top_score,
            fallback_reason="score_below_soft_threshold",
        )

    selected_candidates = sorted_candidates[: settings.max_selected_skills]
    selected_skills: List[RoutedSkill] = []
    canonical_measures: List[str] = []
    required_items: List[Dict[str, str]] = []
    preferred_tables: List[str] = []
    allowed_dimensions: List[str] = []
    constraints: List[str] = []
    metadata_complete = True
    for candidate in selected_candidates:
        skill: AnalyticsSkill = candidate["skill"]
        metadata = skill.metadata_json if isinstance(skill.metadata_json, dict) else {}
        metadata_complete = metadata_complete and bool(metadata)
        canonical_measures.extend(_metadata_list(metadata, "canonical_measures"))
        required_items.extend(_required_schema_items(metadata))
        preferred_tables.extend(_metadata_list(metadata, "preferred_tables"))
        allowed_dimensions.extend(_metadata_list(metadata, "allowed_dimensions"))
        constraints.extend(_metadata_list(metadata, "constraints"))
        selected_skills.append(_routed_skill_from_candidate(candidate))

    first_metadata = selected_skills[0].metadata if selected_skills else {}
    enforcement_mode = str(
        selected_skills[0].enforcement_mode
        if selected_skills
        else first_metadata.get("enforcement_mode") or "soft"
    ).strip().lower()
    is_hard = (
        settings.hard_enforcement_enabled
        and top_score >= settings.hard_score_threshold
        and margin >= settings.hard_margin_threshold
        and metadata_complete
        and enforcement_mode in {"hard", "hard_candidate"}
    )
    return RouteDecision(
        strategy="hard_route" if is_hard else "soft_route",
        confidence=top_score,
        selected_skills=selected_skills,
        canonical_measures=_unique_strings(canonical_measures),
        required_schema_items=_required_schema_items({"required_schema_items": required_items}),
        preferred_tables=_unique_strings(preferred_tables),
        allowed_dimensions=_unique_strings(allowed_dimensions),
        constraints=_unique_strings(constraints),
        is_hard_route=is_hard,
    )


def _build_selector_decision(
    *,
    candidates: List[Dict[str, Any]],
    selector_decision: SkillSelectorDecision,
    settings: SkillRouterSettings,
) -> RouteDecision:
    selected_ids = set(selector_decision.selected_skill_ids[: settings.max_selected_skills])
    selected_candidates = [candidate for candidate in candidates if int(candidate["skill"].id) in selected_ids]
    if not selected_candidates:
        return RouteDecision(
            strategy="no_skill_match",
            confidence=selector_decision.confidence,
            fallback_reason="selector_returned_no_valid_skills",
        )

    selected_skills: List[RoutedSkill] = []
    canonical_measures: List[str] = []
    required_items: List[Dict[str, str]] = []
    preferred_tables: List[str] = []
    allowed_dimensions: List[str] = []
    constraints: List[str] = []
    metadata_complete = True
    for candidate in selected_candidates:
        skill: AnalyticsSkill = candidate["skill"]
        metadata = skill.metadata_json if isinstance(skill.metadata_json, dict) else {}
        metadata_complete = metadata_complete and bool(metadata)
        canonical_measures.extend(_metadata_list(metadata, "canonical_measures"))
        required_items.extend(_required_schema_items(metadata))
        preferred_tables.extend(_metadata_list(metadata, "preferred_tables"))
        allowed_dimensions.extend(_metadata_list(metadata, "allowed_dimensions"))
        constraints.extend(_metadata_list(metadata, "constraints"))
        selected_skills.append(_routed_skill_from_candidate(candidate))

    first_enforcement_mode = str(selected_skills[0].enforcement_mode if selected_skills else "soft").strip().lower()
    is_hard = (
        settings.hard_enforcement_enabled
        and selector_decision.confidence >= settings.hard_score_threshold
        and metadata_complete
        and first_enforcement_mode in {"hard", "hard_candidate"}
    )
    return RouteDecision(
        strategy="hard_route" if is_hard else "soft_route",
        confidence=selector_decision.confidence,
        selected_skills=selected_skills,
        canonical_measures=_unique_strings(canonical_measures),
        required_schema_items=_required_schema_items({"required_schema_items": required_items}),
        preferred_tables=_unique_strings(preferred_tables),
        allowed_dimensions=_unique_strings(allowed_dimensions),
        constraints=_unique_strings(constraints),
        is_hard_route=is_hard,
    )


async def resolve_skill_route(
    *,
    user_message: str,
    report_id: int,
    empresa_id: Optional[int],
    dataset_id: Optional[str],
    settings: Optional[SkillRouterSettings] = None,
    usage_totals: Optional[Dict[str, int]] = None,
    ai_usage_events: Optional[List[Dict[str, Any]]] = None,
) -> RouteDecision:
    """Resolve a route from the original user message without interrupting chat."""
    router_settings = settings or build_skill_router_settings()
    if not router_settings.enabled:
        return RouteDecision(strategy="router_disabled", confidence=0.0, fallback_reason="router_disabled")

    with start_observation(
        name="resolve-skill-route",
        as_type="chain",
        input={"user_message": user_message},
    ) as observation:
        if observation is not None:
            observation.update(
                metadata={
                    "reportid": str(report_id),
                    "datasethash": hash_identifier(dataset_id, prefix="dataset") if dataset_id else None,
                    "routermode": router_settings.mode,
                    "routerenabled": "true",
                }
            )
        try:
            with start_observation(
                name="embed-skill-routing-query",
                as_type="embedding",
                input=[user_message],
            ) as embedding_observation:
                if embedding_observation is not None:
                    embedding_observation.update(
                        model=VOYAGE_SKILL_MODEL,
                        metadata={"provider": "voyageai", "inputtype": "query"},
                    )
                client = _get_voyage_client()
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: client.embed([user_message], model=VOYAGE_SKILL_MODEL, input_type="query")
                    ),
                    timeout=max(1, router_settings.timeout_seconds),
                )
                query_embedding = list(response.embeddings[0])
                total_tokens = int(getattr(response, "total_tokens", None) or 0)
                if usage_totals is not None:
                    usage_totals["input_tokens"] = int(usage_totals.get("input_tokens", 0)) + total_tokens
                if ai_usage_events is not None:
                    ai_usage_events.append(
                        {
                            "provider": "voyageai",
                            "model": VOYAGE_SKILL_MODEL,
                            "event_type": "embedding",
                            "source_type": "skill_router_embedding",
                            "trigger_type": "user_request",
                            "operation_name": "embed-skill-routing-query",
                            "status": "success",
                            "input_tokens": total_tokens,
                            "output_tokens": 0,
                            "total_tokens": total_tokens,
                            "metadata_json": {"input_type": "query", "router_mode": router_settings.mode},
                        }
                    )
                if embedding_observation is not None:
                    embedding_observation.update(
                        output={"embedding_dimensions": len(query_embedding), "vector_count": 1},
                        usage_details={"input": total_tokens} if total_tokens else None,
                    )
        except Exception as exc:
            LOG.exception("[SkillRouter] Query embedding failed")
            estimated_tokens = _estimate_tokens(user_message)
            if usage_totals is not None:
                usage_totals["input_tokens"] = int(usage_totals.get("input_tokens", 0)) + estimated_tokens
            if ai_usage_events is not None:
                ai_usage_events.append(
                    {
                        "provider": "voyageai",
                        "model": VOYAGE_SKILL_MODEL,
                        "event_type": "embedding",
                        "source_type": "skill_router_embedding",
                        "trigger_type": "user_request",
                        "operation_name": "embed-skill-routing-query",
                        "status": "error",
                        "input_tokens": estimated_tokens,
                        "output_tokens": 0,
                        "total_tokens": estimated_tokens,
                        "metadata_json": {
                            "input_type": "query",
                            "router_mode": router_settings.mode,
                            "estimated_usage": True,
                            "error_type": "voyage_provider_error",
                        },
                    }
                )
            decision = RouteDecision(strategy="router_error", confidence=0.0, fallback_reason="query_embedding_failed")
            if observation is not None:
                observation.update(output={"error": observation_preview(repr(exc), max_length=500)})
            return decision

        try:
            search_limit = router_settings.candidate_limit
            if router_settings.selector_enabled:
                search_limit = max(search_limit, router_settings.selector_candidate_limit)
            with start_observation(name="search-skill-candidates", as_type="retriever") as search_observation:
                candidates = await asyncio.to_thread(
                    search_skill_candidates,
                    query_embedding=query_embedding,
                    report_id=report_id,
                    empresa_id=empresa_id,
                    dataset_id=dataset_id,
                    limit=search_limit,
                )
                if search_observation is not None:
                    search_observation.update(output={"candidate_count": len(candidates)})
            vector_candidates = _dedupe_by_skill_key(candidates[: router_settings.candidate_limit])
            selector_candidates = _dedupe_by_skill_key(candidates[: router_settings.selector_candidate_limit])
            rerank_failed = False
            if router_settings.rerank_enabled and vector_candidates:
                with start_observation(name="rerank-skill-candidates", as_type="reranker") as rerank_observation:
                    rerank_result = await asyncio.to_thread(
                        _apply_rerank,
                        user_message,
                        vector_candidates,
                        usage_totals=usage_totals,
                        ai_usage_events=ai_usage_events,
                        router_mode=router_settings.mode,
                    )
                    rerank_ok = bool(rerank_result.get("ok"))
                    rerank_failed = not rerank_ok
                    if rerank_observation is not None:
                        usage = rerank_result.get("usage")
                        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
                        update_payload = {
                            "output": {
                                "rerank_ok": rerank_ok,
                                "total_tokens": total_tokens,
                                "estimated_usage": bool(getattr(usage, "estimated", False)),
                            },
                        }
                        if total_tokens:
                            update_payload["usage_details"] = {"input": total_tokens}
                        rerank_observation.update(**update_payload)
            vector_decision = _build_decision(candidates=vector_candidates, settings=router_settings)
            if rerank_failed and vector_decision.fallback_reason is None:
                vector_decision.fallback_reason = "rerank_failed_used_vector_ranking"

            selector_decision: Optional[SkillSelectorDecision] = None
            decision = vector_decision
            decision_source = "vector" if vector_decision.selected_skills else "none"
            if router_settings.selector_enabled and selector_candidates:
                selector_decision = await _select_skill_candidates(
                    user_message=user_message,
                    candidates=selector_candidates,
                    settings=router_settings,
                    usage_totals=usage_totals,
                    ai_usage_events=ai_usage_events,
                )
                if router_settings.selector_mode == "active":
                    if selector_decision.status == "error":
                        if decision.fallback_reason is None:
                            decision.fallback_reason = "selector_failed_used_vector_routing"
                        decision_source = "vector" if decision.selected_skills else "none"
                    elif selector_decision.no_skill_match:
                        decision = RouteDecision(
                            strategy="no_skill_match",
                            confidence=selector_decision.confidence,
                            fallback_reason="selector_no_skill_match",
                        )
                        decision_source = "none"
                    elif selector_decision.confidence < router_settings.selector_confidence_threshold:
                        decision = RouteDecision(
                            strategy="no_skill_match",
                            confidence=selector_decision.confidence,
                            fallback_reason="selector_confidence_below_threshold",
                        )
                        decision_source = "none"
                    else:
                        decision = _build_selector_decision(
                            candidates=selector_candidates,
                            selector_decision=selector_decision,
                            settings=router_settings,
                        )
                        decision_source = "llm_selector" if decision.selected_skills else "none"
            decision = _expand_required_companions(
                decision,
                report_id=report_id,
                empresa_id=empresa_id,
                dataset_id=dataset_id,
            )
            decision = _apply_route_metadata(
                decision,
                candidates=selector_candidates if router_settings.selector_enabled else vector_candidates,
                selector_decision=selector_decision,
                selector_mode=router_settings.selector_mode if router_settings.selector_enabled else None,
                decision_source=decision_source,
            )
            if observation is not None:
                observation.update(
                    output={
                        **decision.to_metadata(),
                        "candidate_count": len(
                            selector_candidates if router_settings.selector_enabled else vector_candidates
                        ),
                        "rerank_scores": [
                            round(float(candidate.get("rerank_score") or 0.0), 4)
                            for candidate in vector_candidates
                            if candidate.get("rerank_score") is not None
                        ],
                    }
                )
            return decision
        except Exception as exc:
            LOG.exception("[SkillRouter] Routing failed")
            if observation is not None:
                observation.update(output={"error": observation_preview(repr(exc), max_length=500)})
            return RouteDecision(strategy="router_error", confidence=0.0, fallback_reason="router_exception")
