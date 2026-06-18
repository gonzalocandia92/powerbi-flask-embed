"""
Analytics routes for viewing visit metrics.
"""
import logging
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func, or_

from app.utils.analytics import (
    get_visit_stats,
    get_visits_by_hour,
    get_visits_by_day,
    get_top_referrers,
    get_utm_stats,
    get_device_browser_stats
)
from app.utils.decorators import retry_on_db_error

bp = Blueprint('analytics', __name__, url_prefix='/analytics')

logger = logging.getLogger(__name__)


def _ai_cost_scope_query(query, scope):
    """Apply the selected billing scope to an AI usage query."""
    from app.models import AIUsageEvent

    if scope == 'global':
        return query.filter(
            AIUsageEvent.billing_scope_type == 'global',
            AIUsageEvent.billing_scope_id.is_(None),
        )
    return query.filter(
        AIUsageEvent.billing_scope_type == 'empresa',
        AIUsageEvent.billing_scope_id == str(scope),
    )


def parse_date_range(days_param: str = '30') -> tuple:
    """
    Parse date range from request parameter.
    
    Args:
        days_param: Number of days as string
        
    Returns:
        Tuple of (start_date, end_date)
    """
    try:
        days = int(days_param)
        days = max(1, min(days, 365))  # Limit to 1-365 days
    except (ValueError, TypeError):
        days = 30
    
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days)
    
    return start_date, end_date


@bp.route('/api/stats')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def api_stats():
    """
    API endpoint for analytics statistics (JSON).
    
    Query parameters:
        - link_slug: Optional filter by link slug
        - days: Number of days to look back (default 30)
    """
    try:
        link_slug = request.args.get('link_slug')
        days = request.args.get('days', '30')
        
        start_date, end_date = parse_date_range(days)
        
        # Get all statistics
        stats = get_visit_stats(link_slug, start_date, end_date)
        hourly = get_visits_by_hour(link_slug, int(days))
        daily = get_visits_by_day(link_slug, int(days))
        referrers = get_top_referrers(link_slug, int(days))
        utm_stats = get_utm_stats(link_slug, int(days))
        device_browser = get_device_browser_stats(link_slug, int(days))
        
        return jsonify({
            'success': True,
            'data': {
                'overview': stats,
                'hourly_distribution': hourly,
                'daily_trend': daily,
                'top_referrers': referrers,
                'utm': utm_stats,
                'devices': device_browser['devices'],
                'browsers': device_browser['browsers']
            }
        })
        
    except Exception as e:
        logger.error(f"Error in analytics API: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'An error occurred while fetching analytics data'
        }), 500


@bp.route('/api/search-links')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def search_links():
    """
    Search for public links by slug or report name.
    
    Query parameters:
        - q: Search query
        
    Returns:
        JSON array of matching links (max 10)
    """
    from app.models import PublicLink, Report
    from sqlalchemy import or_
    
    query = request.args.get('q', '').strip()
    
    try:
        links_query = PublicLink.query.filter(
            PublicLink.is_active == True
        ).join(PublicLink.report)
        
        if query:
            links_query = links_query.filter(
                or_(
                    PublicLink.custom_slug.ilike(f'%{query}%'),
                    Report.name.ilike(f'%{query}%')
                )
            )
        
        links = links_query.order_by(PublicLink.custom_slug).limit(10).all()
        
        results = []
        for link in links:
            results.append({
                'slug': link.custom_slug,
                'report_name': link.report.name if link.report else 'Unknown'
            })
        
        return jsonify({
            'success': True,
            'links': results
        })
        
    except Exception as e:
        logger.error(f"Error searching links: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'An error occurred while searching links'
        }), 500


