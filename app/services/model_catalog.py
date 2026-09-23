"""Persistent model catalog, role resolution and client allowlists."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from app.models import AIModelConfig, AIModelGrant, AIModelRoleAssignment, ChatSession
from app.services.llm import ModelCapabilities, ModelConfig
from app.services.llm.profiles import PROFILES


MODEL_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


class ModelSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class ClientModelSelection:
    model: ModelConfig
    display_name: str
    requested_model_key: str | None
    source: str


@dataclass(frozen=True)
class ComponentSelection:
    role: str
    strategy: str
    model: ModelConfig | None
    source_scope: str
    source_scope_id: str | None


def _scope_chain(*, report_id: int | None, empresa_id: int | None):
    scopes = []
    if report_id is not None:
        scopes.append(("report", str(report_id)))
    if empresa_id is not None:
        scopes.append(("empresa", str(empresa_id)))
    scopes.append(("global", None))
    return scopes


def _scope_query(query, model, scope_type: str, scope_id: str | None):
    query = query.filter(model.scope_type == scope_type)
    return query.filter(model.scope_id.is_(None)) if scope_id is None else query.filter(model.scope_id == scope_id)


def _api_key_for(record: AIModelConfig, config: dict[str, Any] | None = None) -> str | None:
    config = config or {}
    key_provider = "openrouter" if record.gateway == "openrouter" else record.provider
    env_name = MODEL_API_KEY_ENV.get(key_provider, f"{key_provider.upper()}_API_KEY")
    return config.get(env_name) or os.getenv(env_name)


def to_model_config(
    record: AIModelConfig,
    *,
    assignment: AIModelRoleAssignment | None = None,
    config: dict[str, Any] | None = None,
) -> ModelConfig:
    options = dict(record.provider_options_json or {})
    if assignment is not None:
        options.update(assignment.provider_options_json or {})
    return ModelConfig(
        model_key=record.model_key,
        physical_model=record.physical_model,
        provider=record.provider,
        gateway=record.gateway,
        max_output_tokens=(assignment.max_output_tokens if assignment and assignment.max_output_tokens else record.max_output_tokens),
        capabilities=ModelCapabilities(
            supports_tools=record.supports_tools,
            supports_reasoning=record.supports_reasoning,
            supports_cache_key=record.supports_cache_key,
            supports_flex=record.supports_flex,
            context_window=record.context_window,
        ),
        service_tier=(assignment.service_tier if assignment and assignment.service_tier is not None else record.default_service_tier),
        pricing_tier=record.pricing_tier,
        reasoning_effort=(None if assignment and assignment.thinking_mode == 'off' else
                          assignment.reasoning_effort if assignment and assignment.reasoning_effort is not None else record.default_reasoning_effort),
        provider_options=options,
        api_key=_api_key_for(record, config),
        family_key=record.family_key,
        family_options=dict(record.family_options_json or {}),
        thinking_mode=(assignment.thinking_mode if assignment and assignment.thinking_mode is not None else record.thinking_mode),
        default_verbosity=record.default_verbosity,
    )


def get_model_config(model_key: str, *, require_enabled: bool = True, config=None) -> ModelConfig:
    record = AIModelConfig.query.filter_by(model_key=model_key).first()
    if record is None or (require_enabled and not record.enabled):
        raise ModelSelectionError(f"Modelo no disponible: {model_key}")
    return to_model_config(record, config=config)


def model_readiness(record: AIModelConfig, *, config=None) -> dict[str, Any]:
    """Return safe operational readiness without exposing credentials."""
    blockers = []
    credential_configured = bool(_api_key_for(record, config))
    if not credential_configured:
        blockers.append("credential_missing")
    if record.family_key:
        profile = PROFILES.get(record.family_key)
        if profile is None:
            blockers.append("profile_unknown")
        else:
            try:
                profile.validate(to_model_config(record, config=config))
            except ValueError:
                blockers.append("profile_invalid")
    elif record.provider in {"anthropic", "openai", "deepseek"}:
        blockers.append("profile_unverified")
    pricing_configured = False
    try:
        from app.services import ai_billing
        ai_billing.validate_pricing_coverage(to_model_config(record, config=config))
        pricing_configured = True
    except Exception:
        blockers.append("pricing_missing")
    if not record.enabled:
        blockers.append("disabled")
    if record.validation_status != "passed":
        blockers.append("validation_pending")
    return {
        "credential_configured": credential_configured,
        "pricing_configured": pricing_configured,
        "ready_for_evaluation": bool(record.enabled and
                                      not (set(blockers) - {'profile_unverified', 'validation_pending'})),
        "ready_for_chat": bool(
            record.enabled and record.client_selectable and record.validation_status == "passed"
            and not blockers
        ),
        "blockers": blockers,
    }


def _effective_grants(*, report_id: int | None, empresa_id: int | None) -> list[AIModelGrant]:
    for scope_type, scope_id in _scope_chain(report_id=report_id, empresa_id=empresa_id):
        query = _scope_query(AIModelGrant.query, AIModelGrant, scope_type, scope_id)
        grants = query.order_by(AIModelGrant.id.asc()).all()
        if grants:
            return grants
    return []


def available_client_models(*, report_id: int | None, empresa_id: int | None, config=None) -> list[dict[str, Any]]:
    result = []
    for grant in _effective_grants(report_id=report_id, empresa_id=empresa_id):
        record = grant.model
        selectable = record.client_selectable if grant.client_selectable is None else grant.client_selectable
        if not grant.allowed or not record.enabled or not selectable or record.validation_status != "passed":
            continue
        model = to_model_config(record, config=config)
        result.append({
            "model_key": model.model_key,
            "display_name": record.display_name,
            "provider": model.provider,
            "gateway": model.gateway,
            "supports_reasoning": model.capabilities.supports_reasoning,
            "service_tier": model.service_tier,
            "is_default": bool(grant.is_default),
        })
    result.sort(key=lambda item: (not item["is_default"], item["display_name"].casefold()))
    return result


def resolve_client_selection(
    *,
    requested_model_key: str | None,
    session_model_key: str | None,
    report_id: int | None,
    empresa_id: int | None,
    config=None,
) -> ClientModelSelection | None:
    available = available_client_models(report_id=report_id, empresa_id=empresa_id, config=config)
    if not available:
        if requested_model_key:
            raise ModelSelectionError(f"Modelo no permitido para este reporte: {requested_model_key}")
        return None
    by_key = {item["model_key"]: item for item in available}
    if requested_model_key:
        selected_key, source = requested_model_key, "request"
        if selected_key not in by_key:
            raise ModelSelectionError(f"Modelo no permitido para este reporte: {selected_key}")
    elif session_model_key and session_model_key in by_key:
        selected_key, source = session_model_key, "session"
    else:
        selected = next((item for item in available if item["is_default"]), available[0])
        selected_key, source = selected["model_key"], "default"
    record = AIModelConfig.query.filter_by(model_key=selected_key).one()
    return ClientModelSelection(
        model=to_model_config(record, config=config),
        display_name=record.display_name,
        requested_model_key=requested_model_key,
        source=source,
    )


def session_model_key(conversation_id: str | None, *, report_id: int | None) -> str | None:
    try:
        session_id = int(conversation_id) if conversation_id else None
    except (TypeError, ValueError):
        return None
    session = ChatSession.query.filter_by(id=session_id).first() if session_id is not None else None
    if session is None or (report_id is not None and session.report_id_fk not in (None, report_id)):
        return None
    return session.last_model_key


class CatalogModelRoleResolver:
    """Database assignments layered over the typed V1 resolver."""

    def __init__(self, fallback, *, report_id=None, empresa_id=None, selected_main=None,
                 config=None, fallback_strategies=None):
        self.fallback = fallback
        self.report_id = report_id
        self.empresa_id = empresa_id
        self.selected_main = selected_main
        self.config = config or {}
        self.fallback_strategies = dict(fallback_strategies or {})

    def _assignment(self, role: str, report_id=None, empresa_id=None):
        for scope_type, scope_id in _scope_chain(
            report_id=self.report_id if report_id is None else report_id,
            empresa_id=self.empresa_id if empresa_id is None else empresa_id,
        ):
            query = AIModelRoleAssignment.query.filter_by(role=role, is_active=True)
            query = _scope_query(query, AIModelRoleAssignment, scope_type, scope_id)
            assignment = query.order_by(AIModelRoleAssignment.id.desc()).first()
            if assignment is not None and (
                assignment.strategy != "model" or (assignment.model is not None and assignment.model.enabled)
            ):
                return assignment
        return None

    def component(self, role: str, *, report_id=None, empresa_id=None) -> ComponentSelection:
        assignment = self._assignment(role, report_id, empresa_id)
        if assignment is not None:
            model = (
                to_model_config(assignment.model, assignment=assignment, config=self.config)
                if assignment.strategy == "model" and assignment.model is not None else None
            )
            return ComponentSelection(
                role=role, strategy=assignment.strategy, model=model,
                source_scope=assignment.scope_type, source_scope_id=assignment.scope_id,
            )
        fallback_strategy = self.fallback_strategies.get(role, "model")
        if fallback_strategy != "model":
            return ComponentSelection(role, fallback_strategy, None, "fallback", None)
        model = self.fallback.resolve(role, report_id=report_id, empresa_id=empresa_id)
        return ComponentSelection(role, "model", model, "fallback", None)

    def resolve(self, role: str, *, report_id=None, empresa_id=None) -> ModelConfig:
        if role == "main_agent" and self.selected_main is not None:
            return self.selected_main
        component = self.component(role, report_id=report_id, empresa_id=empresa_id)
        if component.strategy != "model" or component.model is None:
            raise ModelSelectionError(f"El componente {role} usa estrategia {component.strategy}")
        return component.model


def build_catalog_resolver(settings, *, report_id=None, empresa_id=None, selection=None, config=None):
    from app.services.klara_execution import configured_model_roles
    fallback = configured_model_roles(settings, settings.role_configuration)
    return CatalogModelRoleResolver(
        fallback, report_id=report_id, empresa_id=empresa_id,
        selected_main=selection.model if selection else None, config=config,
        fallback_strategies={
            "query_rewriter": "model",
            "skill_selector": (
                "model" if settings.skill_router_settings.selector_enabled else "embeddings"
            ),
            "complexity_classifier": "disabled",
        },
    )


def build_execution_resolver(settings, *, report_id=None, empresa_id=None, model_key=None, config=None):
    """Build effective execution roles with an optional trusted main-model override.

    Unlike ``resolve_client_selection``, this helper intentionally does not apply
    the Chat client allowlist. Consumers are responsible for authorizing any
    externally supplied model key before creating an analytics request.
    """
    selection = None
    if model_key is not None:
        selected = get_model_config(model_key, require_enabled=True, config=config)
        selection = ClientModelSelection(selected, model_key, model_key, "execution_override")
    return build_catalog_resolver(
        settings,
        report_id=report_id,
        empresa_id=empresa_id,
        selection=selection,
        config=config,
    )


def evaluation_model_resolver(settings, model_key: str, *, report_id=None, empresa_id=None, config=None):
    return build_execution_resolver(
        settings, report_id=report_id, empresa_id=empresa_id,
        model_key=model_key, config=config,
    )
