import json
import inspect
from datetime import datetime, timezone
from resources.lib.log_utils import log, LOGINFO, LOGERROR, LOGWARNING
from resources.lib.date_utils import parse_date_param
from resources.lib.db_utils import get_discover_params_from_db

TRAKT_CALENDAR_PRESETS = {
    'new_shows': {
        'path': '/calendars/{target}/shows/new/{start_date}/{days}',
        'has_target': True,
        'default_media_type': 'tv',
        'label': 'New Shows'
    },
    'season_premieres': {
        'path': '/calendars/{target}/shows/premieres/{start_date}/{days}',
        'has_target': True,
        'default_media_type': 'tv',
        'label': 'Season Premieres'
    },
    'finales': {
        'path': '/calendars/{target}/shows/finales/{start_date}/{days}',
        'has_target': True,
        'default_media_type': 'tv',
        'label': 'Finales'
    },
    'movies': {
        'path': '/calendars/{target}/movies/{start_date}/{days}',
        'has_target': True,
        'default_media_type': 'movie',
        'label': 'Movies'
    },
    'streaming_releases': {
        'path': '/calendars/{target}/streaming/{start_date}/{days}',
        'has_target': True,
        'default_media_type': 'movie',
        'label': 'Streaming Releases'
    },
    'dvd_releases': {
        'path': '/calendars/{target}/dvd/{start_date}/{days}',
        'has_target': True,
        'default_media_type': 'movie',
        'label': 'DVD Releases'
    },
    'media': {
        'path': '/calendars/{target}/media/{start_date}/{days}',
        'has_target': True,
        'default_media_type': 'media',
        'label': 'Media'
    },
    'hot_releases': {
        'path': '/calendars/releases/hot/{start_date}/{days}',
        'has_target': False,
        'default_media_type': 'media',
        'label': 'Hot Releases'
    },
    'hot_premieres': {
        'path': '/calendars/releases/hot/premieres/{start_date}/{days}',
        'has_target': False,
        'default_media_type': 'tv',
        'label': 'Hot Premieres'
    },
    'hot_finales': {
        'path': '/calendars/releases/hot/finales/{start_date}/{days}',
        'has_target': False,
        'default_media_type': 'tv',
        'label': 'Hot Finales'
    },
    'hot_new_shows': {
        'path': '/calendars/releases/hot/new/{start_date}/{days}',
        'has_target': False,
        'default_media_type': 'tv',
        'label': 'Hot New Shows'
    }
}

# Cache for Trakt genres
_trakt_genres_cache = None


def _fetch_from_trakt(trakt_handler, endpoint, params=None):
    """Safely invokes TraktAuth whether using sync _get or async get."""
    if hasattr(trakt_handler, '_get') and callable(trakt_handler._get):
        return trakt_handler._get(endpoint, params=params)
    if hasattr(trakt_handler, 'get') and callable(trakt_handler.get):
        res = trakt_handler.get(endpoint, params=params)
        if inspect.isawaitable(res):
            import asyncio
            try:
                return asyncio.run(res)
            except RuntimeError:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    return executor.submit(asyncio.run, res).result()
        return res
    return None


def normalize_preset_key(key):
    """Normalize human-readable preset names or keys to canonical preset key."""
    if not key:
        return 'hot_releases'
    k = str(key).strip().lower().replace('-', '_').replace(' ', '_')
    if k in TRAKT_CALENDAR_PRESETS:
        return k
    # Map common variations
    variations = {
        'new': 'new_shows',
        'premieres': 'season_premieres',
        'streaming': 'streaming_releases',
        'dvd': 'dvd_releases',
        'hot': 'hot_releases',
    }
    return variations.get(k, k)


def convert_rating_to_trakt_scale(rating_val):
    """
    Converts 0-10 user input to Trakt 0-100 scale range.
    Examples:
      7.5 -> '75-100'
      7 -> '70-100'
      70-90 -> '70-90'
    """
    if not rating_val:
        return None
    val_str = str(rating_val).strip()
    if '-' in val_str:
        # Already a range
        return val_str
    try:
        f = float(val_str)
        if f <= 10.0:
            min_score = int(round(f * 10))
            return f"{min_score}-100"
        else:
            return f"{int(round(f))}-100"
    except ValueError:
        return val_str