@bp.route('/dashboard')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def dashboard():
    """
    Analytics dashboard view.
    
    Query parameters:
        - link_slug: Optional filter by link slug
        - days: Number of days to look back (default 30)
    """
    from app.models import PublicLink
    
    link_slug = request.args.get('link_slug')
    days = request.args.get('days', '30')
    
    try:
        days_int = int(days)
        days_int = max(1, min(days_int, 365))
    except (ValueError, TypeError):
        days_int = 30
    
    start_date, end_date = parse_date_range(days)
    
    # Get available public links for the dropdown
    available_links = PublicLink.query.filter_by(is_active=True).limit(10).all()
    
    # Get statistics
    stats = get_visit_stats(link_slug, start_date, end_date)
    hourly = get_visits_by_hour(link_slug, days_int)
    daily = get_visits_by_day(link_slug, days_int)
    referrers = get_top_referrers(link_slug, days_int)
    utm_stats = get_utm_stats(link_slug, days_int)
    device_browser = get_device_browser_stats(link_slug, days_int)
    
    return render_template(
        'analytics/dashboard.html',
        stats=stats,
        hourly=hourly,
        daily=daily,
        referrers=referrers,
        utm_stats=utm_stats,
        devices=device_browser['devices'],
        browsers=device_browser['browsers'],
        link_slug=link_slug,
        days=days_int,
        available_links=available_links
    )


