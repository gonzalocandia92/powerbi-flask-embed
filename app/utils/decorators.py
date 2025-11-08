"""
Utility decorators for the Power BI Flask Embed application.
"""
import time
import logging
from functools import wraps
from sqlalchemy.exc import OperationalError, DBAPIError

from app import db


def retry_on_db_error(max_retries=3, delay=1):
    """
    Decorator to retry database operations on connection errors.
    
    This decorator handles transient database connection failures by automatically
    retrying the operation with exponential backoff.
    
    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        delay: Base delay in seconds between retries (default: 1)
    
    Returns:
        Decorated function that retries on database errors
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (OperationalError, DBAPIError) as e:
                    last_exception = e
                    logging.warning(
                        f"Database connection error (attempt {attempt + 1}/{max_retries}): {e}"
                    )
                    
                    db.session.rollback()
                    db.session.remove()
                    
                    if attempt < max_retries - 1:
                        time.sleep(delay * (attempt + 1))
                    else:
                        logging.error(
                            f"Database connection error after {max_retries} attempts: {e}"
                        )
                        raise last_exception
            
            return None
        
        return wrapper
    
    return decorator
