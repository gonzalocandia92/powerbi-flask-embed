"""Administrative API and screens for KLARA's provider-neutral model catalog."""
import json
import re

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy.exc import IntegrityError

from app import db
from app.forms import AIModelConfigForm
from app.models import AIModelConfig, AIModelGrant, AIModelRoleAssignment, Empresa, Report
from app.services.agent_core import build_runtime_settings
from app.services import model_catalog as catalog_service
from app.services.llm.profiles import PROFILES, CONTROLLED_OPTIONS, profile_catalog
from app.utils.decorators import admin_required


bp = Blueprint('model_catalog', __name__, url_prefix='/admin/ai-models')
PROVIDERS = {'anthropic', 'openai', 'deepseek'}
GATEWAYS = {'direct', 'openrouter'}
SCOPES = {'global', 'empresa', 'report'}
ROLES = {'main_agent', 'query_rewriter', 'skill_selector', 'complexity_classifier'}
CONFIGURABLE_COMPONENTS = {'query_rewriter', 'skill_selector', 'complexity_classifier'}
COMPONENT_STRATEGIES = {
    'query_rewriter': {'model', 'disabled'},
    'skill_selector': {'model', 'embeddings', 'jev', 'jev_with_llm_fallback'},
    'complexity_classifier': {'model', 'disabled'},
}
SECRET_OPTION_MARKERS = ('api_key', 'secret', 'token', 'authorization', 'password')
MODEL_KEY_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


def _model_json(model, *, include_readiness=True):
    payload = {
        'model_key': model.model_key, 'display_name': model.display_name,
        'provider': model.provider, 'physical_model': model.physical_model,
        'family_key': model.family_key, 'family_options': model.family_options_json or {},
        'thinking_mode': model.thinking_mode,
        'gateway': model.gateway, 'enabled': model.enabled,
        'client_selectable': model.client_selectable,
        'validation_status': model.validation_status,
        'validated_at': model.validated_at.isoformat() if model.validated_at else None,
        'capabilities': {
            'supports_tools': model.supports_tools,
            'supports_reasoning': model.supports_reasoning,
            'supports_cache_key': model.supports_cache_key,
            'supports_flex': model.supports_flex,
            'context_window': model.context_window,
            'max_output_tokens': model.max_output_tokens,
        },
        'default_reasoning_effort': model.default_reasoning_effort,
        'default_verbosity': model.default_verbosity,
        'default_service_tier': model.default_service_tier,
        'pricing_tier': model.pricing_tier,
        'provider_options': model.provider_options_json or {},
    }
    if include_readiness:
        payload['readiness'] = catalog_service.model_readiness(model, config=dict(current_app.config))
    return payload


def _scope(data):
    scope_type = str(data.get('scope_type') or '').strip()
    scope_id = data.get('scope_id')
    if scope_type not in SCOPES:
        raise ValueError('scope_type must be global, empresa or report')
    if scope_type == 'global':
        return scope_type, None
    if scope_id is None or not str(scope_id).strip():
        raise ValueError('scope_id is required for empresa/report scope')
    return scope_type, str(scope_id).strip()


def _validate_provider_options(value):
    options = dict(value or {})

    def validate_nested(nested):
        if isinstance(nested, dict):
            for key, child in nested.items():
                if any(marker in str(key).lower() for marker in SECRET_OPTION_MARKERS):
                    raise ValueError('provider_options cannot contain secrets or credentials')
                if str(key).lower() in CONTROLLED_OPTIONS:
                    raise ValueError(f'provider option {key} is owned by the family profile')
                validate_nested(child)
        elif isinstance(nested, (list, tuple)):
            for child in nested:
                validate_nested(child)

    validate_nested(options)
    return options


