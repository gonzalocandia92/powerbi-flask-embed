"""
Analytics service for tracking and aggregating visit metrics.
"""
import hashlib
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import uuid

from user_agents import parse
from sqlalchemy import func, extract
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.models import Visit

logger = logging.getLogger(__name__)

# Environment configuration
ANALYTICS_ENABLED = os.getenv('ANALYTICS_ENABLED', 'true').lower() == 'true'
ANALYTICS_SALT = os.getenv('ANALYTICS_SALT', 'default-salt-change-in-production')
DNT_RESPECT = os.getenv('ANALYTICS_RESPECT_DNT', 'true').lower() == 'true'

# Bot detection patterns
BOT_PATTERNS = [
    'bot', 'crawl', 'spider', 'slurp', 'scrape', 'scraper',
    'facebookexternalhit', 'twitterbot', 'linkedinbot',
    'whatsapp', 'telegram', 'slackbot', 'discordbot',
    'googlebot', 'bingbot', 'yahoo', 'duckduckgo',
    'headless', 'phantom', 'selenium', 'webdriver',
    'curl', 'wget', 'python-requests', 'postman',
]


def anonymize_ip(ip_address: str) -> str:
    """
    Anonymize IP address by hashing with salt.
    
    Args:
        ip_address: The IP address to anonymize
        
    Returns:
        Hashed IP address
    """
    if not ip_address:
        return ''
    
    combined = f"{ip_address}{ANALYTICS_SALT}"
    return hashlib.sha256(combined.encode()).hexdigest()


def is_bot(user_agent: str) -> bool:
    """
    Detect if user agent appears to be a bot.
    
    Args:
        user_agent: User agent string
        
    Returns:
        True if bot detected, False otherwise
    """
    if not user_agent:
        return True
    
    user_agent_lower = user_agent.lower()
    
    # Check against known bot patterns
    for pattern in BOT_PATTERNS:
        if pattern in user_agent_lower:
            return True
    
    return False


def parse_user_agent(user_agent: str) -> Dict[str, str]:
    """
    Parse user agent string to extract device, browser, and OS info.
    
    Args:
        user_agent: User agent string
        
    Returns:
        Dictionary with device_type, browser, and os
    """
    if not user_agent:
        return {'device_type': 'unknown', 'browser': 'unknown', 'os': 'unknown'}
    
    try:
        ua = parse(user_agent)
        
        # Determine device type
        if ua.is_mobile:
            device_type = 'mobile'
        elif ua.is_tablet:
            device_type = 'tablet'
        elif ua.is_pc:
            device_type = 'pc'
        else:
            device_type = 'other'
        
        browser = f"{ua.browser.family} {ua.browser.version_string}" if ua.browser.family else 'unknown'
        operating_system = f"{ua.os.family} {ua.os.version_string}" if ua.os.family else 'unknown'
        
        return {
            'device_type': device_type,
            'browser': browser[:100],  # Limit length
            'os': operating_system[:100]
        }
    except Exception as e:
        logger.error(f"Error parsing user agent: {e}")
        return {'device_type': 'unknown', 'browser': 'unknown', 'os': 'unknown'}


def should_track_visit(request) -> bool:
    """
    Determine if this visit should be tracked based on DNT and other factors.
    
    Args:
        request: Flask request object
        
    Returns:
        True if visit should be tracked
    """
    if not ANALYTICS_ENABLED:
        return False
    
    # Respect Do Not Track header
    if DNT_RESPECT:
        dnt = request.headers.get('DNT', '0')
        if dnt == '1':
            return False
    
    return True


def track_visit(
    link_slug: str,
    request,
    visitor_id: Optional[str] = None
) -> Optional[Visit]:
    """
    Track a visit to a public link.
    
    Args:
        link_slug: The slug of the public link
        request: Flask request object
        visitor_id: Optional visitor UUID from cookie
        
    Returns:
        Visit object if tracked, None otherwise
    """
    if not should_track_visit(request):
        return None
    
    try:
        # Extract request data
        user_agent = request.headers.get('User-Agent', '')
        ip_address = request.remote_addr or ''
        referrer = request.referrer or ''
        
        # Parse UTM parameters
        utm_source = request.args.get('utm_source', '')
        utm_medium = request.args.get('utm_medium', '')
        utm_campaign = request.args.get('utm_campaign', '')
        
        # Detect bot
        is_bot_visit = is_bot(user_agent)
        
        # Parse user agent
        ua_info = parse_user_agent(user_agent)
        
        # Create visit record
        visit = Visit(
            link_slug=link_slug,
            timestamp=datetime.utcnow(),
            visitor_id=visitor_id,
            ip_hash=anonymize_ip(ip_address),
            user_agent=user_agent[:500] if user_agent else None,
            referrer=referrer[:1000] if referrer else None,
            utm_source=utm_source[:100] if utm_source else None,
            utm_medium=utm_medium[:100] if utm_medium else None,
            utm_campaign=utm_campaign[:100] if utm_campaign else None,
            device_type=ua_info['device_type'],
            browser=ua_info['browser'],
            os=ua_info['os'],
            is_bot=is_bot_visit
        )
        
        db.session.add(visit)
        db.session.commit()
        
        logger.info(f"Tracked visit to {link_slug} from visitor {visitor_id}")
        return visit
        
    except SQLAlchemyError as e:
        logger.error(f"Database error tracking visit: {e}")
        db.session.rollback()
        return None
    except Exception as e:
        logger.error(f"Error tracking visit: {e}")
        return None


