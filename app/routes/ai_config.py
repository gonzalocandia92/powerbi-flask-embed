"""Administration routes for AI limits, prompts and model pricing."""
from datetime import datetime, time

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app import db
from app.forms import AgentPromptConfigForm, AIModelPricingForm, BillingLimitForm
from app.models import AgentPromptConfig, AIModelPricing, BillingLimit, Empresa, Report
from app.utils.decorators import retry_on_db_error


bp = Blueprint('ai_config', __name__, url_prefix='/admin/ai-config')


def _date_start(value):
    return datetime.combine(value, time.min) if value else None


def _date_end(value):
    return datetime.combine(value, time.max) if value else None


def _as_float(value):
    return float(value) if value is not None else 0.0


def _active_limits_by_scope():
    limits = (
        BillingLimit.query
        .filter(BillingLimit.period_type == 'monthly_anniversary')
        .order_by(BillingLimit.id.desc())
        .all()
    )
    result = {}
    for limit_item in limits:
        key = (limit_item.scope_type, limit_item.scope_id)
        if key not in result:
            result[key] = limit_item
    return result


def _active_prompts_by_scope():
    prompts = AgentPromptConfig.query.order_by(AgentPromptConfig.id.desc()).all()
    result = {}
    for prompt in prompts:
        key = (prompt.scope_type, prompt.scope_id)
        if key not in result:
            result[key] = prompt
    return result


@bp.route('/')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def index():
    """AI configuration overview."""
    active_tab = request.args.get('tab', 'limits')
    if active_tab not in {'limits', 'pricing', 'prompts'}:
        active_tab = 'limits'

    limits_by_scope = _active_limits_by_scope()
    prompts_by_scope = _active_prompts_by_scope()
    global_limit = limits_by_scope.get(('global', None))
    global_prompt = prompts_by_scope.get(('global', None))
    companies = Empresa.query.order_by(Empresa.nombre).all()
    reports = Report.query.order_by(Report.name).all()
    company_limits = [
        {
            'company': company,
            'limit': limits_by_scope.get(('empresa', str(company.id))),
        }
        for company in companies
    ]
    company_prompts = [
        {
            'company': company,
            'prompt': prompts_by_scope.get(('empresa', str(company.id))),
        }
        for company in companies
    ]
    report_prompts = [
        {
            'report': report,
            'prompt': prompts_by_scope.get(('report', str(report.id))),
        }
        for report in reports
    ]
    pricings = (
        AIModelPricing.query
        .order_by(
            AIModelPricing.is_active.desc(),
            AIModelPricing.provider,
            AIModelPricing.model,
            AIModelPricing.effective_from.desc(),
        )
        .all()
    )
    return render_template(
        'admin/ai_config/index.html',
        active_tab=active_tab,
        global_limit=global_limit,
        global_prompt=global_prompt,
        company_limits=company_limits,
        company_prompts=company_prompts,
        report_prompts=report_prompts,
        pricings=pricings,
    )


def _prompt_config(scope_type, scope_id):
    query = AgentPromptConfig.query.filter(AgentPromptConfig.scope_type == scope_type)
    if scope_id is None:
        query = query.filter(AgentPromptConfig.scope_id.is_(None))
    else:
        query = query.filter(AgentPromptConfig.scope_id == str(scope_id))
    return query.order_by(AgentPromptConfig.id.desc()).first()


def _populate_prompt_form_dates(form, prompt_item):
    if prompt_item:
        form.starts_at.data = prompt_item.starts_at.date() if prompt_item.starts_at else None
        form.ends_at.data = prompt_item.ends_at.date() if prompt_item.ends_at else None


def _populate_report_retrieval_form(form, report):
    form.schema_retrieval_prompt.data = report.schema_retrieval_prompt
    form.schema_table_context_limit.data = report.schema_table_context_limit
    form.schema_measure_context_limit.data = report.schema_measure_context_limit


def _save_report_retrieval_form(form, report):
    report.schema_retrieval_prompt = (form.schema_retrieval_prompt.data or '').strip() or None
    report.schema_table_context_limit = form.schema_table_context_limit.data
    report.schema_measure_context_limit = form.schema_measure_context_limit.data