@bp.route('/ai-costs')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def ai_costs():
    """General AI cost dashboard grouped by company."""
    from app.models import AIUsageEvent, Empresa
    from app.services.ai_billing import resolve_billing_limit

    company_search = request.args.get('company', '').strip()
    period = request.args.get('period', '30')
    status_filter = request.args.get('status', '').strip()
    provider = request.args.get('provider', '').strip()
    now = datetime.utcnow()

    companies = Empresa.query.order_by(Empresa.nombre).all()
    if company_search:
        normalized_search = company_search.lower()
        filtered_companies = [
            company for company in companies
            if normalized_search in company.nombre.lower()
        ]
        include_global = normalized_search in 'sin empresa / global'
    else:
        filtered_companies = companies
        include_global = True

    if period not in {'7', '30', '90'}:
        period = '30'
    start_date = now - timedelta(days=int(period))
    end_date = now

    selected_company_ids = [company.id for company in filtered_companies]
    base_query = AIUsageEvent.query.filter(
        AIUsageEvent.created_at >= start_date,
        AIUsageEvent.created_at < end_date,
    )
    if provider:
        base_query = base_query.filter(AIUsageEvent.provider == provider)

    scope_conditions = []
    if selected_company_ids:
        scope_conditions.append(AIUsageEvent.empresa_id.in_(selected_company_ids))
    if include_global:
        scope_conditions.append(
            (AIUsageEvent.billing_scope_type == 'global')
            & AIUsageEvent.billing_scope_id.is_(None)
        )
    if scope_conditions:
        base_query = base_query.filter(or_(*scope_conditions))
    else:
        base_query = base_query.filter(AIUsageEvent.id.is_(None))

    totals = base_query.with_entities(
        func.coalesce(func.sum(AIUsageEvent.total_cost_usd), 0.0),
        func.coalesce(func.sum(AIUsageEvent.total_tokens), 0),
        func.count(AIUsageEvent.id),
    ).first()
    spent_usd = float(totals[0] or 0.0)
    total_tokens = int(totals[1] or 0)
    total_events = int(totals[2] or 0)

    company_aggregates = {
        row.empresa_id: row
        for row in (
            base_query
            .filter(AIUsageEvent.empresa_id.in_(selected_company_ids or [-1]))
            .with_entities(
                AIUsageEvent.empresa_id,
                func.coalesce(func.sum(AIUsageEvent.total_cost_usd), 0.0).label('cost_usd'),
                func.coalesce(func.sum(AIUsageEvent.total_tokens), 0).label('tokens'),
                func.count(AIUsageEvent.id).label('events'),
                func.max(AIUsageEvent.created_at).label('last_activity'),
            )
            .group_by(AIUsageEvent.empresa_id)
            .all()
        )
    }

    global_aggregate = (
        base_query
        .filter(
            AIUsageEvent.billing_scope_type == 'global',
            AIUsageEvent.billing_scope_id.is_(None),
        )
        .with_entities(
            func.coalesce(func.sum(AIUsageEvent.total_cost_usd), 0.0).label('cost_usd'),
            func.coalesce(func.sum(AIUsageEvent.total_tokens), 0).label('tokens'),
            func.count(AIUsageEvent.id).label('events'),
            func.max(AIUsageEvent.created_at).label('last_activity'),
        )
        .first()
    )

    def build_scope_row(label, scope, aggregate, empresa_id=None):
        active_limit = resolve_billing_limit(empresa_id=empresa_id, as_of=now)
        limit_usd = float(active_limit.limit_usd) if active_limit else None
        cost_usd = float(aggregate.cost_usd or 0.0) if aggregate else 0.0
        remaining_usd = limit_usd - cost_usd if limit_usd is not None else None
        usage_percent = (cost_usd / limit_usd * 100) if limit_usd else 0.0
        if scope == 'global':
            status = 'Global'
        elif limit_usd is None:
            status = 'Sin limite'
        elif usage_percent >= 100:
            status = 'Excedido'
        elif usage_percent >= 80:
            status = 'Cerca del limite'
        else:
            status = 'Normal'
        return {
            'label': label,
            'scope': scope,
            'limit_usd': limit_usd,
            'cost_usd': cost_usd,
            'remaining_usd': remaining_usd,
            'usage_percent': usage_percent,
            'last_activity': aggregate.last_activity if aggregate else None,
            'status': status,
        }

    scope_rows = [
        build_scope_row(
            company.nombre,
            str(company.id),
            company_aggregates.get(company.id),
            empresa_id=company.id,
        )
        for company in filtered_companies
    ]
    if include_global:
        scope_rows.append(
            build_scope_row('Sin empresa / Global', 'global', global_aggregate)
        )

    if status_filter:
        scope_rows = [
            row for row in scope_rows
            if row['status'].lower().replace(' ', '_') == status_filter
        ]
        filtered_scope_conditions = []
        filtered_company_ids = [
            int(row['scope']) for row in scope_rows
            if row['scope'] != 'global'
        ]
        if filtered_company_ids:
            filtered_scope_conditions.append(
                AIUsageEvent.empresa_id.in_(filtered_company_ids)
            )
        if any(row['scope'] == 'global' for row in scope_rows):
            filtered_scope_conditions.append(
                (AIUsageEvent.billing_scope_type == 'global')
                & AIUsageEvent.billing_scope_id.is_(None)
            )
        if filtered_scope_conditions:
            base_query = base_query.filter(or_(*filtered_scope_conditions))
        else:
            base_query = base_query.filter(AIUsageEvent.id.is_(None))

        filtered_totals = base_query.with_entities(
            func.coalesce(func.sum(AIUsageEvent.total_cost_usd), 0.0),
            func.coalesce(func.sum(AIUsageEvent.total_tokens), 0),
            func.count(AIUsageEvent.id),
        ).first()
        spent_usd = float(filtered_totals[0] or 0.0)
        total_tokens = int(filtered_totals[1] or 0)
        total_events = int(filtered_totals[2] or 0)

    daily_results = (
        base_query
        .with_entities(
            func.date(AIUsageEvent.created_at).label('date'),
            func.coalesce(func.sum(AIUsageEvent.total_cost_usd), 0.0).label('cost_usd'),
        )
        .group_by(func.date(AIUsageEvent.created_at))
        .order_by(func.date(AIUsageEvent.created_at))
        .all()
    )
    event_results = (
        base_query
        .with_entities(
            AIUsageEvent.event_type,
            func.coalesce(func.sum(AIUsageEvent.total_cost_usd), 0.0).label('cost_usd'),
        )
        .group_by(AIUsageEvent.event_type)
        .order_by(func.sum(AIUsageEvent.total_cost_usd).desc())
        .all()
    )
    daily_rows = [
        {
            'date': str(row.date),
            'cost_usd': float(row.cost_usd or 0.0),
        }
        for row in daily_results
    ]
    event_rows = [
        {
            'event_type': row.event_type or 'other',
            'cost_usd': float(row.cost_usd or 0.0),
        }
        for row in event_results
    ]
    providers = [
        row[0]
        for row in AIUsageEvent.query.with_entities(AIUsageEvent.provider)
        .distinct().order_by(AIUsageEvent.provider).all()
        if row[0]
    ]
    active_companies = sum(
        1 for row in scope_rows
        if row['scope'] != 'global' and row['cost_usd'] > 0
    )

    return render_template(
        'analytics/ai_costs.html',
        company_search=company_search,
        period=period,
        status_filter=status_filter,
        provider=provider,
        providers=providers,
        start_date=start_date,
        end_date=end_date,
        spent_usd=spent_usd,
        total_tokens=total_tokens,
        total_events=total_events,
        active_companies=active_companies,
        scope_rows=scope_rows,
        daily_rows=daily_rows,
        event_rows=event_rows,
    )


