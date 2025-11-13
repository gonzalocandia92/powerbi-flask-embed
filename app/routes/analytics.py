"""
Analytics routes for viewing visit metrics.
"""
import logging
from datetime import datetime, timedelta
from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required

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
    link_slug = request.args.get('link_slug')
    days = request.args.get('days', '30')
    
    try:
        days_int = int(days)
        days_int = max(1, min(days_int, 365))
    except (ValueError, TypeError):
        days_int = 30
    
    start_date, end_date = parse_date_range(days)
    
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
        days=days_int
    )