def _save_prompt_form(form, prompt_item, *, scope_type, scope_id, default_title):
    if prompt_item is None:
        prompt_item = AgentPromptConfig(
            scope_type=scope_type,
            scope_id=str(scope_id) if scope_id is not None else None,
            title=default_title,
        )
        db.session.add(prompt_item)

    prompt_item.title = form.title.data.strip()
    prompt_item.instructions = form.instructions.data.strip()
    prompt_item.starts_at = _date_start(form.starts_at.data)
    prompt_item.ends_at = _date_end(form.ends_at.data)
    prompt_item.is_active = form.is_active.data
    return prompt_item


@bp.route('/prompts/global', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def global_prompt():
    """Create or edit the global agent prompt instructions."""
    prompt_item = _prompt_config('global', None)
    form = AgentPromptConfigForm(obj=prompt_item)
    if request.method == 'GET':
        if prompt_item:
            _populate_prompt_form_dates(form, prompt_item)
        else:
            form.title.data = 'Default global'
            form.is_active.data = True

    if form.validate_on_submit():
        if form.starts_at.data and form.ends_at.data and form.ends_at.data < form.starts_at.data:
            form.ends_at.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            _save_prompt_form(
                form,
                prompt_item,
                scope_type='global',
                scope_id=None,
                default_title='Default global',
            )
            db.session.commit()
            flash("Prompt global actualizado.", "success")
            return redirect(url_for('ai_config.index', tab='prompts'))

    return render_template(
        'admin/ai_config/prompt_form.html',
        form=form,
        title='Prompt global del agente',
        scope_label='Default Global',
    )


@bp.route('/prompts/company/<int:empresa_id>', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def company_prompt(empresa_id):
    """Create or edit company-specific agent prompt instructions."""
    company = Empresa.query.get_or_404(empresa_id)
    prompt_item = _prompt_config('empresa', company.id)
    form = AgentPromptConfigForm(obj=prompt_item)
    if request.method == 'GET':
        if prompt_item:
            _populate_prompt_form_dates(form, prompt_item)
        else:
            form.title.data = f'Prompt de {company.nombre}'
            form.is_active.data = True

    if form.validate_on_submit():
        if form.starts_at.data and form.ends_at.data and form.ends_at.data < form.starts_at.data:
            form.ends_at.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            _save_prompt_form(
                form,
                prompt_item,
                scope_type='empresa',
                scope_id=company.id,
                default_title=f'Prompt de {company.nombre}',
            )
            db.session.commit()
            flash(f"Prompt de {company.nombre} actualizado.", "success")
            return redirect(url_for('ai_config.index', tab='prompts'))

    return render_template(
        'admin/ai_config/prompt_form.html',
        form=form,
        title=f'Prompt de {company.nombre}',
        scope_label=company.nombre,
    )


@bp.route('/prompts/report/<int:report_id>', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def report_prompt(report_id):
    """Create or edit report-specific agent prompt instructions."""
    report = Report.query.get_or_404(report_id)
    prompt_item = _prompt_config('report', report.id)
    form = AgentPromptConfigForm(obj=prompt_item)
    if request.method == 'GET':
        if prompt_item:
            _populate_prompt_form_dates(form, prompt_item)
        else:
            form.title.data = f'Prompt de {report.name}'
            form.is_active.data = True
        _populate_report_retrieval_form(form, report)

    if form.validate_on_submit():
        if form.starts_at.data and form.ends_at.data and form.ends_at.data < form.starts_at.data:
            form.ends_at.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            _save_prompt_form(
                form,
                prompt_item,
                scope_type='report',
                scope_id=report.id,
                default_title=f'Prompt de {report.name}',
            )
            _save_report_retrieval_form(form, report)
            db.session.commit()
            flash(f"Prompt de {report.name} actualizado.", "success")
            return redirect(url_for('ai_config.index', tab='prompts'))

    return render_template(
        'admin/ai_config/prompt_form.html',
        form=form,
        title=f'Prompt de {report.name}',
        scope_label=report.name,
        retrieval_form_enabled=True,
    )


@bp.route('/limits/global', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def global_limit():
    """Create or edit the default global limit."""
    limit_item = (
        BillingLimit.query
        .filter(
            BillingLimit.scope_type == 'global',
            BillingLimit.scope_id.is_(None),
            BillingLimit.period_type == 'monthly_anniversary',
        )
        .order_by(BillingLimit.id.desc())
        .first()
    )
    form = BillingLimitForm(obj=limit_item)
    if request.method == 'GET' and limit_item:
        form.starts_at.data = limit_item.starts_at.date() if limit_item.starts_at else None
        form.ends_at.data = limit_item.ends_at.date() if limit_item.ends_at else None

    if form.validate_on_submit():
        if form.starts_at.data and form.ends_at.data and form.ends_at.data < form.starts_at.data:
            form.ends_at.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            if limit_item is None:
                limit_item = BillingLimit(
                    scope_type='global',
                    scope_id=None,
                    period_type='monthly_anniversary',
                    currency='USD',
                )
                db.session.add(limit_item)
            limit_item.limit_usd = _as_float(form.limit_usd.data)
            limit_item.cycle_anchor_day = form.cycle_anchor_day.data
            limit_item.starts_at = _date_start(form.starts_at.data)
            limit_item.ends_at = _date_end(form.ends_at.data)
            limit_item.is_active = form.is_active.data
            db.session.commit()
            flash("Default global actualizado.", "success")
            return redirect(url_for('ai_config.index', tab='limits'))

    return render_template(
        'admin/ai_config/limit_form.html',
        form=form,
        title='Editar default global',
        scope_label='Default Global',
    )


@bp.route('/limits/company/<int:empresa_id>', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def company_limit(empresa_id):
    """Create or edit the limit assigned to a company."""
    company = Empresa.query.get_or_404(empresa_id)
    limit_item = (
        BillingLimit.query
        .filter(
            BillingLimit.scope_type == 'empresa',
            BillingLimit.scope_id == str(company.id),
            BillingLimit.period_type == 'monthly_anniversary',
        )
        .order_by(BillingLimit.id.desc())
        .first()
    )
    form = BillingLimitForm(obj=limit_item)
    if request.method == 'GET':
        if limit_item:
            form.starts_at.data = limit_item.starts_at.date() if limit_item.starts_at else None
            form.ends_at.data = limit_item.ends_at.date() if limit_item.ends_at else None
        else:
            form.is_active.data = True
            form.cycle_anchor_day.data = 1

    if form.validate_on_submit():
        if form.starts_at.data and form.ends_at.data and form.ends_at.data < form.starts_at.data:
            form.ends_at.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            if limit_item is None:
                limit_item = BillingLimit(
                    scope_type='empresa',
                    scope_id=str(company.id),
                    period_type='monthly_anniversary',
                    currency='USD',
                )
                db.session.add(limit_item)
            limit_item.limit_usd = _as_float(form.limit_usd.data)
            limit_item.cycle_anchor_day = form.cycle_anchor_day.data
            limit_item.starts_at = _date_start(form.starts_at.data)
            limit_item.ends_at = _date_end(form.ends_at.data)
            limit_item.is_active = form.is_active.data
            db.session.commit()
            flash(f"Limite de {company.nombre} actualizado.", "success")
            return redirect(url_for('ai_config.index', tab='limits'))

    return render_template(
        'admin/ai_config/limit_form.html',
        form=form,
        title=f'Limite de {company.nombre}',
        scope_label=company.nombre,
    )


@bp.route('/pricing/new', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def pricing_new():
    """Create a model pricing record."""
    form = AIModelPricingForm()
    if request.method == 'GET':
        form.effective_from.data = datetime.utcnow().date()
        form.is_active.data = True

    if form.validate_on_submit():
        duplicate = AIModelPricing.query.filter(
            AIModelPricing.provider == form.provider.data.strip().lower(),
            AIModelPricing.model == form.model.data.strip(),
            AIModelPricing.event_type == form.event_type.data,
            AIModelPricing.is_active.is_(True),
        ).first()
        if duplicate and form.is_active.data:
            form.model.errors.append("Ya existe un pricing activo para esta combinacion.")
        elif form.effective_to.data and form.effective_to.data < form.effective_from.data:
            form.effective_to.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            pricing = AIModelPricing(
                provider=form.provider.data.strip().lower(),
                model=form.model.data.strip(),
                event_type=form.event_type.data,
                currency='USD',
                input_cost_per_million_usd=_as_float(form.input_cost_per_million_usd.data),
                output_cost_per_million_usd=_as_float(form.output_cost_per_million_usd.data),
                cache_write_cost_per_million_usd=_as_float(form.cache_write_cost_per_million_usd.data),
                cache_read_cost_per_million_usd=_as_float(form.cache_read_cost_per_million_usd.data),
                effective_from=_date_start(form.effective_from.data),
                effective_to=_date_end(form.effective_to.data),
                is_active=form.is_active.data,
            )
            db.session.add(pricing)
            db.session.commit()
            flash("Pricing creado.", "success")
            return redirect(url_for('ai_config.index', tab='pricing'))

    return render_template(
        'admin/ai_config/pricing_form.html',
        form=form,
        title='Nuevo pricing',
    )


@bp.route('/pricing/<int:pricing_id>/edit', methods=['GET', 'POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def pricing_edit(pricing_id):
    """Edit a model pricing record."""
    pricing = AIModelPricing.query.get_or_404(pricing_id)
    form = AIModelPricingForm(obj=pricing)
    if request.method == 'GET':
        form.effective_from.data = pricing.effective_from.date()
        form.effective_to.data = pricing.effective_to.date() if pricing.effective_to else None

    if form.validate_on_submit():
        duplicate = AIModelPricing.query.filter(
            AIModelPricing.id != pricing.id,
            AIModelPricing.provider == form.provider.data.strip().lower(),
            AIModelPricing.model == form.model.data.strip(),
            AIModelPricing.event_type == form.event_type.data,
            AIModelPricing.is_active.is_(True),
        ).first()
        if duplicate and form.is_active.data:
            form.model.errors.append("Ya existe otro pricing activo para esta combinacion.")
        elif form.effective_to.data and form.effective_to.data < form.effective_from.data:
            form.effective_to.errors.append("La fecha final no puede ser anterior a la inicial.")
        else:
            pricing.provider = form.provider.data.strip().lower()
            pricing.model = form.model.data.strip()
            pricing.event_type = form.event_type.data
            pricing.input_cost_per_million_usd = _as_float(form.input_cost_per_million_usd.data)
            pricing.output_cost_per_million_usd = _as_float(form.output_cost_per_million_usd.data)
            pricing.cache_write_cost_per_million_usd = _as_float(form.cache_write_cost_per_million_usd.data)
            pricing.cache_read_cost_per_million_usd = _as_float(form.cache_read_cost_per_million_usd.data)
            pricing.effective_from = _date_start(form.effective_from.data)
            pricing.effective_to = _date_end(form.effective_to.data)
            pricing.is_active = form.is_active.data
            db.session.commit()
            flash("Pricing actualizado.", "success")
            return redirect(url_for('ai_config.index', tab='pricing'))

    return render_template(
        'admin/ai_config/pricing_form.html',
        form=form,
        title=f'Editar pricing: {pricing.model}',
    )


@bp.route('/pricing/<int:pricing_id>/toggle', methods=['POST'])
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def pricing_toggle(pricing_id):
    """Activate or deactivate model pricing."""
    pricing = AIModelPricing.query.get_or_404(pricing_id)
    if not pricing.is_active:
        duplicate = AIModelPricing.query.filter(
            AIModelPricing.id != pricing.id,
            AIModelPricing.provider == pricing.provider,
            AIModelPricing.model == pricing.model,
            AIModelPricing.event_type == pricing.event_type,
            AIModelPricing.is_active.is_(True),
        ).first()
        if duplicate:
            flash("Ya existe otro pricing activo para esta combinacion.", "danger")
            return redirect(url_for('ai_config.index', tab='pricing'))

    pricing.is_active = not pricing.is_active
    db.session.commit()
    flash("Estado del pricing actualizado.", "success")
    return redirect(url_for('ai_config.index', tab='pricing'))
