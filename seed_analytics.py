"""
Seed script to generate sample analytics data for demonstration.
Run this script to populate the database with test visit data.
"""
import random
from datetime import datetime, timedelta
from app import create_app, db
from app.models import Visit
from app.utils.analytics import generate_visitor_id


def seed_analytics_data(link_slug='demo-report', days=30, visits_per_day=50):
    """
    Generate sample analytics data.
    
    Args:
        link_slug: The link slug to generate data for
        days: Number of days to generate data for
        visits_per_day: Average number of visits per day
    """
    app = create_app()
    
    with app.app_context():
        print(f"Generating sample analytics data for '{link_slug}'...")
        print(f"Time range: {days} days, ~{visits_per_day} visits/day")
        
        # Clear existing data for this slug
        Visit.query.filter_by(link_slug=link_slug).delete()
        db.session.commit()
        
        # Sample data sources
        referrers = [
            'https://google.com',
            'https://facebook.com',
            'https://twitter.com',
            'https://linkedin.com',
            'https://reddit.com',
            '',  # Direct traffic
        ]
        
        utm_sources = ['google', 'facebook', 'email', 'newsletter', 'twitter', None]
        utm_mediums = ['cpc', 'social', 'email', 'organic', None]
        utm_campaigns = ['spring-2024', 'product-launch', 'newsletter-q4', None]
        
        devices = ['pc', 'mobile', 'tablet']
        browsers = [
            'Chrome 119.0',
            'Safari 17.0',
            'Firefox 120.0',
            'Edge 119.0',
            'Mobile Safari 17.0',
            'Chrome Mobile 119.0'
        ]
        os_list = [
            'Windows 10',
            'macOS 14.0',
            'iOS 17.0',
            'Android 13',
            'Linux'
        ]
        
        # Generate unique visitors
        num_unique_visitors = int(visits_per_day * days * 0.3)  # 30% unique
        visitor_ids = [generate_visitor_id() for _ in range(num_unique_visitors)]
        
        now = datetime.utcnow()
        visits_created = 0
        
        for day in range(days):
            # Vary visits per day (80-120% of average)
            daily_visits = int(visits_per_day * random.uniform(0.8, 1.2))
            
            for _ in range(daily_visits):
                # Random time during the day (weighted toward business hours)
                hour = random.choices(
                    range(24),
                    weights=[2, 1, 1, 1, 2, 3, 5, 8, 10, 12, 12, 11, 10, 11, 12, 11, 10, 8, 6, 4, 3, 3, 2, 2]
                )[0]
                minute = random.randint(0, 59)
                second = random.randint(0, 59)
                
                visit_time = now - timedelta(
                    days=day,
                    hours=23-hour,
                    minutes=59-minute,
                    seconds=59-second
                )
                
                # Randomly decide if this is a bot (10% chance)
                is_bot = random.random() < 0.1
                
                # Select random visitor (some return visitors)
                visitor_id = random.choice(visitor_ids) if not is_bot else None
                
                # Create visit
                visit = Visit(
                    link_slug=link_slug,
                    timestamp=visit_time,
                    visitor_id=visitor_id,
                    ip_hash=f"hash_{random.randint(1000, 9999)}",
                    user_agent=f"Mozilla/5.0 ({random.choice(os_list)})",
                    referrer=random.choice(referrers),
                    utm_source=random.choice(utm_sources),
                    utm_medium=random.choice(utm_mediums),
                    utm_campaign=random.choice(utm_campaigns),
                    device_type=random.choice(devices),
                    browser=random.choice(browsers),
                    os=random.choice(os_list),
                    is_bot=is_bot,
                    session_duration=random.randint(10, 600) if not is_bot else None
                )
                
                db.session.add(visit)
                visits_created += 1
                
                # Commit in batches for performance
                if visits_created % 100 == 0:
                    db.session.commit()
                    print(f"  Created {visits_created} visits...")
        
        # Final commit
        db.session.commit()
        
        print(f"\n✓ Successfully created {visits_created} sample visits!")
        print(f"  Unique visitors: {num_unique_visitors}")
        print(f"  Link slug: {link_slug}")
        print(f"\nYou can now view the analytics at: /analytics/dashboard?link_slug={link_slug}")


if __name__ == '__main__':
    import sys
    
    # Parse command line arguments
    link_slug = sys.argv[1] if len(sys.argv) > 1 else 'demo-report'
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    visits_per_day = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    
    print("=" * 60)
    print("Analytics Data Seed Script")
    print("=" * 60)
    
    seed_analytics_data(link_slug, days, visits_per_day)
    
    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)