@bp.route('', methods=['GET'])
@login_required
@admin_required
def list_models():
    models = AIModelConfig.query.order_by(AIModelConfig.display_name.asc()).all()
    return jsonify({
        'models': [_model_json(model) for model in models],
        'families': profile_catalog(),
        'grants': [{
            'id': grant.id, 'model_key': grant.model.model_key,
            'scope_type': grant.scope_type, 'scope_id': grant.scope_id,
            'allowed': grant.allowed, 'is_default': grant.is_default,
            'client_selectable': grant.client_selectable,
        } for grant in AIModelGrant.query.order_by(AIModelGrant.id.asc()).all()],
        'roles': [{
            'id': role.id, 'model_key': role.model.model_key if role.model else None,
            'role': role.role, 'strategy': role.strategy,
            'scope_type': role.scope_type, 'scope_id': role.scope_id,
            'is_active': role.is_active, 'max_output_tokens': role.max_output_tokens,
            'reasoning_effort': role.reasoning_effort, 'service_tier': role.service_tier,
            'thinking_mode': role.thinking_mode,
            'provider_options': role.provider_options_json or {},
        } for role in AIModelRoleAssignment.query.order_by(AIModelRoleAssignment.id.asc()).all()],
    })


def _apply_model(model, data, *, creating=False):
    if any(key in data for key in ('api_key', 'secret', 'base_url_secret')):
        raise ValueError('Secrets must be configured through environment variables')
    provider = str(data.get('provider', model.provider or '')).strip().lower()
    gateway = str(data.get('gateway', model.gateway or 'direct')).strip().lower()
    if provider not in PROVIDERS or gateway not in GATEWAYS:
        raise ValueError('Unsupported provider or gateway')
    old_transport = (model.provider, model.physical_model, model.gateway)
    model.provider = provider
    model.gateway = gateway
    if not creating and 'model_key' in data and str(data['model_key']).strip() != model.model_key:
        raise ValueError('model_key is immutable after creation')
    for field in ('model_key', 'display_name', 'physical_model'):
        value = str(data.get(field, getattr(model, field, '') or '')).strip()
        if not value:
            raise ValueError(f'{field} is required')
        setattr(model, field, value)
    if not MODEL_KEY_PATTERN.fullmatch(model.model_key):
        raise ValueError('model_key may contain only letters, numbers, dots, dashes and underscores')
    for field in ('enabled', 'client_selectable', 'supports_tools', 'supports_reasoning',
                  'supports_cache_key', 'supports_flex'):
        if field in data:
            setattr(model, field, bool(data[field]))
    for field in ('context_window', 'max_output_tokens'):
        if field in data:
            value = int(data[field])
            if value < 1:
                raise ValueError(f'{field} must be positive')
            setattr(model, field, value)
    for field in ('default_reasoning_effort', 'default_verbosity', 'default_service_tier', 'pricing_tier'):
        if field in data:
            setattr(model, field, data[field] or None)
    if 'family_key' in data:
        model.family_key = data['family_key'] or None
    if 'thinking_mode' in data:
        model.thinking_mode = data['thinking_mode'] or None
    if 'family_options' in data:
        model.family_options_json = dict(data['family_options'] or {})
    if 'provider_options' in data:
        model.provider_options_json = _validate_provider_options(data['provider_options'])
    if model.family_key:
        profile = PROFILES.get(model.family_key)
        if profile is None:
            raise ValueError('Unknown family_key')
        from types import SimpleNamespace
        profile.validate(SimpleNamespace(
            provider=model.provider, physical_model=model.physical_model, gateway=model.gateway,
            thinking_mode=model.thinking_mode, reasoning_effort=model.default_reasoning_effort,
            default_verbosity=model.default_verbosity,
            family_options=model.family_options_json or {}, max_output_tokens=model.max_output_tokens,
            capabilities=SimpleNamespace(context_window=model.context_window),
            provider_options=model.provider_options_json or {}, service_tier=model.default_service_tier,
        ))
        model.supports_reasoning = profile.supports_thinking
        model.supports_flex = profile.supports_flex
        model.supports_cache_key = profile.supports_report_cache_scope and model.gateway == 'direct'
    elif model.enabled:
        raise ValueError('Enabled models require a verified family profile')
    if not creating and old_transport != (model.provider, model.physical_model, model.gateway):
        model.validation_status = 'pending'
        model.validated_at = None


