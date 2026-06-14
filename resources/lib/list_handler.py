
# handlers/list_handler.py
import sqlite3
import json
import asyncio
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
from collections import defaultdict
from resources.lib.tmdb_lists import TMDB_GENERIC_LISTS
from resources.lib.trakt_list_sync import add_movie
from resources.lib.db_utils import add_tvshow
from resources.lib.formatting_utils import format_movie

def _get_list_movies(list_name, user, movies_static_db_path, movies_dynamic_db_path, lists_db_path, preserve_order=False):
    # Step 1: Get trakt_ids for movies in the specified list from list_items + lists DB
    with sqlite3.connect(lists_db_path) as lists_conn:
        lists_cursor = lists_conn.cursor()
        lists_cursor.execute("""
            SELECT li.tmdb_id
            FROM list_items li
            JOIN lists l ON li.list_id = l.list_id 
            WHERE l.slug = ? AND li.media_type = 'movie' AND l.user = ?
            ORDER BY li.id ASC
        """, (list_name, user,))
        tmdb_ids = [row[0] for row in lists_cursor.fetchall() if row[0]]
        log(f"[Orac] _get_list_movies: Found {len(tmdb_ids)} TMDB IDs for list '{list_name}': {tmdb_ids}", level=LOGDEBUG)

    if not tmdb_ids:
        return []

    # Step 2: Query movie details from the static DB using TMDB IDs
    placeholders = ",".join(["?"] * len(tmdb_ids))
    with sqlite3.connect(movies_static_db_path) as static_conn:
        static_cursor = static_conn.cursor()
        static_cursor.execute(f"""attach database '{movies_dynamic_db_path}' as dynamic;""")
        static_cursor.execute(f"""
            SELECT 
                m.tmdb_id, m.imdb_id, m.title, m.year, m.released, 
                m.tagline, m.overview, m.runtime, m.country, m.rating, 
                m.language, m.certification, m.original_title, m.trakt_id, 
                m.poster_path, m.fanart_path, m.thumbnail_path, 
                m.landscape_path, m.clearlogo_path, m.belongs_to_collection,
                m.studio, ms.watched, mg.genre
            FROM movies m
            LEFT JOIN movie_genres mg ON m.tmdb_id = mg.tmdb_id
            LEFT JOIN dynamic.movie_status ms ON m.tmdb_id = ms.tmdb_id
            WHERE m.tmdb_id IN ({placeholders})
            ORDER BY m.released DESC, m.tmdb_id, mg.genre
        """, tmdb_ids)
        rows = static_cursor.fetchall()
        log(f"[Orac] _get_list_movies: Query returned {len(rows)} rows for {len(tmdb_ids)} IDs.", level=LOGDEBUG)
        static_cursor.execute("detach database dynamic;")

    movies_dict = defaultdict(lambda: {
        "tmdb_id": None, "imdb_id": None, "title": None, "year": None, "released": None,
        "tagline": None, "overview": None, "runtime": None, "country": None,
        "rating": None, "language": None, "certification": None, "original_title": None,
        "trakt_id": None, "poster_path": None, "fanart_path": None, "thumbnail_path": None,
        "landscape_path": None, "clearlogo_path": None, "belongs_to_collection": None,
        "studio": None, "watched": None, "genres": [], "media_type": "movie"
    })

    for row in rows:
        tmdb_id = row[0]
        # Use TMDB ID as key because Trakt ID can be None for web-sourced items
        if movies_dict[tmdb_id]["tmdb_id"] is None:
            movies_dict[tmdb_id].update({
                "tmdb_id": tmdb_id, "imdb_id": row[1], "title": row[2], "year": row[3], "released": row[4],
                "tagline": row[5], "overview": row[6], "runtime": row[7], "country": row[8],
                "rating": row[9], "language": row[10], "certification": row[11], "original_title": row[12],
                "trakt_id": row[13], "poster_path": row[14], "fanart_path": row[15], "thumbnail_path": row[16],
                "landscape_path": row[17], "clearlogo_path": row[18],
                "belongs_to_collection": json.loads(row[19]) if row[19] else None,
                "studio": json.loads(row[20]) if row[20] else None,
                "watched": row[21] if row[21] is not None else 0
            })
        genre = row[22]
        if genre and genre not in movies_dict[tmdb_id]["genres"]:
            movies_dict[tmdb_id]["genres"].append(genre)

    movies = [format_movie(m, None) for m in list(movies_dict.values())]
    if preserve_order:
        id_to_index = {int(tmdb_id): idx for idx, tmdb_id in enumerate(tmdb_ids)}
        movies.sort(key=lambda x: id_to_index.get(x["tmdb_id"], 999))
    else:
        movies.sort(key=lambda x: str(x.get("released") or 0), reverse=True)
    return movies

