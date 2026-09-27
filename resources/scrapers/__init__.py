# scrapers/__init__.py

"""
Scrapers package for the HTTP service
Provides async scraper implementations for various torrent sites
"""

# Import base classes to make them available at package level
from .base_scraper import AsyncScraperBase

# Import scraper manager
from .scraper_manager import ScraperManager

# Optionally import specific scrapers for direct access
try:
    from .limetorrents import LimeTorrentsService
except ImportError:
    LimeTorrentsService = None

# Package metadata
__version__ = "1.0.0"
__author__ = "Your Name"

# List of available scrapers (populated dynamically)
AVAILABLE_SCRAPERS = []

def get_available_scrapers():
    """Get list of available scraper names"""
    manager = ScraperManager()
    return list(manager.scrapers.keys())

# Convenience function for creating scraper manager
def create_scraper_manager():
    """Create and return a configured scraper manager"""
    return ScraperManager()

# Export main classes and functions
__all__ = [
    'AsyncScraperBase',
    'ScraperManager', 
    'LimeTorrentsService',
    'get_available_scrapers',
    'create_scraper_manager'
]