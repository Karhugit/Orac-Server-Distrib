import sqlite3
import json
from resources.lib.log_utils import log, LOGERROR, LOGDEBUG, LOGINFO
from time import time

def _format_tmdb_show_details(show_details, tmdb_handler): 
    """Formats a raw TMDb show details object into the application's standard format."""
    first_aired = show_details.get('first_air_date', '')
    year = int(first_aired.split('-')[0]) if first_aired and first_aired.split('-')[0].isdigit() else None

    # Extract network, country, and IMDB ID from the TMDb response
    network = show_details.get('networks', [{}])[0].get('name') if show_details.get('networks') else None
    country = show_details.get('origin_country', [None])[0] if show_details.get('origin_country') else None
    imdb_id = show_details.get('external_ids', {}).get('imdb_id')

    show_images = tmdb_handler.get_show_images_from_data(show_details)

    show_result = {
        'show_tmdb_id': show_details.get('id'),
        'title': show_details.get('name'),
        'original_title': show_details.get('original_name'),
        'year': year,
        'first_aired': first_aired,
        'overview': show_details.get('overview'),
        'status': show_details.get('status'),
        'tagline': show_details.get('tagline'),
        'rating': show_details.get('vote_average'),
        'votes': show_details.get('vote_count'),
        'poster_path': show_images.get('poster'),
        'fanart_path': show_images.get('fanart'),
        'thumbnail_path': show_images.get('thumb'),
        'clearlogo_path': show_images.get('clearlogo'),
        'landscape_path': show_images.get('landscape'),
        'genres': [genre['name'] for genre in show_details.get('genres', [])],
        # Fields not available from TMDb in this call are set to default/null values
        'show_trakt_id': None, 'imdb_id': imdb_id, 'slug': None, 'last_updated': None, 'dropped': 0,
        'trailer': None, 'certification': None, 'network': network, 'country': country,
        'seasons': []
    }

    tmdb_seasons = tmdb_handler.get_seasons_and_episodes_from_full_data(show_details)
    for season_data in tmdb_seasons:
        season_images = tmdb_handler.get_season_images_from_data(season_data, show_details)
        formatted_season = {
            'season': season_data.get('season_number'),
            'title': season_data.get('name'),
            'overview': season_data.get('overview'),
            'air_date': season_data.get('air_date'),
            'episode_count': len(season_data.get('episodes', [])),
            'poster_path': season_images.get('poster'),
            'fanart_path': season_images.get('fanart'),
            'episodes': []
        }

        for episode_data in season_data.get('episodes', []):
            formatted_episode = dict(episode_data)
            formatted_episode['episode_title'] = episode_data.get('name')
            formatted_episode['episode_overview'] = episode_data.get('overview')

            # Get episode images
            episode_images = tmdb_handler.get_episode_images_from_data(episode_data, show_details)
            formatted_episode['episode_poster_path'] = episode_images.get('poster')
            formatted_episode['episode_fanart_path'] = episode_images.get('fanart')
            formatted_episode['episode_thumbnail_path'] = episode_images.get('thumb')
            formatted_episode['episode_landscape_path'] = episode_images.get('landscape')

            # Watched status is unknown for non-DB items
            formatted_episode['watched_at'] = None
            formatted_episode['percent_watched'] = 0
            formatted_episode['episode_trakt_id'] = None  # Not available from TMDb
            formatted_episode['tmdb_id'] = episode_data.get('id')
            formatted_episode['show_tmdb_id'] = show_details.get('id')
            formatted_episode['first_aired'] = episode_data.get('air_date')
            formatted_episode['rating'] = episode_data.get('vote_average')
            formatted_episode['votes'] = episode_data.get('vote_count')
            formatted_episode['episode_number'] = episode_data.get('episode_number')
            formatted_episode['season'] = episode_data.get('season_number')
            formatted_season['episodes'].append(formatted_episode)
        
        show_result['seasons'].append(formatted_season)

    return show_result

