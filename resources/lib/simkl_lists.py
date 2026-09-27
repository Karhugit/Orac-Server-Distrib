# -*- coding: utf-8 -*-
"""
simkl_lists.py
--------------
Configuration for Simkl Generic Lists (Trending and Popular).
Endpoints are Cloudflare edge-cached static JSON feeds hosted on data.simkl.in.
"""

SIMKL_GENERIC_LISTS = [
    {
        "slug": "trending-movies-today",
        "name": "Simkl Trending Movies (Today)",
        "type": "movie",
        "endpoint": "discover/trending/movies/today_100.json",
        "description": "Currently trending movies on Simkl today."
    },
    {
        "slug": "trending-shows-today",
        "name": "Simkl Trending Shows (Today)",
        "type": "show",
        "endpoint": "discover/trending/tv/today_100.json",
        "description": "Currently trending TV shows on Simkl today."
    },
    {
        "slug": "popular-movies-weekly",
        "name": "Simkl Popular Movies (This Week)",
        "type": "movie",
        "endpoint": "discover/trending/movies/week_100.json",
        "description": "Most watched movies this week on Simkl."
    },
    {
        "slug": "popular-shows-weekly",
        "name": "Simkl Popular Shows (This Week)",
        "type": "show",
        "endpoint": "discover/trending/tv/week_100.json",
        "description": "Most watched TV shows this week on Simkl."
    },
    {
        "slug": "trending-anime-today",
        "name": "Simkl Trending Anime (Today)",
        "type": "show",
        "endpoint": "discover/trending/anime/today_100.json",
        "description": "Currently trending anime on Simkl today."
    }
]


def get_simkl_generic_list(slug: str):
    for item in SIMKL_GENERIC_LISTS:
        if item["slug"] == slug:
            return item
    return None