def get_trakt_genres(trakt_handler):
    """
    Fetches and caches combined movie & TV show genres from Trakt.
    Returns list of dicts: [{'name': 'Action', 'slug': 'action'}, ...]
    """
    global _trakt_genres_cache
    if _trakt_genres_cache is not None:
        return _trakt_genres_cache

    genres_by_slug = {}
    try:
        # Fetch movie genres
        m_resp = _fetch_from_trakt(trakt_handler, "/genres/movies")
        if m_resp and m_resp.status_code == 200:
            for g in m_resp.json():
                if isinstance(g, dict) and 'slug' in g and 'name' in g:
                    genres_by_slug[g['slug']] = g['name']

        # Fetch show genres
        s_resp = _fetch_from_trakt(trakt_handler, "/genres/shows")
        if s_resp and s_resp.status_code == 200:
            for g in s_resp.json():
                if isinstance(g, dict) and 'slug' in g and 'name' in g:
                    genres_by_slug[g['slug']] = g['name']

        sorted_genres = [
            {'name': name, 'slug': slug}
            for slug, name in sorted(genres_by_slug.items(), key=lambda x: x[1].lower())
        ]
        if sorted_genres:
            _trakt_genres_cache = sorted_genres
            return sorted_genres
    except Exception as e:
        log(f"[TraktCalendar] Error fetching Trakt genres: {e}", level=LOGWARNING)

    # Fallback default genre set if offline
    fallback = [
        {'name': 'Action', 'slug': 'action'},
        {'name': 'Adventure', 'slug': 'adventure'},
        {'name': 'Animation', 'slug': 'animation'},
        {'name': 'Comedy', 'slug': 'comedy'},
        {'name': 'Crime', 'slug': 'crime'},
        {'name': 'Documentary', 'slug': 'documentary'},
        {'name': 'Drama', 'slug': 'drama'},
        {'name': 'Family', 'slug': 'family'},
        {'name': 'Fantasy', 'slug': 'fantasy'},
        {'name': 'History', 'slug': 'history'},
        {'name': 'Horror', 'slug': 'horror'},
        {'name': 'Music', 'slug': 'music'},
        {'name': 'Mystery', 'slug': 'mystery'},
        {'name': 'Romance', 'slug': 'romance'},
        {'name': 'Science Fiction', 'slug': 'science-fiction'},
        {'name': 'Thriller', 'slug': 'thriller'},
        {'name': 'War', 'slug': 'war'},
        {'name': 'Western', 'slug': 'western'}
    ]
    return fallback