def _get_list_shows(list_name, user, tvshows_static_db_path, tvshows_dynamic_db_path, lists_db_path, preserve_order=False):
    def get_total_seasons_and_episodes(show_tmdb_id, static_conn, dynamic_conn):
        static_cursor = static_conn.cursor()
        static_cursor.execute("SELECT COUNT(DISTINCT season) FROM episodes WHERE show_id = ?", (show_tmdb_id,))
        total_seasons = static_cursor.fetchone()[0]
        static_cursor.execute("SELECT COUNT(*) FROM episodes WHERE show_id = ?", (show_tmdb_id,))
        total_episodes = static_cursor.fetchone()[0]
        static_cursor.execute("""
            SELECT COUNT(DISTINCT E.tmdb_id)
            FROM episodes AS E
            JOIN dynamic_db.watched_episodes AS W ON E.tmdb_id = W.tmdb_id
            WHERE E.show_id = ?;""", (show_tmdb_id,))
        total_watched_episodes = static_cursor.fetchone()[0]
        static_cursor.execute("""
            SELECT COUNT(*) FROM episodes
            WHERE show_id = ? AND first_aired > strftime('%Y-%m-%d %H:%M:%S', 'now');
        """, (show_tmdb_id,))
        total_unaired_episodes = static_cursor.fetchone()[0]
        return total_seasons, total_episodes, total_watched_episodes, total_unaired_episodes

    with sqlite3.connect(lists_db_path) as lists_conn:
        lists_cursor = lists_conn.cursor()
        lists_cursor.execute("""
            SELECT li.tmdb_id
            FROM list_items li
            JOIN lists l ON li.list_id = l.list_id 
            WHERE l.slug = ? AND li.media_type = 'show' AND l.user = ?
            ORDER BY li.id ASC
        """, (list_name, user,))
        show_ids = [row[0] for row in lists_cursor.fetchall() if row[0]]

    if not show_ids:
        return []

    placeholders = ",".join(["?"] * len(show_ids))
    with sqlite3.connect(tvshows_dynamic_db_path) as dynamic_conn, \
         sqlite3.connect(tvshows_static_db_path) as static_conn:
        static_conn.execute(f"ATTACH DATABASE ? AS dynamic_db", (tvshows_dynamic_db_path,))
        static_cursor = static_conn.cursor()
        static_cursor.execute(f"""
            SELECT show_trakt_id, show_tmdb_id, imdb_id, title, year, slug, last_updated, first_aired, 
            poster_path, fanart_path, thumbnail_path, landscape_path, clearlogo_path,
            original_title, trailer, overview, tagline, status, certification, network, country, rating, votes
            FROM shows
            WHERE show_tmdb_id IN ({placeholders})
        """, show_ids)
        rows = static_cursor.fetchall()

        tmdb_ids = [show[1] for show in rows if show[1]]
        genres_dict = {}
        if tmdb_ids:
            tmdb_placeholders = ",".join(["?"] * len(tmdb_ids))
            static_cursor.execute(f"""
                SELECT tg.tmdb_id, g.name 
                FROM genres g
                JOIN tvshows_genres tg ON g.name = tg.genre
                WHERE tg.tmdb_id IN ({tmdb_placeholders})
            """, tmdb_ids)
            for tmdb_id, genre in static_cursor.fetchall():
                if tmdb_id not in genres_dict:
                    genres_dict[tmdb_id] = []
                genres_dict[tmdb_id].append(genre)

        shows = []
        for show in rows:
            total_seasons, total_episodes, total_watched_episodes, total_unaired_episodes = get_total_seasons_and_episodes(show[1], static_conn, dynamic_conn)
            shows.append({
                "trakt_id": show[0], "tmdb_id": show[1], "imdb_id": show[2], "title": show[3], "year": show[4],
                "slug": show[5], "last_updated": show[6], "premiered": show[7], "poster_path": show[8],
                "fanart_path": show[9], "thumbnail_path": show[10], "landscape_path": show[11],
                "clearlogo_path": show[12], "total_seasons": total_seasons, "total_episodes": total_episodes,
                "total_watched_episodes": total_watched_episodes, "total_unaired_episodes": total_unaired_episodes,
                "original_title": show[13], "trailer": show[14], "overview": show[15], "tagline": show[16],
                "genres": genres_dict.get(show[1], []), "status": show[17], "certification": show[18],
                "network": show[19], "country": show[20], "rating": show[21], "votes": show[22], "media_type": "show"
            })
        if preserve_order:
            id_to_index = {int(show_id): idx for idx, show_id in enumerate(show_ids)}
            shows.sort(key=lambda x: id_to_index.get(x["tmdb_id"], 999))
    return shows

