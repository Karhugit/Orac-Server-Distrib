from resources.lib.formatting_utils import format_movie

tvshow_genres = [
    {'id': 10759, 'name': 'Action & Adventure'}, {'id': 16, 'name': 'Animation'}, {'id': 35, 'name': 'Comedy'},
    {'id': 80, 'name': 'Crime'}, {'id': 99, 'name': 'Documentary'}, {'id': 18, 'name': 'Drama'},
    {'id': 10751, 'name': 'Family'}, {'id': 10762, 'name': 'Kids'}, {'id': 9648, 'name': 'Mystery'},
    {'id': 10763, 'name': 'News'}, {'id': 10764, 'name': 'Reality'}, {'id': 10765, 'name': 'Sci-Fi & Fantasy'},
    {'id': 10766, 'name': 'Soap'}, {'id': 10767, 'name': 'Talk'}, {'id': 10768, 'name': 'War & Politics'},
    {'id': 37, 'name': 'Western'}
]

# Create lookup dictionaries for faster access
TV_GENRE_MAP = {genre['id']: genre['name'] for genre in tvshow_genres}


def search_tmdb(query, tmdb_handler, item_type='movie'):
    results = tmdb_handler.search(item_type, query)
# Check the total pages and read the next pages if available
    total_pages = results.get('total_pages', 1)
    if total_pages > 1:
        for page in range(2, min(total_pages + 1, 6)):  # Limit to first 5 pages
            paged_results = tmdb_handler.search(item_type, query, page=page)
            if paged_results and 'results' in paged_results:
                results['results'].extend(paged_results['results'])
            else:
                break
    results = results.get('results', [])[:100]  # Limit to first 100 results

    # Sort results to improve relevance.
    # We prioritize exact title matches, then sort by popularity and vote count.
    def sort_key(item):
        title = (item.get("title") or item.get("name", "")).lower()
        query_lower = query.lower()
        
        # Primary sort key: give a huge boost to exact matches.
        exact_match_bonus = 0 if title == query_lower else 1
        
        # Secondary sort keys: popularity and vote count (descending).
        return (exact_match_bonus, -item.get("popularity", 0), -item.get("vote_count", 0))

    results.sort(key=sort_key)

    formatted_results = []
    for item in results:
        media_type = item.get("media_type", item_type)
        if media_type == 'tv':
            # TV shows still use localized genre mapping for now (can also be unified later)
            genre_ids = item.get("genre_ids", [])
            genre_names = [TV_GENRE_MAP.get(gid) for gid in genre_ids if TV_GENRE_MAP.get(gid)]
            formatted_item = {
                "id": item.get("id"),
                "title": item.get("title") or item.get("name"),
                "overview": item.get("overview"),
                "release_date": item.get("release_date") or item.get("first_air_date"),
                "poster_path": tmdb_handler._build_url(item.get("poster_path"), "w500"),
                "backdrop_path": tmdb_handler._build_url(item.get("backdrop_path"), "w780"),
                "media_type": media_type,
                "genres": genre_names,
                "origin_country": item.get("origin_country", []),
                "original_language": item.get("original_language"),
                "original_title": item.get("original_title") or item.get("original_name"),
                "popularity": item.get("popularity"),
                "first_air_date": item.get("first_air_date"),
                "vote_average": item.get("vote_average"),
                "vote_count": item.get("vote_count")
            }
        else: 
            # Use unified movie formatter for 'movie'
            formatted_item = format_movie(item, tmdb_handler, media_type=media_type)

        formatted_results.append(formatted_item)

    return formatted_results