def handle_trakt_calendar_request(query_params, trakt_handler, ext_indexes_cursor=None, target_media_type=None):
    """
    Handles a Trakt Calendar discover request and returns normalized media results.
    If target_media_type is provided ('tv' or 'movie'), items are filtered to match.
    """
    log(f"[TraktCalendar] Received query params: {query_params}", level=LOGINFO)

    processed_params = {}

    # Extract query_name if present
    query_name = None
    if isinstance(query_params, dict):
        if 'name' in query_params:
            val = query_params['name']
            query_name = val[0] if isinstance(val, list) else val
        elif len(query_params) == 1 and 'preset' not in query_params and 'start_date' not in query_params:
            query_name = list(query_params.keys())[0]

    # Look up saved index parameters if query_name provided
    if query_name and ext_indexes_cursor:
        db_params = get_discover_params_from_db(ext_indexes_cursor, query_name, 'media')
        if not db_params:
            db_params = get_discover_params_from_db(ext_indexes_cursor, query_name, 'tv')
        if not db_params:
            db_params = get_discover_params_from_db(ext_indexes_cursor, query_name, 'movie')
        if not db_params:
            db_params = get_discover_params_from_db(ext_indexes_cursor, query_name, None)
        if db_params:
            log(f"[TraktCalendar] Loaded stored index '{query_name}': {db_params}", level=LOGINFO)
            processed_params.update(db_params)

    # Merge incoming query parameters (unpacking 1-element lists if needed)
    for k, v in query_params.items():
        if k == 'name':
            continue
        processed_params[k] = v[0] if isinstance(v, list) and len(v) == 1 else v

    # Resolve preset
    raw_preset = processed_params.get('preset') or processed_params.get('calendar') or 'hot_releases'
    preset_key = normalize_preset_key(raw_preset)
    if preset_key not in TRAKT_CALENDAR_PRESETS:
        preset_key = 'hot_releases'

    preset_def = TRAKT_CALENDAR_PRESETS[preset_key]

    # Resolve target ('all' or 'my')
    target = str(processed_params.get('target', 'all')).strip().lower()
    if target not in ('all', 'my'):
        target = 'all'

    if target == 'my' and preset_def['has_target']:
        if not trakt_handler.is_authenticated():
            return 401, json.dumps({'error': "Authentication required for 'My Calendar'. Please link Trakt in Services tab."}), "application/json"

    # Resolve start_date
    raw_start = processed_params.get('start_date')
    if raw_start:
        parsed_start = parse_date_param(str(raw_start))
        start_date = parsed_start if parsed_start else raw_start
    else:
        start_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Resolve days (Trakt maximum is 33 days)
    try:
        days = int(processed_params.get('days', 7))
        days = max(1, min(days, 33))
    except (ValueError, TypeError):
        days = 7

    # Build endpoint path
    if preset_def['has_target']:
        endpoint = preset_def['path'].format(target=target, start_date=start_date, days=days)
    else:
        endpoint = preset_def['path'].format(start_date=start_date, days=days)

    # Build Trakt query parameters
    trakt_query = {
        'extended': 'full,images'
    }

    # Genres (comma-separated slugs)
    raw_genres = processed_params.get('genres') or processed_params.get('with_genres')
    if raw_genres:
        if isinstance(raw_genres, list):
            trakt_query['genres'] = ','.join(str(g).strip() for g in raw_genres if g)
        else:
            trakt_query['genres'] = str(raw_genres).replace('|', ',').strip()

    # Ratings
    raw_rating = processed_params.get('ratings') or processed_params.get('vote_average_gte') or processed_params.get('vote_average.gte')
    if raw_rating:
        converted_rating = convert_rating_to_trakt_scale(raw_rating)
        if converted_rating:
            trakt_query['ratings'] = converted_rating

    # Runtimes (minutes range or min)
    raw_runtimes = processed_params.get('runtimes')
    if raw_runtimes:
        trakt_query['runtimes'] = str(raw_runtimes).strip()

    # End date (optional)
    raw_end = processed_params.get('end_date')
    if raw_end:
        parsed_end = parse_date_param(str(raw_end))
        if parsed_end:
            trakt_query['end_date'] = parsed_end

    # Query Trakt API
    log(f"[TraktCalendar] Fetching {endpoint} with params={trakt_query}", level=LOGINFO)
    try:
        resp = _fetch_from_trakt(trakt_handler, endpoint, params=trakt_query)
        if not resp:
            return 500, json.dumps({'error': 'Failed to reach Trakt API'}), "application/json"

        if resp.status_code != 200:
            error_msg = f"Trakt API responded with HTTP {resp.status_code}"
            try:
                err_data = resp.json()
                if isinstance(err_data, dict) and 'message' in err_data:
                    error_msg = err_data['message']
            except Exception:
                pass
            return resp.status_code, json.dumps({'error': error_msg}), "application/json"

        raw_items = resp.json()
        if not isinstance(raw_items, list):
            log(f"[TraktCalendar] Unexpected response structure: {type(raw_items)}", level=LOGWARNING)
            return 200, json.dumps([]), "application/json"

        # Format and deduplicate results
        formatted_items = []
        seen_keys = set()

        for item in raw_items:
            if not isinstance(item, dict):
                continue

            show_data = item.get('show')
            movie_data = item.get('movie')
            ep_data = item.get('episode')

            if show_data:
                media_type = 'tv'
                entity = show_data
                ids = entity.get('ids', {})
                tmdb_id = ids.get('tmdb')
                trakt_id = ids.get('trakt')
                imdb_id = ids.get('imdb')
                title = entity.get('title') or 'Unknown'
                year = entity.get('year')
                premiered = item.get('first_aired') or entity.get('first_aired') or ''
                if premiered and 'T' in premiered:
                    premiered = premiered.split('T')[0]
                if not year and premiered:
                    try:
                        year = int(premiered.split('-')[0])
                    except (ValueError, IndexError):
                        pass

                overview = (ep_data.get('overview') if ep_data else None) or entity.get('overview', '')

                # Images
                images = entity.get('images', {})
                poster_url = ''
                posters = images.get('poster', [])
                if posters and isinstance(posters, list) and len(posters) > 0:
                    p = posters[0]
                    poster_url = p if p.startswith('http') else f"https://{p}"

                fanart_url = ''
                fanarts = images.get('fanart', [])
                if fanarts and isinstance(fanarts, list) and len(fanarts) > 0:
                    f = fanarts[0]
                    fanart_url = f if f.startswith('http') else f"https://{f}"

                rating = (ep_data.get('rating') if ep_data else None) or entity.get('rating', 0.0)
                votes = (ep_data.get('votes') if ep_data else None) or entity.get('votes', 0)
                genres = entity.get('genres', [])

                ep_info = None
                if ep_data:
                    ep_info = {
                        'season': ep_data.get('season'),
                        'number': ep_data.get('number'),
                        'title': ep_data.get('title')
                    }

                dedupe_key = (media_type, tmdb_id or trakt_id or title.lower())
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)

                formatted_items.append({
                    'tmdb_id': tmdb_id,
                    'trakt_id': trakt_id,
                    'imdb_id': imdb_id,
                    'media_type': 'tv',
                    'title': title,
                    'original_title': title,
                    'year': year,
                    'premiered': premiered,
                    'overview': overview,
                    'poster_path': poster_url,
                    'fanart_path': fanart_url,
                    'rating': rating,
                    'votes': votes,
                    'genres': genres,
                    'genre_ids': [],  # Trakt uses slugs
                    'episode': ep_info,
                    'source': 'trakt'
                })

            elif movie_data:
                media_type = 'movie'
                entity = movie_data
                ids = entity.get('ids', {})
                tmdb_id = ids.get('tmdb')
                trakt_id = ids.get('trakt')
                imdb_id = ids.get('imdb')
                title = entity.get('title') or 'Unknown'
                year = entity.get('year')
                premiered = item.get('released') or entity.get('released') or ''
                if premiered and 'T' in premiered:
                    premiered = premiered.split('T')[0]
                if not year and premiered:
                    try:
                        year = int(premiered.split('-')[0])
                    except (ValueError, IndexError):
                        pass

                overview = entity.get('overview', '')

                # Images
                images = entity.get('images', {})
                poster_url = ''
                posters = images.get('poster', [])
                if posters and isinstance(posters, list) and len(posters) > 0:
                    p = posters[0]
                    poster_url = p if p.startswith('http') else f"https://{p}"

                fanart_url = ''
                fanarts = images.get('fanart', [])
                if fanarts and isinstance(fanarts, list) and len(fanarts) > 0:
                    f = fanarts[0]
                    fanart_url = f if f.startswith('http') else f"https://{f}"

                rating = entity.get('rating', 0.0)
                votes = entity.get('votes', 0)
                genres = entity.get('genres', [])

                dedupe_key = (media_type, tmdb_id or trakt_id or title.lower())
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)

                formatted_items.append({
                    'tmdb_id': tmdb_id,
                    'trakt_id': trakt_id,
                    'imdb_id': imdb_id,
                    'media_type': 'movie',
                    'title': title,
                    'original_title': title,
                    'year': year,
                    'premiered': premiered,
                    'overview': overview,
                    'poster_path': poster_url,
                    'fanart_path': fanart_url,
                    'rating': rating,
                    'votes': votes,
                    'genres': genres,
                    'genre_ids': [],
                    'episode': None,
                    'source': 'trakt'
                })

        if target_media_type:
            if target_media_type in ('tv', 'show', 'tvshow'):
                formatted_items = [i for i in formatted_items if i.get('media_type') == 'tv']
            elif target_media_type == 'movie':
                formatted_items = [i for i in formatted_items if i.get('media_type') == 'movie']

        log(f"[TraktCalendar] Returning {len(formatted_items)} formatted items for preset={preset_key}", level=LOGINFO)
        return 200, json.dumps(formatted_items), "application/json"

    except Exception as e:
        log(f"[TraktCalendar] Error handling request: {e}", level=LOGERROR)
        return 500, json.dumps({'error': f"Internal server error: {str(e)}"}), "application/json"