def handle_show_request(show_tmdb_id, user, tvshows_static_db_path, tvshows_dynamic_db_path, tmdb_handler):
    """
    Returns enriched show data (including seasons and episodes)
    for a given show TMDb ID and user.
    """
    try:
        starttime = time()
        with sqlite3.connect(tvshows_static_db_path) as static_conn:
            # Use row factory to get dict-like rows
            static_conn.row_factory = sqlite3.Row
            
            # Attach dynamic DB to static connection for joins
            static_conn.execute(f"ATTACH DATABASE ? AS dynamic_db", (tvshows_dynamic_db_path,))
            
            static_cursor = static_conn.cursor()

            # 1. Get Show Details
            static_cursor.execute("SELECT * FROM shows WHERE show_tmdb_id = ?", (show_tmdb_id,))
            show_data = static_cursor.fetchone()

            if not show_data:
                log(f"Show with TMDb ID {show_tmdb_id} not found in DB. Fetching from TMDb.", LOGINFO)
                show_details = tmdb_handler.get_full_show_details(show_tmdb_id)
                if not show_details:
                    return 404, "Show not found in local database or TMDb", "text/plain"
                
                formatted_show = _format_tmdb_show_details(show_details, tmdb_handler)
                return 200, json.dumps(formatted_show), "application/json"
            
            show_result = dict(show_data)
            # Debug logging to verify imdb_id is present in the database row
            log(f"[ShowHandler] Raw DB row keys for TMDb ID {show_tmdb_id}: {list(show_result.keys())}", LOGDEBUG)
            log(f"[ShowHandler] IMDB ID from DB: {show_result.get('imdb_id', 'NOT FOUND')}", LOGINFO)
            show_trakt_id = show_result['show_trakt_id']

            # 1a. Get Genres for the show
            static_cursor.execute("SELECT genre FROM tvshows_genres WHERE tmdb_id = ?", (show_tmdb_id,))
            genres = [row['genre'] for row in static_cursor.fetchall()]
            show_result['genres'] = genres

            # 2. Get all seasons for the show
            # DB Schema Update: seasons table now uses show_tmdb_id as Foreign Key linked to show_id
            static_cursor.execute("SELECT * FROM seasons WHERE show_id = ? ORDER BY season", (show_tmdb_id,))
            seasons_data = static_cursor.fetchall()
            
            seasons_dict = {s['season']: dict(s) for s in seasons_data}
            for season_num in seasons_dict:
                seasons_dict[season_num]['episodes'] = []

            # 3. Get all episodes for the show with watched status
            static_cursor.execute("""
                SELECT e.*, we.watched_at, COALESCE(we.percent_watched, 0) as percent_watched
                FROM episodes e
                LEFT JOIN dynamic_db.watched_episodes we ON e.tmdb_id = we.tmdb_id AND we.user = ? COLLATE NOCASE
                WHERE e.show_id = ?
                ORDER BY e.season, e.episode_number
            """, (user, show_tmdb_id))
            
            episodes_data = static_cursor.fetchall()
            for episode_row in episodes_data:
                episode_dict = dict(episode_row)
                season_number = episode_dict['season']
                if season_number in seasons_dict:
                    seasons_dict[season_number]['episodes'].append(episode_dict)
            
            show_result['seasons'] = sorted(list(seasons_dict.values()), key=lambda s: s['season'])
            
            # Check if we need to fetch/sync fanart
            try:
                from resources.lib.config_handler import get_fanart_config
                config = get_fanart_config()
                if config["fanart_enabled"]:
                    if show_result.get("fanart_last_updated") is None:
                        log(f"[ShowsHandler] Fanart enabled but not populated for TV show TMDb ID {show_tmdb_id}, fetching on-the-fly...", LOGINFO)
                        from resources.lib.fanart_client import sync_fanart_for_item
                        sync_fanart_for_item(show_tmdb_id, "show", tmdb_handler, config_db_path=None, force=True)
                        # Re-read the database row to get updated fanart values
                        static_cursor.execute("SELECT fanart_poster_path, fanart_fanart_path, fanart_clearlogo_path, fanart_last_updated FROM shows WHERE show_tmdb_id = ?", (show_tmdb_id,))
                        row2 = static_cursor.fetchone()
                        if row2:
                            show_result["fanart_poster_path"] = row2["fanart_poster_path"]
                            show_result["fanart_fanart_path"] = row2["fanart_fanart_path"]
                            show_result["fanart_clearlogo_path"] = row2["fanart_clearlogo_path"]
                            show_result["fanart_last_updated"] = row2["fanart_last_updated"]
                    
                    # Override show artwork with fanart ones
                    from resources.lib.formatting_utils import get_asset_url
                    f_poster = show_result.get("fanart_poster_path")
                    f_fanart = show_result.get("fanart_fanart_path")
                    f_clearlogo = show_result.get("fanart_clearlogo_path")
                    
                    if f_poster:
                        url_poster = get_asset_url(f_poster)
                        show_result["poster_path"] = url_poster
                        show_result["thumbnail_path"] = url_poster
                    if f_fanart:
                        url_fanart = get_asset_url(f_fanart)
                        show_result["fanart_path"] = url_fanart
                        show_result["landscape_path"] = url_fanart
                    if f_clearlogo:
                        show_result["clearlogo_path"] = get_asset_url(f_clearlogo)
            except Exception as e:
                log(f"[ShowsHandler] Error in fanart sync/override: {e}", LOGERROR)

            static_conn.execute("DETACH DATABASE dynamic_db")

            log(f"[Orac] handle_show_request took {time() - starttime:.2f} seconds for TMDb ID {show_tmdb_id}", level=LOGDEBUG)
            
            return 200, json.dumps(show_result), "application/json"

    except Exception as e:
        log(f"[Orac] Error in handle_show_request: {e}", level=LOGERROR)
        return 500, "Error getting show", "text/plain"
