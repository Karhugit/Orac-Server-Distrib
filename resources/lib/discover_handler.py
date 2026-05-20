from datetime import datetime, timedelta
from .log_utils import log, LOGINFO, LOGERROR
from resources.lib.date_utils import parse_date_param
import json
from .db_utils import get_discover_params_from_db



def handle_discover_request(item_type, query_params, tmdb_handler, ext_indexes_cursor):
    """
    Handles a discover request by forwarding it to TMDb and formatting the response.
    """
    log(f"Discover handler received query_params: {query_params}", LOGINFO)
    if item_type not in ['tv', 'movie', 'tvshow']:
        return 400, f"Unsupported item_type for discover: {item_type}", "text/plain"

    # TMDb API uses 'tv' for tv shows
    if item_type == 'tvshow':
        item_type = 'tv'

    processed_params = {}
    
    # First, try to get a query name from a 'name' parameter
    query_name = query_params.get('name', [None])[0]
    
    # If not found, and there's only one param, use its key as the query name
    if not query_name and len(query_params) == 1:
        query_name = list(query_params.keys())[0]

    log(f"Extracted query_name: '{query_name}'", LOGINFO)

    db_params = None
    if query_name:
        db_params = get_discover_params_from_db(ext_indexes_cursor, query_name)

    if db_params:
        processed_params.update(db_params)
        log(f"Using stored discover query '{query_name}' with params: {processed_params}", LOGINFO)
        # Date parsing for params from DB
        for key, value in processed_params.items():
            if '.gte' in key or '.lte' in key:
                parsed_date = parse_date_param(value)
                if parsed_date:
                    processed_params[key] = parsed_date
    else:
        # Whitelist of valid TMDb discover parameters to avoid sending invalid ones.
        valid_tmdb_params = [
            'sort_by', 'with_genres', 'primary_release_date.gte', 'primary_release_date.lte',
            'air_date.gte', 'air_date.lte', 'first_air_date.gte', 'first_air_date.lte',
            'vote_average.gte', 'vote_average.lte', 'with_keywords', 'with_original_language', 'with_networks',
            'without_genres', 'with_status'
        ]
        for key, value in query_params.items():
            if key in valid_tmdb_params:
                parsed_date = parse_date_param(value)
                if parsed_date:
                    processed_params[key] = parsed_date
            else:
                # This will log any parameters that are not being passed to TMDb.
                log(f"Skipping unknown or invalid discover parameter: {key}", LOGINFO)

    # Set default language to en-US if with_original_language is not specified
    if 'language' not in processed_params and 'language' not in query_params:
        processed_params['language'] = 'en-US'

    log(f"Discovering '{item_type}' with params: {processed_params}", LOGINFO)

    try:
        all_results = []
        page = 1
        total_pages = 1  # Initialize to 1 to ensure the loop runs at least once

        while len(all_results) < 100:
            paged_params = processed_params.copy()
            paged_params['page'] = page
            
            data = tmdb_handler.discover_media(item_type, paged_params)
            
            if not data:
                log(f"Failed to fetch data from TMDb for page {page}", LOGERROR)
                break

            results = data.get('results', [])
            if not results:
                break  # No more results, so we stop.

            all_results.extend(results)
            
            # On the first page, find out total pages.
            if page == 1:
                total_pages = data.get('total_pages', 1)

            # If we are on the last page, stop.
            if page >= total_pages:
                break

            page += 1

        # Trim to a maximum of 100 items
        final_results = all_results[:100]
        
        # Format the results to be consistent with other handlers
        formatted_results = []
        if item_type == 'tv':
            for show in final_results:
                first_aired = show.get('first_air_date', '')
                year = int(first_aired.split('-')[0]) if first_aired else None
                formatted_results.append({
                    'tmdb_id': show.get('id'),
                    'title': show.get('name'),
                    'original_title': show.get('original_name'),
                    'year': year,
                    'premiered': first_aired,
                    'overview': show.get('overview'),
                    'poster_path': tmdb_handler._build_url(show.get('poster_path'), 'w500'),
                    'fanart_path': tmdb_handler._build_url(show.get('backdrop_path'), 'w780'),
                    'rating': show.get('vote_average'),
                    'votes': show.get('vote_count')
                })
        elif item_type == 'movie':
            for movie in final_results:
                release_date = movie.get('release_date', '')
                year = int(release_date.split('-')[0]) if release_date else None
                formatted_results.append({
                    'tmdb_id': movie.get('id'),
                    'title': movie.get('title'),
                    'original_title': movie.get('original_title'),
                    'year': year,
                    'premiered': release_date,
                    'overview': movie.get('overview'),
                    'poster_path': tmdb_handler._build_url(movie.get('poster_path'), 'w500'),
                    'fanart_path': tmdb_handler._build_url(movie.get('backdrop_path'), 'w780'),
                    'rating': movie.get('vote_average'),
                    'votes': movie.get('vote_count')
                })
        
        return 200, json.dumps(formatted_results), "application/json"

    except Exception as e:
        log(f"Error handling discover request: {e}", LOGERROR)
        return 500, "Internal server error during discover", "text/plain"