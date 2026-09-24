# -*- coding: utf-8 -*-
"""
punchplay_lists.py
------------------
Configuration for PunchPlay Generic Lists (Trending Movies, Shows, Anime).
Items are fetched from the PunchPlay public catalog API (no authentication required).
Endpoint: GET https://punchplay.tv/api/public/v1/catalog/trending?type=movie|show|anime
"""

PUNCHPLAY_GENERIC_LISTS = [
    {
        "slug": "trending-movies",
        "name": "PunchPlay Trending Movies",
        "type": "movie",
        "api_type": "movie",
        "description": "Currently trending movies on PunchPlay."
    },
    {
        "slug": "trending-shows",
        "name": "PunchPlay Trending Shows",
        "type": "show",
        "api_type": "show",
        "description": "Currently trending TV shows on PunchPlay."
    },
    {
        "slug": "trending-anime",
        "name": "PunchPlay Trending Anime",
        "type": "show",
        "api_type": "anime",
        "description": "Currently trending anime on PunchPlay."
    },
]


def get_punchplay_generic_list(slug: str):
    """Returns the generic list definition for the given slug, or None."""
    for item in PUNCHPLAY_GENERIC_LISTS:
        if item["slug"] == slug:
            return item
    return None