@bp.route('', methods=['POST'])
@login_required
@admin_required
def create_model():
    data = request.get_json(silent=True) or {}
    model = AIModelConfig()
    try:
        _apply_model(model, data, creating=True)
        model.validation_status = 'pending'
        db.session.add(model)
        db.session.commit()
    except (IntegrityError, TypeError, ValueError) as exc:
        db.session.rollback()
        message = 'model_key already exists' if isinstance(exc, IntegrityError) else str(exc)
        return jsonify({'error': message}), 400
    return jsonify(_model_json(model)), 201


@bp.route('/<string:model_key>', methods=['PATCH'])
@login_required
@admin_required
def update_model(model_key):
    model = AIModelConfig.query.filter_by(model_key=model_key).first_or_404()
    try:
        _apply_model(model, request.get_json(silent=True) or {})
        db.session.commit()
    except (TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400
    return jsonify(_model_json(model))


@bp.route('/grants', methods=['POST'])
@login_required
@admin_required
def set_grant():
    data = request.get_json(silent=True) or {}
    model = AIModelConfig.query.filter_by(model_key=data.get('model_key')).first()
    if model is None:
        return jsonify({'error': 'Unknown model_key'}), 400
    try:
        scope_type, scope_id = _scope(data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    grant = AIModelGrant.query.filter_by(model_id=model.id, scope_type=scope_type, scope_id=scope_id).first()
    if grant is None:
        grant = AIModelGrant(model_id=model.id, scope_type=scope_type, scope_id=scope_id)
        db.session.add(grant)
    grant.allowed = bool(data.get('allowed', True))
    grant.is_default = bool(data.get('is_default', False))
    grant.client_selectable = data.get('client_selectable') if 'client_selectable' in data else None
    if grant.is_default:
        query = AIModelGrant.query.filter(AIModelGrant.scope_type == scope_type, AIModelGrant.id != grant.id)
        query = query.filter(AIModelGrant.scope_id.is_(None)) if scope_id is None else query.filter(AIModelGrant.scope_id == scope_id)
        query.update({'is_default': False}, synchronize_session=False)
    db.session.commit()
    return jsonify({'id': grant.id, 'model_key': model.model_key}), 201


@bp.route('/roles', methods=['POST'])
@login_required
@admin_required
def set_role():
    data = request.get_json(silent=True) or {}
    role = str(data.get('role') or '').strip()
    strategy = str(data.get('strategy') or 'model').strip()
    model = AIModelConfig.query.filter_by(model_key=data.get('model_key')).first() if data.get('model_key') else None
    allowed = COMPONENT_STRATEGIES.get(role, {'model'})
    if role not in ROLES or strategy not in allowed or (strategy == 'model' and model is None):
        return jsonify({'error': 'Unknown model_key, role or strategy'}), 400
    try:
        scope_type, scope_id = _scope(data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    try:
        query = AIModelRoleAssignment.query.filter_by(role=role, scope_type=scope_type, scope_id=scope_id)
        query.update({'is_active': False}, synchronize_session=False)
        assignment = AIModelRoleAssignment(
            model_id=model.id if model else None, role=role, strategy=strategy,
            scope_type=scope_type, scope_id=scope_id,
            is_active=True,
            max_output_tokens=int(data['max_output_tokens']) if data.get('max_output_tokens') else None,
            reasoning_effort=data.get('reasoning_effort') or None,
            thinking_mode=data.get('thinking_mode') or None,
            service_tier=data.get('service_tier') or None,
            provider_options_json=_validate_provider_options(data.get('provider_options')),
        )
        db.session.add(assignment)
        db.session.commit()
    except (TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400
    return jsonify({'id': assignment.id, 'model_key': model.model_key if model else None,
                    'role': role, 'strategy': strategy}), 201


def _scope_values(scope_type, scope_id):
    if scope_type == 'global':
        return None, None
    value = int(scope_id)
    if scope_type == 'empresa':
        return None, value
    report = db.session.get(Report, value)
    if report is None:
        raise ValueError('Unknown report scope')
    from app.services import ai_billing
    return value, ai_billing.resolve_report_billing_context(report).empresa_id


def _direct_scope_query(model, scope_type, scope_id):
    query = model.query.filter(model.scope_type == scope_type)
    return query.filter(model.scope_id.is_(None)) if scope_id is None else query.filter(model.scope_id == scope_id)


def _scope_payload(scope_type, scope_id):
    report_id, empresa_id = _scope_values(scope_type, scope_id)
    normalized_id = None if scope_type == 'global' else str(scope_id)
    direct_grants = _direct_scope_query(AIModelGrant, scope_type, normalized_id).all()
    direct_roles = _direct_scope_query(AIModelRoleAssignment, scope_type, normalized_id).filter(
        AIModelRoleAssignment.is_active.is_(True)
    ).all()
    resolver = catalog_service.build_catalog_resolver(
        build_runtime_settings(dict(current_app.config)),
        report_id=report_id, empresa_id=empresa_id, config=dict(current_app.config),
    )
    components = {}
    for role in sorted(CONFIGURABLE_COMPONENTS):
        try:
            selected = resolver.component(role, report_id=report_id, empresa_id=empresa_id)
            components[role] = {
                'strategy': selected.strategy,
                'model_key': selected.model.model_key if selected.model else None,
                'source_scope': selected.source_scope,
                'source_scope_id': selected.source_scope_id,
            }
        except Exception as exc:
            components[role] = {'strategy': 'unavailable', 'model_key': None,
                                'source_scope': 'fallback', 'error': str(exc)}
    direct_role_map = {item.role: item for item in direct_roles}
    for role, item in components.items():
        direct = direct_role_map.get(role)
        item['direct'] = None if direct is None else {
            'strategy': direct.strategy,
            'model_key': direct.model.model_key if direct.model else None,
            'max_output_tokens': direct.max_output_tokens,
            'reasoning_effort': direct.reasoning_effort,
            'thinking_mode': direct.thinking_mode,
            'service_tier': direct.service_tier,
        }
    effective_grants = catalog_service._effective_grants(report_id=report_id, empresa_id=empresa_id)
    return {
        'scope_type': scope_type, 'scope_id': normalized_id,
        'grants': [{
            'model_key': grant.model.model_key, 'allowed': grant.allowed,
            'is_default': grant.is_default, 'client_selectable': grant.client_selectable,
            'direct': grant.id in {item.id for item in direct_grants},
        } for grant in effective_grants],
        'direct_grants': [{
            'model_key': grant.model.model_key, 'allowed': grant.allowed,
            'is_default': grant.is_default, 'client_selectable': grant.client_selectable,
        } for grant in direct_grants],
        'components': components,
    }


@bp.route('/effective', methods=['GET'])
@login_required
@admin_required
def effective_scope():
    try:
        scope_type, scope_id = _scope(request.args)
        return jsonify(_scope_payload(scope_type, scope_id))
    except (TypeError, ValueError) as exc:
        return jsonify({'error': str(exc)}), 400


@bp.route('/scope', methods=['PUT'])
@login_required
@admin_required
def replace_scope():
    data = request.get_json(silent=True) or {}
    try:
        scope_type, scope_id = _scope(data)
        grants = list(data.get('grants') or [])
        roles = dict(data.get('roles') or {})
        default_count = sum(bool(item.get('allowed', True) and item.get('is_default')) for item in grants)
        if (grants or scope_type == 'global') and default_count != 1:
            raise ValueError('Exactly one allowed model must be the default')
        normalized_id = None if scope_type == 'global' else scope_id
        _direct_scope_query(AIModelGrant, scope_type, normalized_id).delete(synchronize_session=False)
        _direct_scope_query(AIModelRoleAssignment, scope_type, normalized_id).filter(
            AIModelRoleAssignment.role.in_(CONFIGURABLE_COMPONENTS)
        ).delete(synchronize_session=False)
        for item in grants:
            model = AIModelConfig.query.filter_by(model_key=item.get('model_key')).first()
            if model is None:
                raise ValueError(f"Unknown model_key: {item.get('model_key')}")
            db.session.add(AIModelGrant(
                model_id=model.id, scope_type=scope_type, scope_id=normalized_id,
                allowed=bool(item.get('allowed', True)), is_default=bool(item.get('is_default', False)),
                client_selectable=item.get('client_selectable'),
            ))
        for role in CONFIGURABLE_COMPONENTS:
            item = roles.get(role) or {'strategy': 'inherit'}
            strategy = str(item.get('strategy') or 'inherit')
            if strategy == 'inherit':
                continue
            if strategy not in COMPONENT_STRATEGIES[role]:
                raise ValueError(f'Invalid strategy for {role}: {strategy}')
            model = AIModelConfig.query.filter_by(model_key=item.get('model_key')).first() if strategy == 'model' else None
            if strategy == 'model' and (model is None or not model.enabled):
                raise ValueError(f'Enabled model required for {role}')
            if item.get('thinking_mode') == 'off' and item.get('reasoning_effort'):
                raise ValueError(f'{role}: reasoning_effort requires thinking_mode=on')
            assignment = AIModelRoleAssignment(
                model_id=model.id if model else None, role=role, strategy=strategy,
                scope_type=scope_type, scope_id=normalized_id, is_active=True,
                max_output_tokens=int(item['max_output_tokens']) if item.get('max_output_tokens') else None,
                reasoning_effort=item.get('reasoning_effort') or None,
                thinking_mode=item.get('thinking_mode') or None,
                service_tier=item.get('service_tier') or None,
                provider_options_json=_validate_provider_options(item.get('provider_options')),
            )
            if model is not None and model.family_key:
                profile = PROFILES.get(model.family_key)
                if profile is None:
                    raise ValueError(f'Unknown family_key for {role}')
                configured = catalog_service.to_model_config(model, assignment=assignment)
                profile.validate(configured)
                if role == 'query_rewriter':
                    profile.validate_off_override(configured)
            db.session.add(assignment)
        db.session.commit()
        return jsonify(_scope_payload(scope_type, normalized_id))
    except (TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400


def _form_payload(form, *, include_key=True):
    try:
        options = json.loads(form.provider_options_json.data or '{}')
    except json.JSONDecodeError as exc:
        form.provider_options_json.errors.append(f'JSON invalido: {exc.msg}')
        return None
    payload = {
        'display_name': form.display_name.data, 'provider': form.provider.data,
        'family_key': form.family_key.data, 'thinking_mode': form.thinking_mode.data,
        'family_options': {
            **({'budget_tokens': form.budget_tokens.data} if form.budget_tokens.data else {}),
            **({'cache_by_report': True} if form.cache_by_report.data else {}),
        },
        'physical_model': form.physical_model.data, 'gateway': form.gateway.data,
        'enabled': form.enabled.data, 'client_selectable': form.client_selectable.data,
        'supports_tools': form.supports_tools.data, 'supports_reasoning': form.supports_reasoning.data,
        'supports_flex': form.supports_flex.data,
        'context_window': form.context_window.data, 'max_output_tokens': form.max_output_tokens.data,
        'default_reasoning_effort': form.default_reasoning_effort.data,
        'default_verbosity': form.default_verbosity.data,
        'default_service_tier': form.default_service_tier.data, 'pricing_tier': form.pricing_tier.data,
        'provider_options': options,
    }
    if include_key:
        payload['model_key'] = form.model_key.data
    return payload


@bp.route('/ui', methods=['GET'])
@login_required
@admin_required
def catalog_page():
    models = AIModelConfig.query.order_by(AIModelConfig.display_name.asc()).all()
    return render_template('admin/ai_models/catalog.html', models=models,
                           readiness={item.id: catalog_service.model_readiness(item, config=dict(current_app.config)) for item in models})


@bp.route('/ui/new', methods=['GET', 'POST'])
@login_required
@admin_required
def catalog_new():
    form = AIModelConfigForm()
    form.family_key.choices = [('', 'Seleccionar familia')] + [
        (item['key'], item['label']) for item in profile_catalog()
    ]
    form.default_reasoning_effort.choices = [('', 'Seleccionar nivel')] + [
        (level, level) for level in sorted({level for item in profile_catalog() for level in item['levels']})
    ]
    form.default_verbosity.choices = [('', 'Default del proveedor')] + [
        (level, level) for level in sorted({level for item in profile_catalog() for level in item['verbosity_levels']})
    ]
    if form.validate_on_submit():
        payload = _form_payload(form)
        if payload is not None:
            model = AIModelConfig()
            try:
                _apply_model(model, payload, creating=True)
                model.validation_status = 'pending'
                db.session.add(model)
                db.session.commit()
                flash('Modelo creado. Debe evaluarse antes de habilitarlo para usuarios.', 'success')
                return redirect(url_for('model_catalog.catalog_page'))
            except (IntegrityError, ValueError) as exc:
                db.session.rollback()
                form.model_key.errors.append(
                    'La clave interna ya existe.' if isinstance(exc, IntegrityError) else str(exc)
                )
    return render_template('admin/ai_models/form.html', form=form, model=None, families=profile_catalog())


@bp.route('/ui/<string:model_key>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def catalog_edit(model_key):
    model = AIModelConfig.query.filter_by(model_key=model_key).first_or_404()
    form = AIModelConfigForm(obj=model)
    form.family_key.choices = [('', 'Seleccionar familia')] + [
        (item['key'], item['label']) for item in profile_catalog()
    ]
    form.default_reasoning_effort.choices = [('', 'Seleccionar nivel')] + [
        (level, level) for level in sorted({level for item in profile_catalog() for level in item['levels']})
    ]
    form.default_verbosity.choices = [('', 'Default del proveedor')] + [
        (level, level) for level in sorted({level for item in profile_catalog() for level in item['verbosity_levels']})
    ]
    if request.method == 'GET':
        form.provider_options_json.data = json.dumps(model.provider_options_json or {}, ensure_ascii=False, indent=2)
        form.budget_tokens.data = (model.family_options_json or {}).get('budget_tokens')
        form.cache_by_report.data = bool((model.family_options_json or {}).get('cache_by_report'))
    if form.validate_on_submit():
        payload = _form_payload(form, include_key=False)
        if payload is not None:
            try:
                _apply_model(model, payload)
                db.session.commit()
                flash('Modelo actualizado.', 'success')
                return redirect(url_for('model_catalog.catalog_page'))
            except ValueError as exc:
                db.session.rollback()
                form.physical_model.errors.append(str(exc))
    return render_template('admin/ai_models/form.html', form=form, model=model, families=profile_catalog())


@bp.route('/ui/components', methods=['GET'])
@login_required
@admin_required
def components_page():
    return render_template(
        'admin/ai_models/components.html',
        models=AIModelConfig.query.order_by(AIModelConfig.display_name.asc()).all(),
        companies=Empresa.query.order_by(Empresa.nombre.asc()).all(),
        reports=Report.query.order_by(Report.name.asc()).all(),
    )
