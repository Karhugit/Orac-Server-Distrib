"""
Centralized configuration for Trakt generic lists.

This module contains metadata for all Trakt generic lists (trending, popular, etc.)
to avoid duplication across sync and handler modules.
"""

# Configuration for Trakt generic lists
# Each entry contains:
# - slug: The unique identifier for the list
# - endpoint: The Trakt API endpoint to fetch items
# - name: Display name for the list
# - description: Description of the list
# - media_type: Type of media (show, movie, or mixed)
TRAKT_GENERIC_LISTS = {
    "shows-trending": {
        "endpoint": "/shows/trending?limit=100&extended=full",
        "name": "Trending Shows",
        "description": "Currently trending TV shows on Trakt.",
        "media_type": "show"
    },
    "shows-most-played-weekly": {
        "endpoint": "/shows/played/weekly?limit=100&extended=full",
        "name": "Most Played Shows (Weekly)",
        "description": "Most played TV shows in the last week on Trakt.",
        "media_type": "show"
    },
    "shows-most-watched-weekly": {
        "endpoint": "/shows/watched/weekly?limit=100&extended=full",
        "name": "Most Watched Shows (Weekly)",
        "description": "Most watched TV shows in the last week on Trakt.",
        "media_type": "show"
    },
    "show-recommendations": {
        "endpoint": "/recommendations/shows?limit=100&extended=full&ignore_collected=true&ignore_watchlisted=true",
        "name": "Show Recommendations",
        "description": "Recommended TV shows based on your watch history on Trakt.",
        "media_type": "show",
        "requires_auth": True
    },
    "movies-trending": {
        "endpoint": "/movies/trending?limit=100&extended=full",
        "name": "Trending Movies",
        "description": "Currently trending movies on Trakt.",
        "media_type": "movie"
    },
    "movies-recommendations": {
        "endpoint": "/recommendations/movies?limit=100&extended=full&ignore_collected=true&ignore_watchlisted=true",
        "name": "Movie Recommendations",
        "description": "Recommended movies based on your watch history on Trakt.",
        "media_type": "movie",
        "requires_auth": True
    },
    "movies-boxoffice": {
        "endpoint": "/movies/boxoffice?limit=100&extended=full",
        "name": "Box Office Movies",
        "description": "Currently box office movies on Trakt.",
        "media_type": "movie"
    },
    "movies-most-watched-weekly": {
        "endpoint": "/movies/watched/weekly?limit=100&extended=full",
        "name": "Most Watched Movies (Weekly)",
        "description": "Most watched movies in the last week on Trakt.",
        "media_type": "movie"
    },
    "movies-most-favourited-weekly": {
        "endpoint": "/movies/favorited/weekly?limit=100&extended=full",
        "name": "Most Favourited Movies (Weekly)",
        "description": "Most favourited movies in the last week on Trakt.",
        "media_type": "movie"
    },
    "movies-most-played-weekly": {
        "endpoint": "/movies/played/weekly?limit=100&extended=full",
        "name": "Most Played Movies (Weekly)",
        "description": "Most played movies in the last week on Trakt.",
        "media_type": "movie"   
    },
    "shows-most-favourited-weekly": {
        "endpoint": "/shows/favorited/weekly?limit=100&extended=full",
        "name": "Most Favourited Shows (Weekly)",
        "description": "Most favourited shows in the last week on Trakt.",
        "media_type": "show"   
    },
    "shows-most-collected-weekly": {
        "endpoint": "/shows/collected/weekly?limit=100&extended=full",
        "name": "Most Collected Shows (Weekly)",
        "description": "Most collected TV shows in the last week on Trakt.",
        "media_type": "show"
    },
}


def get_list_config(slug):
    """
    Get the configuration for a specific Trakt generic list.
    
    Args:
        slug: The slug identifier for the list
        
    Returns:
        Dictionary with list configuration, or None if not found
    """
    return TRAKT_GENERIC_LISTS.get(slug)


def get_all_slugs():
    """
    Get all available Trakt generic list slugs.
    
    Returns:
        List of slug strings
    """
    return list(TRAKT_GENERIC_LISTS.keys())


def is_generic_list(slug):
    """
    Check if a slug corresponds to a Trakt generic list.
    
    Args:
        slug: The slug to check
        
    Returns:
        True if the slug is a generic list, False otherwise
    """
    return slug in TRAKT_GENERIC_LISTS