@bp.route('/ai-costs/<scope>')
@login_required
@retry_on_db_error(max_retries=3, delay=1)
def ai_cost_detail(scope):
    """Detailed AI consumption dashboard for one billing scope."""
    from app.models import AIUsageEvent, Empresa, Report
    from app.services.ai_billing import monthly_anniversary_window, resolve_billing_limit

    period = request.args.get('period', 'cycle')
    report_id = request.args.get('report_id', type=int)
    event_type = request.args.get('event_type', '').strip()
    now = datetime.utcnow()

    companies = Empresa.query.order_by(Empresa.nombre).all()
    valid_company_ids = {str(company.id) for company in companies}
    if scope != 'global' and scope not in valid_company_ids:
        return redirect(url_for('analytics.ai_costs'))

    active_limit = resolve_billing_limit(
        empresa_id=int(scope) if scope != 'global' else None,
        as_of=now,
    )
    if period == 'cycle' and active_limit:
        window = monthly_anniversary_window(active_limit, as_of=now)
        start_date, end_date = window.cycle_start, window.cycle_end
    elif period in {'7', '30', '90'}:
        start_date, end_date = now - timedelta(days=int(period)), now
    else:
        period = 'cycle'
        start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_date = now

    base_query = _ai_cost_scope_query(AIUsageEvent.query, scope).filter(
        AIUsageEvent.created_at >= start_date,
        AIUsageEvent.created_at < end_date,
    )
    if report_id:
        base_query = base_query.filter(AIUsageEvent.report_id_fk == report_id)
    if event_type:
        base_query = base_query.filter(AIUsageEvent.event_type == event_type)

    totals = base_query.with_entities(
        func.coalesce(func.sum(AIUsageEvent.total_cost_usd), 0.0),
        func.coalesce(func.sum(AIUsageEvent.total_tokens), 0),
        func.count(AIUsageEvent.id),
    ).first()
    spent_usd = float(totals[0] or 0.0)
    total_tokens = int(totals[1] or 0)
    total_events = int(totals[2] or 0)
    limit_usd = float(active_limit.limit_usd) if active_limit else None
    remaining_usd = max(0.0, limit_usd - spent_usd) if limit_usd is not None else None
    usage_percent = min(100.0, (spent_usd / limit_usd * 100)) if limit_usd else 0.0
    if limit_usd is None:
        consumption_status = 'Sin limite'
    elif spent_usd >= limit_usd:
        consumption_status = 'Limite alcanzado'
    elif usage_percent >= 80:
        consumption_status = 'Atencion'
    else:
        consumption_status = 'Normal'

    report_rows = (
        base_query
        .outerjoin(Report, AIUsageEvent.report_id_fk == Report.id)
        .with_entities(
            AIUsageEvent.report_id_fk,
            func.coalesce(Report.name, 'Sin reporte').label('report_name'),
            func.coalesce(func.sum(AIUsageEvent.total_cost_usd), 0.0).label('cost_usd'),
            func.coalesce(func.sum(AIUsageEvent.total_tokens), 0).label('tokens'),
            func.count(AIUsageEvent.id).label('events'),
            func.max(AIUsageEvent.created_at).label('last_activity'),
        )
        .group_by(AIUsageEvent.report_id_fk, Report.name)
        .order_by(func.sum(AIUsageEvent.total_cost_usd).desc())
        .limit(20)
        .all()
    )
    recent_events = base_query.order_by(AIUsageEvent.created_at.desc()).limit(8).all()
    reports = Report.query.order_by(Report.name).all()
    event_types = [
        row[0] for row in
        AIUsageEvent.query.with_entities(AIUsageEvent.event_type)
        .distinct().order_by(AIUsageEvent.event_type).all()
        if row[0]
    ]
    selected_company = next(
        (company for company in companies if str(company.id) == scope),
        None,
    )

    return render_template(
        'analytics/ai_cost_detail.html',
        selected_company=selected_company,
        scope=scope,
        period=period,
        report_id=report_id,
        event_type=event_type,
        reports=reports,
        event_types=event_types,
        start_date=start_date,
        end_date=end_date,
        spent_usd=spent_usd,
        total_tokens=total_tokens,
        total_events=total_events,
        limit_usd=limit_usd,
        remaining_usd=remaining_usd,
        usage_percent=usage_percent,
        consumption_status=consumption_status,
        report_rows=report_rows,
        recent_events=recent_events,
    )
