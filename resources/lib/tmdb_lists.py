# -*- coding: utf-8 -*-
# Configuration for TMDB Generic Lists

TMDB_GENERIC_LISTS = [
    {
        "slug": "trending-movies",
        "name": "Trending Movies",
        "type": "movie",
        "api_method": "get_trending_movies",
        "description": "TMDB Trending Movies"
    },
    {
        "slug": "trending-tv",
        "name": "Trending TV",
        "type": "show",
        "api_method": "get_trending_shows",
        "description": "TMDB Trending TV Shows"
    }
]