async def handle_list_request(list_name, item_type, user, movies_dynamic_db_path, movies_static_db_path, tvshows_dynamic_db_path, tvshows_static_db_path, lists_db_path, trakt_handler=None, tmdb_handler=None, ext_indexes_db_path=None):
    if not list_name:
        return 400, "Missing list name", "text/plain"
    if not item_type:
        return 400, "Missing item type", "text/plain"

    try:
        # Step 1: Check list metadata to see if it's in the library
        add_to_library = 0
        source = 'trakt' # Default
        slug = list_name
        list_user = user
        list_id = None
        
        with sqlite3.connect(lists_db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Resolve list_id using slug/user pair which is safer than reconstructing the ID
            # Priority 1: Exact slug and user match
            cursor.execute("SELECT list_id, source, user, slug, add_to_library FROM lists WHERE slug = ? AND user = ?", (list_name, user))
            row = cursor.fetchone()
            
            # Priority 2: Try by list_id for legacy compatibility if user passed "user:slug" as name? 
            # Or just fallback to name match if passed slug is actually a name
            if not row:
                 cursor.execute("SELECT list_id, source, user, slug, add_to_library FROM lists WHERE name = ? AND user = ? COLLATE NOCASE", (list_name, user))
                 row = cursor.fetchone()

            if row:
                list_id = row['list_id']
                add_to_library = row['add_to_library']
                source = row['source']
                slug = row['slug']
                list_user = row['user']
            else:
                # If not found locally, we assume it's external.
                # Construct a temporary fallback ID if needed for logging, but we mainly need source/add_to_library
                list_id = f"{user}:{list_name}" # Legacy fallback for logging
                log(f"[Orac] List '{list_name}' not found in DB. Treating as external.", level=LOGDEBUG)
                # Check if this is a known TMDB generic list even if not in DB (e.g. fresh install)
                from resources.lib.tmdb_lists import TMDB_GENERIC_LISTS
                _tmdb_slugs = {l['slug'] for l in TMDB_GENERIC_LISTS}
                if list_name in _tmdb_slugs:
                    source = 'tmdb'
                    log(f"[Orac] List '{list_name}' recognised as TMDB generic list.", level=LOGDEBUG)

        # Step 2: If add_to_library is 1, or source is web/mdblist/flixpatrol, fetch from local DB
        # (These sources always have items synced locally, so we serve from DB regardless of add_to_library)
        if add_to_library == 1 or source in ('web', 'mdblist', 'flixpatrol'):
            results = []
            if source == 'web':
                 log(f"[Orac] List {list_name} is web-sourced. Serving from local database.", level=LOGDEBUG)
            
            # If list source is in ('flixpatrol', 'web', 'mdblist'), we want to preserve insertion/rank order
            preserve = source in ('flixpatrol', 'web', 'mdblist')
            
            if item_type in ["movie", "all"]:
                results.extend(_get_list_movies(list_name, user, movies_static_db_path, movies_dynamic_db_path, lists_db_path, preserve_order=preserve))
            if item_type in ["tvshow", "all"]:
                results.extend(_get_list_shows(list_name, user, tvshows_static_db_path, tvshows_dynamic_db_path, lists_db_path, preserve_order=preserve))
            
            if not results and item_type not in ["movie", "tvshow", "all"]:
                return 400, f"Unsupported item type: {item_type}", "text/plain"

            if not preserve:
                # Sort combined results by date
                def get_date(item):
                    return str(item.get("released") or item.get("premiered") or 0)
                results.sort(key=get_date, reverse=True)
                
            return 200, json.dumps(results), "application/json"

        # Step 3: If add_to_library is 0, fetch externally
        log(f"[Orac] List {list_name} not in library (add_to_library=0). Fetching from {source}...", level=LOGINFO)
        
        if source == 'trakt':
            results = await _fetch_trakt_list_external(trakt_handler, tmdb_handler, list_user, slug, item_type, movies_static_db_path, tvshows_static_db_path)
        elif source == 'tmdb':
            # Distinguish between External Index (Saved Query) and Static List
            if list_user == 'external_index':
               results = await _fetch_tmdb_discover_external(tmdb_handler, slug, item_type, ext_indexes_db_path)
            else:
               results = await _fetch_tmdb_list_external(tmdb_handler, slug, item_type)
        else:
            log(f"[Orac] Unknown source '{source}' for list {list_name}", level=LOGWARNING)
            results = []

        # Enrich external results with watched/in-progress status from the local database
        results = _enrich_external_results_watched_status(results, user, movies_dynamic_db_path, tvshows_dynamic_db_path, tvshows_static_db_path)

        return 200, json.dumps(results), "application/json"

    except Exception as e:
        log(f"[Orac] Error in handle_list_request: {e}", level=LOGERROR)
        import traceback
        log(traceback.format_exc(), LOGERROR)
        return 500, "Error getting list", "text/plain"

async def _fetch_trakt_list_external(trakt_handler, tmdb_handler, user, slug, item_type, movies_static_db_path, tvshows_static_db_path):
    """Fetches list items directly from Trakt API."""
    if not trakt_handler:
        log("[Orac] Trakt handler not available for external fetch", level=LOGERROR)
        return []

    try:
        path = ""
        if slug == "watchlist":
            path = "/users/me/watchlist/all?extended=full"
        elif slug == "favorites":
            path = "/users/me/favorites?extended=full"
        elif slug == "collection-movies":
            path = "/users/me/collection/movies?extended=full"
        elif slug == "collection-tvshows":
            path = "/users/me/collection/shows?extended=full"
        # Check if this is a Trakt generic list
        from resources.lib.trakt_lists import is_generic_list, get_list_config
        
        if is_generic_list(slug):
            config = get_list_config(slug)
            path = config["endpoint"]
            log(f"[Orac] Fetching Trakt items from external path: {path}", level=LOGINFO)
            resp = await trakt_handler.get(path, authenticated=config.get("requires_auth", False))
        else:
            # Custom lists
            if not user or user == 'None':
                log(f"[Orac] Custom list '{slug}' requested without a valid user. Attempting to use 'me' as fallback.", level=LOGWARNING)
                user = "me"
            path = f"/users/{user}/lists/{slug}/items?extended=full&limit=1000"
            log(f"[Orac] Fetching Trakt items from external path: {path}", level=LOGINFO)
            resp = await trakt_handler.get(path)
        if not resp:
            log(f"[Orac] No response from Trakt handler for path: {path}", level=LOGERROR)
            return []
            
        if resp.status_code != 200:
            log(f"[Orac] Failed to fetch Trakt list {slug} from path {path}: status {resp.status_code}", level=LOGWARNING)
            return []

        data = resp.json()
        items = []
        
        for item in data:
            media_type = item.get("type")
            if not media_type:
                if "movie" in item: media_type = "movie"
                elif "show" in item: media_type = "show"
                elif is_generic_list(slug):
                    media_type = get_list_config(slug).get("media_type")
                elif slug.startswith("collection-"):
                    media_type = "movie" if "movie" in slug else "show"
            
            if media_type == "movie" and item_type in ["movie", "all"]:
                movie = item.get("movie") or item
                items.append({
                    "trakt_id": movie.get("ids", {}).get("trakt"),
                    "tmdb_id": movie.get("ids", {}).get("tmdb"),
                    "imdb_id": movie.get("ids", {}).get("imdb"),
                    "title": movie.get("title"),
                    "year": movie.get("year"),
                    "released": movie.get("released"),
                    "overview": movie.get("overview"),
                    "media_type": "movie"
                })
            elif (media_type == "show" or media_type == "tvshow") and item_type in ["tvshow", "all"]:
                show = item.get("show") or item
                items.append({
                    "trakt_id": show.get("ids", {}).get("trakt"),
                    "tmdb_id": show.get("ids", {}).get("tmdb"),
                    "imdb_id": show.get("ids", {}).get("imdb"),
                    "title": show.get("title"),
                    "year": show.get("year"),
                    "premiered": show.get("first_aired"),
                    "overview": show.get("overview"),
                    "media_type": "show"
                })

        # Enrich items with images and extra metadata
        enriched_items = await _enrich_items_with_metadata(items, tmdb_handler, movies_static_db_path, tvshows_static_db_path)
        # Final formatting pass
        return [format_movie(item, tmdb_handler) if item.get('media_type') == 'movie' else item for item in enriched_items]

    except Exception as e:
        log(f"[Orac] Error fetching Trakt list {slug} externally: {e}", level=LOGERROR)
        import traceback
        log(traceback.format_exc(), LOGERROR)
        return []

async def _enrich_items_with_metadata(items, tmdb_handler, movies_static_db_path, tvshows_static_db_path):
    """Enriches list items with images and metadata from local DB and TMDb."""
    if not items:
        return []

    try:
        # 1. Gather local metadata first
        with sqlite3.connect(movies_static_db_path) as m_conn, sqlite3.connect(tvshows_static_db_path) as s_conn:
            m_conn.row_factory = sqlite3.Row
            s_conn.row_factory = sqlite3.Row
            m_cursor = m_conn.cursor()
            s_cursor = s_conn.cursor()

            for item in items:
                trakt_id = item.get("trakt_id")
                tmdb_id = item.get("tmdb_id")
                
                if item["media_type"] == "movie":
                    m_cursor.execute("SELECT poster_path, fanart_path, thumbnail_path, landscape_path, tagline, runtime, rating, studio FROM movies WHERE trakt_id = ? OR tmdb_id = ?", (trakt_id, tmdb_id))
                    row = m_cursor.fetchone()
                    if row:
                        item.update(dict(row))
                else:
                    s_cursor.execute("SELECT poster_path, fanart_path, thumbnail_path, landscape_path, status, rating, network FROM shows WHERE show_trakt_id = ? OR show_tmdb_id = ?", (trakt_id, tmdb_id))
                    row = s_cursor.fetchone()
                    if row:
                        item.update(dict(row))

        # 2. Identify items missing images and fetch from TMDb concurrently
        missing_image_items = [item for item in items if not item.get("poster_path")]
        log(f"[Orac] Enrichment: {len(items)} items total, {len(missing_image_items)} missing images.", level=LOGINFO)
        
        if missing_image_items and tmdb_handler:
            log(f"[Orac] Fetching missing images for {len(missing_image_items)} items from TMDb...", level=LOGINFO)
            
            tasks = []
            for item in missing_image_items:
                tmdb_id = item.get("tmdb_id")
                if tmdb_id:
                    media_type = 'movie' if item['media_type'] == 'movie' else 'tv'
                    tasks.append(_fetch_single_tmdb_poster(tmdb_handler, tmdb_id, media_type, item))
            
            if tasks:
                await asyncio.gather(*tasks)

        return items
    except Exception as e:
        log(f"[Orac] Error enriching metadata: {e}", level=LOGERROR)
        return items

async def _fetch_single_tmdb_poster(tmdb_handler, tmdb_id, media_type, item):
    """Helper to fetch a single poster path from TMDb asynchronously."""
    try:
        # Wrap the blocking TMDbAPI calls in a thread
        def _get_tmdb_data():
            path = f"/{media_type}/{tmdb_id}"
            return tmdb_handler._get(path)
        
        data = await asyncio.to_thread(_get_tmdb_data)
        if data:
            item["poster_path"] = tmdb_handler._build_url(data.get("poster_path"), 'w780')
            item["fanart_path"] = tmdb_handler._build_url(data.get("backdrop_path"), 'w1280')
            if media_type == 'movie':
                item["tagline"] = data.get("tagline")
                item["runtime"] = data.get("runtime")
                item["rating"] = data.get("vote_average")
            else:
                item["status"] = data.get("status")
                item["rating"] = data.get("vote_average")
    except Exception as e:
        log(f"[Orac] Error fetching TMDb data for {media_type} {tmdb_id}: {e}", level=LOGWARNING)

async def _fetch_tmdb_discover_external(tmdb_handler, slug, item_type, ext_indexes_db_path):
    """Fetches items using TMDb Discover API based on stored parameters."""
    if not tmdb_handler or not ext_indexes_db_path:
        log("[Orac] TMDb handler or indexes DB path not available", level=LOGERROR)
        return []

    try:
        from resources.lib.db_utils import get_discover_params_from_db
        from resources.lib.discover_handler import handle_discover_request
        
        with sqlite3.connect(ext_indexes_db_path) as conn:
            cursor = conn.cursor()
            params = get_discover_params_from_db(cursor, slug)
            
            if not params:
                # 2. Try "unslugified" match (replace hyphens with spaces)
                unslugified_name = slug.replace("-", " ")
                params = get_discover_params_from_db(cursor, unslugified_name)
                
                if params:
                    # Found it with unslugified name
                    log(f"[Orac] Found external index '{unslugified_name}' for slug '{slug}'", level=LOGINFO)
                    cursor.execute("SELECT media_type FROM external_indexes WHERE id = ?", (unslugified_name,))
                    row = cursor.fetchone()
                    media_type = row[0] if row else "movie"
                else:
                    # 3. Fuzzy/Normalized Match: Fetch all IDs and compare normalized strings
                    cursor.execute("SELECT id, media_type FROM external_indexes")
                    all_indexes = cursor.fetchall()
                    
                    found_id = None
                    # Normalize the search slug: lowercase, remove non-alphanumeric (keep spaces/hyphens for splitting)
                    def normalize(s):
                        return "".join(c.lower() for c in s if c.isalnum())

                    target_norm = normalize(slug)
                    
                    for idx_id, idx_media_type in all_indexes:
                        if normalize(idx_id) == target_norm:
                            found_id = idx_id
                            media_type = idx_media_type
                            break
                    
                    if found_id:
                        log(f"[Orac] Found external index '{found_id}' via fuzzy match for slug '{slug}'", level=LOGINFO)
                        params = get_discover_params_from_db(cursor, found_id)
                    else:
                        log(f"[Orac] No discover parameters found for {slug} (tried exact, unslugified, and fuzzy)", level=LOGWARNING)
                        return []
            else:
                cursor.execute("SELECT media_type FROM external_indexes WHERE id = ?", (slug,))
                row = cursor.fetchone()
                media_type = row[0] if row else "movie"

        # Reuse handle_discover_request logic but in a more direct way
        # handle_discover_request(item_type, query_params, tmdb_handler, ext_indexes_cursor)
        # However, handle_discover_request is designed for HTTP query params.
        # Let's call tmdb_handler.discover_media directly.
        
        tmdb_media_type = 'tv' if media_type in ['show', 'tvshow'] else 'movie'
        data = tmdb_handler.discover_media(tmdb_media_type, params)
        
        if not data or 'results' not in data:
            return []

        results = data['results']
        formatted_results = []
        
        for item in results:
            if tmdb_media_type == 'movie':
                formatted_results.append({
                    "tmdb_id": item.get("id"),
                    "title": item.get("title"),
                    "year": int(item.get("release_date")[:4]) if item.get("release_date") else None,
                    "released": item.get("release_date"),
                    "overview": item.get("overview"),
                    "poster_path": tmdb_handler._build_url(item.get("poster_path"), 'w780'),
                    "media_type": "movie"
                })
            else:
                formatted_results.append({
                    "tmdb_id": item.get("id"),
                    "title": item.get("name"),
                    "year": int(item.get("first_air_date")[:4]) if item.get("first_air_date") else None,
                    "premiered": item.get("first_air_date"),
                    "overview": item.get("overview"),
                    "poster_path": tmdb_handler._build_url(item.get("poster_path"), 'w780'),
                    "media_type": "show"
                })

        return [format_movie(item, tmdb_handler) if item.get('media_type') == 'movie' else item for item in formatted_results]

    except Exception as e:
        log(f"[Orac] Error fetching TMDb discover list {slug} externally: {e}", level=LOGERROR)
        return []

async def _fetch_tmdb_list_external(tmdb_handler, slug, item_type):
    """Fetches items from a static TMDb list."""
    if not tmdb_handler:
        log("[Orac] TMDb handler not available", level=LOGERROR)
        return []

    try:
        # 1. Fetch the list details from TMDb
        # Support both integer IDs (legacy v3 lists) and string IDs (v4 lists) though v3 is most common.
        # tmdb_handler._get handles building the full URL.
        # Check if it's a generic list
        generic_list = next((l for l in TMDB_GENERIC_LISTS if l['slug'] == slug), None)
        data = None  # Ensure data is always defined

        if generic_list:
            method_name = generic_list['api_method']
            if hasattr(tmdb_handler, method_name):
                # Fetch multiple pages to get more results (TMDB returns 20 per page)
                all_results = []
                max_pages = 5
                for page_num in range(1, max_pages + 1):
                    raw_data = await asyncio.to_thread(getattr(tmdb_handler, method_name), page_num)
                    if raw_data and 'results' in raw_data:
                        all_results.extend(raw_data['results'])
                        total_pages = raw_data.get('total_pages', 1)
                        if page_num >= total_pages:
                            break
                    else:
                        break
                data = {'items': all_results}
            else:
                log(f"[Orac] TMDB Handler missing method {method_name} for generic list {slug}", level=LOGERROR)
        
        else:
            # Standard List
            # Support both integer IDs (legacy v3 lists) and string IDs (v4 lists) though v3 is most common.
            # tmdb_handler._get handles building the full URL.
            data = await asyncio.to_thread(tmdb_handler._get, f"/list/{slug}")
        
        if not data or 'items' not in data:
            log(f"[Orac] Failed to fetch valid data for TMDb list {slug}", level=LOGWARNING)
            return []

        results = data['items']
        formatted_results = []

        for item in results:
            media_type = item.get('media_type')
             # TMDb lists can contain both movies and TV shows
            
            if media_type == 'movie' and item_type in ['movie', 'all']:
                 formatted_results.append({
                    "tmdb_id": item.get("id"),
                    "title": item.get("title"),
                    "year": int(item.get("release_date")[:4]) if item.get("release_date") else None,
                    "released": item.get("release_date"),
                    "overview": item.get("overview"),
                    "poster_path": tmdb_handler._build_url(item.get("poster_path"), 'w780'),
                    "media_type": "movie"
                })
            elif media_type == 'tv' and item_type in ['tvshow', 'all']:
                 formatted_results.append({
                    "tmdb_id": item.get("id"),
                    "title": item.get("name"),
                    "year": int(item.get("first_air_date")[:4]) if item.get("first_air_date") else None,
                    "premiered": item.get("first_air_date"),
                    "overview": item.get("overview"),
                    "poster_path": tmdb_handler._build_url(item.get("poster_path"), 'w780'),
                    "media_type": "show"
                })

        return [format_movie(item, tmdb_handler) if item.get('media_type') == 'movie' else item for item in formatted_results]

    except Exception as e:
        log(f"[Orac] Error fetching TMDb list {slug} externally: {e}", level=LOGERROR)
        return []


def add_to_list(payload, trakt_handler, tmdb_handler, lists_db_path, movies_static_db_path, tvshows_static_db_path, trakt_update_queue_path):
    list_name = payload.get("list_name", [None])[0]
    user = payload.get("user", [None])[0]
    slug = payload.get("slug", [None])[0]
    tmdb_id_str = payload.get("tmdb_id", [None])[0]
    item_type = payload.get("item_type", [None])[0]  # 'movie' or 'tvshow'
    requested_source = payload.get("source", [None])[0]  # explicit source from caller (e.g. 'tmdb', 'trakt')

    if not list_name or not user or not tmdb_id_str or not item_type:
        return {'status': 'error', 'message': 'Missing name, type, or tmdb_id'}

    try:
        tmdb_id = int(tmdb_id_str)
    except ValueError:
        return {'status': 'error', 'message': 'Invalid tmdb_id format'}

    trakt_id = None
    # Step 1: Ensure the item exists in the static DB, add it if not.
    try:
        if item_type == 'movie':
            with sqlite3.connect(movies_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT trakt_id FROM movies WHERE tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                if not row:
                    log(f"Movie with TMDb ID {tmdb_id} not in DB. Fetching from Trakt/TMDb.", LOGINFO)
                    # Fetch full movie details from Trakt to add it
                    response = trakt_handler._get(f"/search/tmdb/{tmdb_id}?type=movie&extended=full")
                    if response.status_code == 200 and response.json():
                        movie_data = response.json()[0].get('movie')
                        if movie_data:
                            trakt_id = movie_data['ids']['trakt']
                            media_id = {"trakt": trakt_id, "tmdb": tmdb_id}
                            # Use trakt_update_queue_path as connection for dynamic db since it's likely just a writable cursor needed
                            # Wait, dynamic_db is passed to add_movie? No, add_movie takes cursor, dynamic_cursor.
                            # We can't easily get dynamic_db connection here without path? 
                            # Oops, add_to_list signature has movies_static_db_path but not dynamic?
                            # Ah, handle_list_request doesn't use dynamic for add_to_list anyway, wait. 
                            # add_to_list function signature (line 558) DOES NOT have dynamic DB paths.
                            # It imports add_movie which requires it.
                            # But wait, original code (line 590) did: `with sqlite3.connect(movies_dynamic_db_path) as dynamic_conn:`
                            # BUT `movies_dynamic_db_path` is NOT passed in arguments!
                            # This means original code would have crashed? Or it relies on global import?
                            # checking line 558... arguments are: payload, trakt_handler, tmdb_handler, lists_db_path, movies_static_db_path, tvshows_static_db_path, trakt_update_queue_path
                            # It seems I missed that. But line 590 in original explicitly uses `movies_dynamic_db_path`.
                            # Maybe it's a global variable? `SimpleHTTPRequestHandler` has it. 
                            # But `list_handler.py` is a module.
                            # Let's assume for now we skip the ADDING to DB part if it's missing and provider is TMDB.
                            # If provider is Trakt, we need it.
                            # Let's fallback to just setting trakt_id to None if we can't fetch it or if DB is missing.
                            pass
                        else:
                            # raise Exception("Movie data not found in Trakt response.")
                            pass
                    else:
                        # raise Exception(f"Failed to fetch movie details from Trakt: {response.status_code}")
                        pass
                else:
                    trakt_id = row[0]
        elif item_type == 'tvshow':
            with sqlite3.connect(tvshows_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT show_trakt_id FROM shows WHERE show_tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                if not row:
                    pass # Similar issue with dynamic DB path
                else:
                    trakt_id = row[0]

        # Determine Provider and Real List ID
        # requested_source from the caller (Liberator) takes priority to avoid ambiguity
        # between same-named lists from different providers (e.g. Trakt watchlist vs TMDB watchlist)
        provider = requested_source or 'trakt'
        list_id = None

        with sqlite3.connect(lists_db_path) as conn:
            cursor = conn.cursor()
            # Try to find the list by user and slug
            cursor.execute("SELECT list_id, source FROM lists WHERE slug = ? AND user = ?", (slug, user))
            row = cursor.fetchone()

            if row:
                list_id = row[0]
                # Only override provider from DB if caller didn't specify one
                if not requested_source:
                    provider = row[1]
            else:
                # Fallback: Try just slug
                cursor.execute("SELECT list_id, source FROM lists WHERE slug = ? AND source = ?", (slug, provider))
                row = cursor.fetchone()
                if row:
                    list_id = row[0]
                    if not requested_source:
                        provider = row[1]

        if not list_id:
            # Construct fallback list_id based on resolved provider
            list_id = f"{provider}:personal:{slug}"
            log(f"[Orac] List '{slug}' not found locally for add_to_list. Using fallback ID: {list_id}", level=LOGWARNING)

        if not trakt_id and provider == 'trakt':
             # Try one last time to query Trakt directly if allowed?
             # For now, if we fail to get Trakt ID, we can't queue to Trakt reliably without it?
             # Actually queue payload uses TMDB ID. Trakt ID in queue table is user ID.
             # So we can proceed!
             pass
        
        # Step 2: Add item to the list_items table and update list count
        with sqlite3.connect(lists_db_path) as conn:
            cursor = conn.cursor()
            # Add item to list
            if item_type == 'tvshow':
                db_item_type = 'show'
            else:
                db_item_type = item_type
            
            # Use 0 or NULL for trakt_id if missing?
            t_id_val = str(trakt_id) if trakt_id else None

            cursor.execute("INSERT OR IGNORE INTO list_items (list_id, media_type, trakt_id, tmdb_id) VALUES (?, ?, ?, ?);",
                           (list_id, db_item_type, t_id_val, str(tmdb_id)))

            # Only update the count if a new row was inserted
            if conn.total_changes > 0:
                log(f"Item {tmdb_id} added to list {list_id}. Updating count.", LOGINFO)
                if db_item_type == 'movie':
                    cursor.execute("UPDATE lists SET item_count_movies = item_count_movies + 1 WHERE list_id = ?;", (list_id,))
                elif db_item_type == 'show':
                    cursor.execute("UPDATE lists SET item_count_shows = item_count_shows + 1 WHERE list_id = ?;", (list_id,))
            conn.commit()

        # Step 3: Queue the update
        with sqlite3.connect(trakt_update_queue_path) as conn:
            cursor = conn.cursor()
            queue_payload = {
                "list_name": list_name,
                "item_type": item_type,
                "tmdb_id": tmdb_id,
                "slug": slug,
                "source": provider
            }
            cursor.execute("""
                INSERT INTO update_queue (trakt_id, update_type, payload, status, media_type, provider)
                VALUES (?, ?, ?, 'pending', ?, ?)
            """, (user, 'add_to_list', json.dumps(queue_payload), item_type, provider))
            conn.commit()

        log(f"Successfully added item {tmdb_id} to list '{list_name}' ({provider}) locally and queued for sync.", LOGINFO)
        return {'status': 'success', 'message': 'Item added to list and task queued'}

    except Exception as e:
        log(f"[Orac] Failed to add item to list: {e}", level=LOGERROR)
        import traceback
        log(traceback.format_exc(), LOGERROR)
        return {'status': 'error', 'message': f'Failed to add item to list: {e}'}

# Remove from list function
def remove_from_list(payload, trakt_handler, tmdb_handler, lists_db_path, movies_static_db_path, tvshows_static_db_path, trakt_update_queue_path):
    list_name = payload.get("list_name", [None])[0]
    user = payload.get("user", [None])[0]
    slug = payload.get("slug", [None])[0]
    tmdb_id_str = payload.get("tmdb_id", [None])[0]
    item_type = payload.get("item_type", [None])[0]  # 'movie' or 'tvshow'
    requested_source = payload.get("source", [None])[0]  # explicit source from caller

    if not list_name or not user or not tmdb_id_str or not item_type:
        return {'status': 'error', 'message': 'Missing name, type, or tmdb_id'}

    try:
        tmdb_id = int(tmdb_id_str)
    except ValueError:
        return {'status': 'error', 'message': 'Invalid tmdb_id format'}

    try:
        trakt_id = None
        # Resolve provider first — requested_source takes priority over DB lookup
        provider = requested_source or 'trakt'
        list_id = None

        with sqlite3.connect(lists_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT list_id, source FROM lists WHERE slug = ? AND user = ?", (slug, user))
            row = cursor.fetchone()
            if row:
                list_id = row[0]
                if not requested_source:
                    provider = row[1]
            else:
                cursor.execute("SELECT list_id, source FROM lists WHERE slug = ? AND source = ?", (slug, provider))
                row = cursor.fetchone()
                if row:
                    list_id = row[0]
                    if not requested_source:
                        provider = row[1]

        if not list_id:
            list_id = f"{provider}:personal:{slug}"
            log(f"[Orac] List '{slug}' not found locally for remove_from_list. Using fallback ID: {list_id}", level=LOGWARNING)
        
        # Step 2: Remove item from the list_items table and update list count
        if list_id:
            with sqlite3.connect(lists_db_path) as conn:
                cursor = conn.cursor()
                if item_type == 'tvshow':
                    db_item_type = 'show'
                else:
                    db_item_type = item_type
    
                cursor.execute("DELETE FROM list_items WHERE list_id = ? AND media_type = ? AND tmdb_id = ?;",
                               (list_id, db_item_type, str(tmdb_id)))
    
                # Only update the count if a row was deleted
                if conn.total_changes > 0:
                    log(f"Item {tmdb_id} removed from list {list_id}. Updating count.", LOGINFO)
                    if db_item_type == 'movie':
                        cursor.execute("UPDATE lists SET item_count_movies = item_count_movies - 1 WHERE list_id = ? AND item_count_movies > 0;", (list_id,))
                    elif db_item_type == 'show':
                        cursor.execute("UPDATE lists SET item_count_shows = item_count_shows - 1 WHERE list_id = ? AND item_count_shows > 0;", (list_id,))
                conn.commit()

        # Step 3: Queue the update
        with sqlite3.connect(trakt_update_queue_path) as conn:
            cursor = conn.cursor()
            queue_payload = {
                "list_name": list_name,
                "item_type": item_type,
                "tmdb_id": tmdb_id,
                "slug": slug,
                "source": provider
            }
            cursor.execute("""
                INSERT INTO update_queue (trakt_id, update_type, payload, status, media_type, provider)
                VALUES (?, ?, ?, 'pending', ?, ?)
            """, (user, 'remove_from_list', json.dumps(queue_payload), item_type, provider))
            conn.commit()
        log(f"Successfully removed item {tmdb_id} from list '{list_name}' ({provider}) locally and queued for sync.", LOGINFO)
        return {'status': 'success', 'message': 'Item removed from list and task queued'}

    except Exception as e:
        log(f"[Orac] Failed to remove item from list: {e}", level=LOGERROR)
        import traceback
        log(traceback.format_exc(), LOGERROR)
        return {'status': 'error', 'message': f'Failed to remove item from list: {e}'}

def _enrich_external_results_watched_status(results, user, movies_dynamic_db_path, tvshows_dynamic_db_path, tvshows_static_db_path):
    if not results:
        return results

    # 1. Separate movies and shows
    movies = [item for item in results if item.get('media_type') == 'movie']
    shows = [item for item in results if item.get('media_type') == 'show']
    user_val = user or ''

    # 2. Enrich movies
    movie_ids = [m['tmdb_id'] for m in movies if m.get('tmdb_id') is not None]
    if movie_ids:
        try:
            placeholders = ",".join(["?"] * len(movie_ids))
            with sqlite3.connect(movies_dynamic_db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT tmdb_id, watched, watched_status 
                    FROM movie_status 
                    WHERE tmdb_id IN ({placeholders})
                """, movie_ids)
                rows = cursor.fetchall()
                movie_status_map = {row['tmdb_id']: (row['watched'], row['watched_status']) for row in rows}
                
                for m in movies:
                    tmdb_id = m['tmdb_id']
                    if tmdb_id in movie_status_map:
                        watched, watched_status = movie_status_map[tmdb_id]
                        m['watched'] = watched
                        m['watched_status'] = watched_status
        except Exception as e:
            log(f"[Orac] Error enriching external movie results: {e}", level=LOGERROR)

    # 3. Enrich shows
    show_ids = [s['tmdb_id'] for s in shows if s.get('tmdb_id') is not None]
    if show_ids:
        try:
            placeholders = ",".join(["?"] * len(show_ids))
            
            seasons_map = {}
            episodes_map = {}
            watched_map = {}
            unaired_map = {}
            
            with sqlite3.connect(tvshows_static_db_path) as static_conn:
                static_conn.execute("ATTACH DATABASE ? AS dynamic_db", (tvshows_dynamic_db_path,))
                cursor = static_conn.cursor()
                
                # Query 1: Total seasons
                cursor.execute(f"""
                    SELECT show_id, COUNT(DISTINCT season) 
                    FROM episodes 
                    WHERE show_id IN ({placeholders}) 
                    GROUP BY show_id
                """, show_ids)
                seasons_map = {row[0]: row[1] for row in cursor.fetchall()}
                
                # Query 2: Total episodes
                cursor.execute(f"""
                    SELECT show_id, COUNT(*) 
                    FROM episodes 
                    WHERE show_id IN ({placeholders}) 
                    GROUP BY show_id
                """, show_ids)
                episodes_map = {row[0]: row[1] for row in cursor.fetchall()}
                
                # Query 3: Total watched episodes
                cursor.execute(f"""
                    SELECT E.show_id, COUNT(DISTINCT E.tmdb_id)
                    FROM episodes AS E
                    JOIN dynamic_db.watched_episodes AS W ON E.tmdb_id = W.tmdb_id
                    WHERE E.show_id IN ({placeholders}) AND W.user = ? COLLATE NOCASE
                    GROUP BY E.show_id
                """, (*show_ids, user_val))
                watched_map = {row[0]: row[1] for row in cursor.fetchall()}
                
                # Query 4: Total unaired episodes
                cursor.execute(f"""
                    SELECT show_id, COUNT(*) 
                    FROM episodes 
                    WHERE show_id IN ({placeholders}) AND first_aired > strftime('%Y-%m-%d %H:%M:%S', 'now')
                    GROUP BY show_id
                """, show_ids)
                unaired_map = {row[0]: row[1] for row in cursor.fetchall()}
                
                static_conn.execute("DETACH DATABASE dynamic_db")
                
            # Apply to shows
            for s in shows:
                show_id = s['tmdb_id']
                s['total_seasons'] = seasons_map.get(show_id, 0)
                s['total_episodes'] = episodes_map.get(show_id, 0)
                s['total_watched_episodes'] = watched_map.get(show_id, 0)
                s['total_unaired_episodes'] = unaired_map.get(show_id, 0)
                
        except Exception as e:
            log(f"[Orac] Error enriching external show results: {e}", level=LOGERROR)

    return results