def generate_visitor_id() -> str:
    """
    Generate a unique visitor ID.
    
    Returns:
        UUID string
    """
    return str(uuid.uuid4())


def get_visit_stats(
    link_slug: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Get aggregated visit statistics.
    
    Args:
        link_slug: Optional filter by link slug
        start_date: Optional start date for filtering
        end_date: Optional end date for filtering
        
    Returns:
        Dictionary with aggregated statistics
    """
    try:
        # Build base query
        query = Visit.query
        
        if link_slug:
            query = query.filter(Visit.link_slug == link_slug)
        
        if start_date:
            query = query.filter(Visit.timestamp >= start_date)
        
        if end_date:
            query = query.filter(Visit.timestamp <= end_date)
        
        # Filter out bots for most stats
        human_query = query.filter(Visit.is_bot == False)
        
        # Total visits (excluding bots)
        total_visits = human_query.count()
        
        # Unique visitors
        unique_visitors = db.session.query(
            func.count(func.distinct(Visit.visitor_id))
        ).select_from(Visit).filter(
            Visit.is_bot == False
        )
        
        if link_slug:
            unique_visitors = unique_visitors.filter(Visit.link_slug == link_slug)
        if start_date:
            unique_visitors = unique_visitors.filter(Visit.timestamp >= start_date)
        if end_date:
            unique_visitors = unique_visitors.filter(Visit.timestamp <= end_date)
        
        unique_visitors = unique_visitors.scalar() or 0
        
        # Bot visits
        bot_visits = query.filter(Visit.is_bot == True).count()
        
        return {
            'total_visits': total_visits,
            'unique_visitors': unique_visitors,
            'bot_visits': bot_visits,
            'link_slug': link_slug,
            'start_date': start_date.isoformat() if start_date else None,
            'end_date': end_date.isoformat() if end_date else None
        }
        
    except Exception as e:
        logger.error(f"Error getting visit stats: {e}", exc_info=True)
        return {
            'total_visits': 0,
            'unique_visitors': 0,
            'bot_visits': 0,
            'error': 'Failed to retrieve statistics'
        }


def get_visits_by_hour(
    link_slug: Optional[str] = None,
    days: int = 7
) -> List[Dict[str, Any]]:
    """
    Get visit distribution by hour of day.
    
    Args:
        link_slug: Optional filter by link slug
        days: Number of days to look back
        
    Returns:
        List of dictionaries with hour and visit count
    """
    try:
        start_date = datetime.utcnow() - timedelta(days=days)
        
        query = db.session.query(
            extract('hour', Visit.timestamp).label('hour'),
            func.count(Visit.id).label('visits')
        ).filter(
            Visit.timestamp >= start_date,
            Visit.is_bot == False
        )
        
        if link_slug:
            query = query.filter(Visit.link_slug == link_slug)
        
        query = query.group_by('hour').order_by('hour')
        
        results = query.all()
        
        # Fill in missing hours with 0
        hour_data = {int(r.hour): r.visits for r in results}
        return [
            {'hour': hour, 'visits': hour_data.get(hour, 0)}
            for hour in range(24)
        ]
        
    except Exception as e:
        logger.error(f"Error getting visits by hour: {e}")
        return []


def get_visits_by_day(
    link_slug: Optional[str] = None,
    days: int = 30
) -> List[Dict[str, Any]]:
    """
    Get visit distribution by day.
    
    Args:
        link_slug: Optional filter by link slug
        days: Number of days to look back
        
    Returns:
        List of dictionaries with date and visit count
    """
    try:
        start_date = datetime.utcnow() - timedelta(days=days)
        
        query = db.session.query(
            func.date(Visit.timestamp).label('date'),
            func.count(Visit.id).label('visits')
        ).filter(
            Visit.timestamp >= start_date,
            Visit.is_bot == False
        )
        
        if link_slug:
            query = query.filter(Visit.link_slug == link_slug)
        
        query = query.group_by('date').order_by('date')
        
        results = query.all()
        
        return [
            {'date': str(r.date), 'visits': r.visits}
            for r in results
        ]
        
    except Exception as e:
        logger.error(f"Error getting visits by day: {e}")
        return []


def get_top_referrers(
    link_slug: Optional[str] = None,
    days: int = 30,
    limit: int = 10
) -> List[Dict[str, Any]]:
    """
    Get top referrers.
    
    Args:
        link_slug: Optional filter by link slug
        days: Number of days to look back
        limit: Maximum number of results
        
    Returns:
        List of dictionaries with referrer and count
    """
    try:
        start_date = datetime.utcnow() - timedelta(days=days)
        
        query = db.session.query(
            Visit.referrer,
            func.count(Visit.id).label('visits')
        ).filter(
            Visit.timestamp >= start_date,
            Visit.is_bot == False,
            Visit.referrer.isnot(None),
            Visit.referrer != ''
        )
        
        if link_slug:
            query = query.filter(Visit.link_slug == link_slug)
        
        query = query.group_by(Visit.referrer).order_by(func.count(Visit.id).desc()).limit(limit)
        
        results = query.all()
        
        return [
            {'referrer': r.referrer, 'visits': r.visits}
            for r in results
        ]
        
    except Exception as e:
        logger.error(f"Error getting top referrers: {e}")
        return []


def get_utm_stats(
    link_slug: Optional[str] = None,
    days: int = 30
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Get UTM parameter statistics.
    
    Args:
        link_slug: Optional filter by link slug
        days: Number of days to look back
        
    Returns:
        Dictionary with sources, mediums, and campaigns
    """
    try:
        start_date = datetime.utcnow() - timedelta(days=days)
        
        base_filter = [
            Visit.timestamp >= start_date,
            Visit.is_bot == False
        ]
        
        if link_slug:
            base_filter.append(Visit.link_slug == link_slug)
        
        # UTM sources
        sources_query = db.session.query(
            Visit.utm_source,
            func.count(Visit.id).label('visits')
        ).filter(
            *base_filter,
            Visit.utm_source.isnot(None),
            Visit.utm_source != ''
        ).group_by(Visit.utm_source).order_by(func.count(Visit.id).desc()).limit(10)
        
        # UTM mediums
        mediums_query = db.session.query(
            Visit.utm_medium,
            func.count(Visit.id).label('visits')
        ).filter(
            *base_filter,
            Visit.utm_medium.isnot(None),
            Visit.utm_medium != ''
        ).group_by(Visit.utm_medium).order_by(func.count(Visit.id).desc()).limit(10)
        
        # UTM campaigns
        campaigns_query = db.session.query(
            Visit.utm_campaign,
            func.count(Visit.id).label('visits')
        ).filter(
            *base_filter,
            Visit.utm_campaign.isnot(None),
            Visit.utm_campaign != ''
        ).group_by(Visit.utm_campaign).order_by(func.count(Visit.id).desc()).limit(10)
        
        return {
            'sources': [{'name': r.utm_source, 'visits': r.visits} for r in sources_query.all()],
            'mediums': [{'name': r.utm_medium, 'visits': r.visits} for r in mediums_query.all()],
            'campaigns': [{'name': r.utm_campaign, 'visits': r.visits} for r in campaigns_query.all()]
        }
        
    except Exception as e:
        logger.error(f"Error getting UTM stats: {e}")
        return {'sources': [], 'mediums': [], 'campaigns': []}


def get_device_browser_stats(
    link_slug: Optional[str] = None,
    days: int = 30
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Get device and browser statistics.
    
    Args:
        link_slug: Optional filter by link slug
        days: Number of days to look back
        
    Returns:
        Dictionary with devices and browsers
    """
    try:
        start_date = datetime.utcnow() - timedelta(days=days)
        
        base_filter = [
            Visit.timestamp >= start_date,
            Visit.is_bot == False
        ]
        
        if link_slug:
            base_filter.append(Visit.link_slug == link_slug)
        
        # Device types
        devices_query = db.session.query(
            Visit.device_type,
            func.count(Visit.id).label('visits')
        ).filter(*base_filter).group_by(Visit.device_type).order_by(func.count(Visit.id).desc())
        
        # Browsers
        browsers_query = db.session.query(
            Visit.browser,
            func.count(Visit.id).label('visits')
        ).filter(*base_filter).group_by(Visit.browser).order_by(func.count(Visit.id).desc()).limit(10)
        
        return {
            'devices': [{'name': r.device_type, 'visits': r.visits} for r in devices_query.all()],
            'browsers': [{'name': r.browser, 'visits': r.visits} for r in browsers_query.all()]
        }
        
    except Exception as e:
        logger.error(f"Error getting device/browser stats: {e}")
        return {'devices': [], 'browsers': []}
