import sqlite3
import requests
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
from resources.lib.config_handler import get_config_value

def fetch_mdblist_lists(config_db_path):
    api_key = get_config_value("mdblist_api", config_db_path)
    
    if not api_key or api_key == "empty_setting":
        log("[Orac] Missing or empty MDBList API key. Skipping MDBList sync.", level=LOGINFO)
        return None
        
    try:
        url = f"https://api.mdblist.com/lists/user?apikey={api_key}&sort=ranked&unified=false"
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data

    except Exception as e:
         log(f"[Orac] MDBList fetch error during list sync: {e}", level=LOGERROR)
         return None

def fetch_mdblist_watchlist(config_db_path, api_key):
    try:
        url = f"https://api.mdblist.com/watchlist/items/?apikey={api_key}"
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data

    except Exception as e:
         log(f"[Orac] MDBList fetch error during watchlist sync: {e}", level=LOGERROR)
         return None

def fetch_mdblist_external_lists(config_db_path):
    api_key = get_config_value("mdblist_api", config_db_path)
    
    if not api_key or api_key == "empty_setting":
        return None
        
    try:
        url = f"https://api.mdblist.com/lists/user/external?apikey={api_key}"
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data

    except Exception as e:
         log(f"[Orac] MDBList fetch error during external list sync: {e}", level=LOGERROR)
         return None

def fetch_mdblist_list_items(numeric_list_id, api_key):
    """Fetch items for a specific MDB list by its numeric ID.
    Returns a normalised list of {'tmdb_id', 'imdb_id', 'media_type'} dicts.
    The /lists/{id}/items endpoint returns {"movies": [...], "shows": [...]},
    the same shape as the watchlist endpoint.
    """
    try:
        url = f"https://api.mdblist.com/lists/{numeric_list_id}/items?apikey={api_key}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        log(f"[Orac] MDBList list {numeric_list_id} raw response keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}", level=LOGDEBUG)
        items = []
        if isinstance(data, dict):
            # Format: {"movies": [...], "shows": [...]} — same as watchlist endpoint
            for movie in data.get("movies", []):
                items.append({
                    "tmdb_id": movie.get("tmdb_id"),
                    "imdb_id": movie.get("imdb_id"),
                    "media_type": "movie",
                })
            for show in data.get("shows", []):
                items.append({
                    "tmdb_id": show.get("tmdb_id"),
                    "imdb_id": show.get("imdb_id"),
                    "media_type": "show",
                })
            # Fallback: flat items list with mediatype field
            if not items:
                for item in data.get("items", []):
                    media_type = item.get("mediatype") or item.get("media_type") or item.get("type")
                    if media_type in ("movie", "show"):
                        items.append({
                            "tmdb_id": item.get("tmdb_id"),
                            "imdb_id": item.get("imdb_id"),
                            "media_type": media_type,
                        })
        elif isinstance(data, list):
            # Bare list of items
            for item in data:
                media_type = item.get("mediatype") or item.get("media_type") or item.get("type")
                if media_type in ("movie", "show"):
                    items.append({
                        "tmdb_id": item.get("tmdb_id"),
                        "imdb_id": item.get("imdb_id"),
                        "media_type": media_type,
                    })
        log(f"[Orac] MDBList list {numeric_list_id}: fetched {len(items)} items", level=LOGDEBUG)
        return items
    except Exception as e:
        log(f"[Orac] MDBList fetch error for list {numeric_list_id}: {e}", level=LOGERROR)
        return []

def fetch_mdblist_watchlist_items(api_key):
    """Fetch items from the MDB watchlist (movies + shows).
    Returns a normalised list of {'tmdb_id', 'imdb_id', 'media_type'} dicts.
    """
    try:
        url = f"https://api.mdblist.com/watchlist/items/?apikey={api_key}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items = []
        for movie in data.get("movies", []):
            items.append({
                "tmdb_id": movie.get("tmdb_id"),
                "imdb_id": movie.get("imdb_id"),
                "media_type": "movie",
            })
        for show in data.get("shows", []):
            items.append({
                "tmdb_id": show.get("tmdb_id"),
                "imdb_id": show.get("imdb_id"),
                "media_type": "show",
            })
        return items
    except Exception as e:
        log(f"[Orac] MDBList fetch error for watchlist items: {e}", level=LOGERROR)
        return []

def _normalise_mdblist_items(raw_items):
    """Convert pre-normalised MDB list items into a uniform list of dicts.
    Each dict has: tmdb_id (int|None), imdb_id (str|None), media_type ('movie'|'show').
    raw_items is already [{tmdb_id, imdb_id, media_type}, ...] from the fetch functions.
    """
    normalised = []
    for item in raw_items:
        media_type = item.get("media_type") or item.get("mediatype") or item.get("type")
        if media_type not in ("movie", "show"):
            continue
        tmdb_id = item.get("tmdb_id")
        imdb_id = item.get("imdb_id")
        if not tmdb_id and not imdb_id:
            continue
        try:
            tmdb_id = int(tmdb_id) if tmdb_id else None
        except (ValueError, TypeError):
            tmdb_id = None
        normalised.append({
            "tmdb_id": tmdb_id,
            "imdb_id": imdb_id,
            "media_type": media_type,
        })
    return normalised

def _sync_items_to_list_items(list_id, normalised_items, lists_db_path, movies_static_db_path,
                               movies_dynamic_db_path, tvshows_static_db_path, tvshows_dynamic_db_path,
                               trakt_update_queue_path, trakt_handler, tmdb_handler):
    """Write the normalised items for a list into list_items and ensure metadata exists in static DBs."""
    if not normalised_items:
        log(f"[Orac] MDBList: No items to sync for list {list_id}", level=LOGDEBUG)
        return

    # Step 1: resolve TMDB IDs for items that only have an IMDB ID
    for item in normalised_items:
        if not item["tmdb_id"] and item["imdb_id"] and tmdb_handler:
            try:
                result = tmdb_handler.find_by_external_id(item["imdb_id"], source="imdb_id")
                if result:
                    item["tmdb_id"] = result.get("id")
            except Exception as e:
                log(f"[Orac] MDBList: Could not resolve TMDB ID for {item['imdb_id']}: {e}", level=LOGWARNING)

    # Step 2: insert/update list_items for items we have a TMDB ID for
    with sqlite3.connect(lists_db_path) as conn:
        cursor = conn.cursor()
        # Fetch current items for this list so we can compute diff
        cursor.execute("SELECT media_type, tmdb_id FROM list_items WHERE list_id = ?", (list_id,))
        existing = {(row[0], str(row[1])) for row in cursor.fetchall()}

        incoming = set()
        for item in normalised_items:
            if not item["tmdb_id"]:
                continue
            db_media_type = "movie" if item["media_type"] == "movie" else "show"
            incoming.add((db_media_type, str(item["tmdb_id"])))

        to_add = incoming - existing
        to_remove = existing - incoming

        for db_media_type, tmdb_id_str in to_add:
            cursor.execute(
                "INSERT OR IGNORE INTO list_items (list_id, media_type, trakt_id, tmdb_id) VALUES (?, ?, ?, ?)",
                (list_id, db_media_type, None, tmdb_id_str)
            )

        for db_media_type, tmdb_id_str in to_remove:
            cursor.execute(
                "DELETE FROM list_items WHERE list_id = ? AND media_type = ? AND tmdb_id = ?",
                (list_id, db_media_type, tmdb_id_str)
            )

        conn.commit()

    if to_add:
        log(f"[Orac] MDBList: Added {len(to_add)} items to list_items for {list_id}", level=LOGDEBUG)
    if to_remove:
        log(f"[Orac] MDBList: Removed {len(to_remove)} stale items from list_items for {list_id}", level=LOGDEBUG)

    # Step 3: ensure metadata exists in static DBs for newly added items
    if not (to_add and (movies_static_db_path or tvshows_static_db_path)):
        return

    new_movies = [(db_mt, int(tid)) for db_mt, tid in to_add if db_mt == "movie"]
    new_shows = [(db_mt, int(tid)) for db_mt, tid in to_add if db_mt == "show"]

    # Check which movies are already in the static DB
    if new_movies and movies_static_db_path:
        tmdb_ids = [t for _, t in new_movies]
        placeholders = ",".join(["?"] * len(tmdb_ids))
        with sqlite3.connect(movies_static_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT tmdb_id FROM movies WHERE tmdb_id IN ({placeholders})", tmdb_ids)
            already_present = {row[0] for row in cursor.fetchall()}
        missing_movies = [t for _, t in new_movies if t not in already_present]
    else:
        missing_movies = []

    if new_shows and tvshows_static_db_path:
        tmdb_ids = [t for _, t in new_shows]
        placeholders = ",".join(["?"] * len(tmdb_ids))
        with sqlite3.connect(tvshows_static_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT show_tmdb_id FROM shows WHERE show_tmdb_id IN ({placeholders})", tmdb_ids)
            already_present = {row[0] for row in cursor.fetchall()}
        missing_shows = [t for _, t in new_shows if t not in already_present]
    else:
        missing_shows = []

    if not missing_movies and not missing_shows:
        return

    log(f"[Orac] MDBList: Fetching metadata for {len(missing_movies)} movies and {len(missing_shows)} shows", level=LOGINFO)

    # Fetch missing movie metadata via Trakt (search by TMDB ID)
    def _add_movie_by_tmdb(tmdb_id):
        try:
            if not trakt_handler:
                return
            resp = trakt_handler._get(f"/search/tmdb/{tmdb_id}?type=movie&extended=full")
            if not resp or resp.status_code != 200:
                return
            results = resp.json()
            if not results:
                return
            movie_data = results[0].get("movie")
            if not movie_data:
                return
            from resources.lib.trakt_list_sync import add_movie
            media_id = {"trakt": movie_data["ids"]["trakt"], "tmdb": tmdb_id}
            with sqlite3.connect(movies_static_db_path, timeout=15) as s_conn, \
                 sqlite3.connect(movies_dynamic_db_path, timeout=15) as d_conn:
                s_cur = s_conn.cursor()
                d_cur = d_conn.cursor()
                add_movie(s_cur, d_cur, movie_data, media_id, tmdb_handler)
                s_conn.commit()
                d_conn.commit()
        except Exception as e:
            log(f"[Orac] MDBList: Failed to add movie tmdb_id={tmdb_id}: {e}", level=LOGWARNING)

    def _add_show_by_tmdb(tmdb_id):
        try:
            if not trakt_handler:
                return
            resp = trakt_handler._get(f"/search/tmdb/{tmdb_id}?type=show&extended=full")
            if not resp or resp.status_code != 200:
                return
            results = resp.json()
            if not results:
                return
            show_data = results[0].get("show")
            if not show_data:
                return
            from resources.lib.db_utils import add_tvshow
            media_id = {"trakt": show_data["ids"]["trakt"], "tmdb": tmdb_id}
            with sqlite3.connect(tvshows_static_db_path, timeout=15) as s_conn, \
                 sqlite3.connect(trakt_update_queue_path, timeout=15) as q_conn:
                s_cur = s_conn.cursor()
                q_cur = q_conn.cursor()
                add_tvshow(s_cur, None, q_cur, media_id, trakt_handler, tmdb_handler, show_data)
                s_conn.commit()
                q_conn.commit()
        except Exception as e:
            log(f"[Orac] MDBList: Failed to add show tmdb_id={tmdb_id}: {e}", level=LOGWARNING)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = []
        for tmdb_id in missing_movies:
            futures.append(executor.submit(_add_movie_by_tmdb, tmdb_id))
        for tmdb_id in missing_shows:
            futures.append(executor.submit(_add_show_by_tmdb, tmdb_id))
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                log(f"[Orac] MDBList: metadata fetch thread error: {e}", level=LOGWARNING)


async def mdblist_list_sync_task(config_db_path, lists_db_path,
                                  movies_static_db_path=None, movies_dynamic_db_path=None,
                                  tvshows_static_db_path=None, tvshows_dynamic_db_path=None,
                                  trakt_update_queue_path=None, trakt_handler=None, tmdb_handler=None):
    api_key = get_config_value("mdblist_api", config_db_path)
    if not api_key or api_key == "empty_setting":
        log("[Orac] Missing or empty MDBList API key. Skipping MDBList sync.", level=LOGINFO)
        return

    try:
        # Fetch MDBList Data (Synchronous inside Async Task, but it's acceptable here)
        mdblist_data = fetch_mdblist_lists(config_db_path)
        mdblist_watchlist_data = fetch_mdblist_watchlist(config_db_path, api_key)
        mdblist_external_data = fetch_mdblist_external_lists(config_db_path)
        
        with sqlite3.connect(lists_db_path) as conn:
            cursor = conn.cursor()
            
            # Fetch existing library settings to preserve them
            lists_library_settings = {}
            cursor.execute("SELECT list_id, add_to_library FROM lists WHERE source = 'mdblist'")
            for row in cursor.fetchall():
                db_list_id, add_to_library = row
                lists_library_settings[db_list_id] = add_to_library
                
            config_username = get_config_value("mdblist.user", config_db_path)

            # Collect all lists to process (list_id, numeric_id) for item syncing later
            lists_to_sync_items = []  # list of (list_id, numeric_id_or_None, kind)
                
            if mdblist_data:
                for lst in mdblist_data:
                    # mdblist lists have a unique id
                    numeric_id = lst.get('id')
                    list_id = f"mdblist:{numeric_id}"
                    source = "mdblist"
                    user_name = config_username or lst.get("user_name", "mdblist_user")
                    slug = lst.get("slug", "")
                    name = lst.get("name", "")
                    description = lst.get("description", "")
                    mediatype = lst.get("mediatype", "")
                    item_count_movies = lst.get("items", 0) if mediatype == "movie" else 0
                    item_count_shows = lst.get("items", 0) if mediatype == "show" else 0
                    
                    # Preserve existing add_to_library setting or default to 1 (MDBList items are always synced locally)
                    add_to_library = lists_library_settings.get(list_id, 1)
                    
                    cursor.execute("""
                        INSERT OR REPLACE INTO lists 
                        (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (list_id, source, user_name, 1, slug, name, description, "1970-01-01T00:00:00.000Z", item_count_movies, item_count_shows, add_to_library))

                    if numeric_id:
                        lists_to_sync_items.append((list_id, numeric_id, "regular"))
            
            if mdblist_external_data:
                for lst in mdblist_external_data:
                    # mdblist external lists have a unique id
                    numeric_id = lst.get('id')
                    list_id = f"mdblist:{numeric_id}"
                    source = "mdblist"
                    user_name = config_username or lst.get("user_name", "mdblist_user")
                    slug = lst.get("slug", "")
                    name = lst.get("name", "")
                    description = lst.get("description", "")
                    mediatype = lst.get("mediatype", "")
                    item_count_movies = lst.get("items", 0) if mediatype == "movie" else 0
                    item_count_shows = lst.get("items", 0) if mediatype == "show" else 0
                    
                    # Preserve existing add_to_library setting or default to 1 (MDBList items are always synced locally)
                    add_to_library = lists_library_settings.get(list_id, 1)
                    
                    cursor.execute("""
                        INSERT OR REPLACE INTO lists 
                        (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (list_id, source, user_name, 1, slug, name, description, "1970-01-01T00:00:00.000Z", item_count_movies, item_count_shows, add_to_library))

                    if numeric_id:
                        lists_to_sync_items.append((list_id, numeric_id, "external"))

            if mdblist_watchlist_data:
                # Add the mock MDBList Watchlist entry
                list_id = "mdblist:watchlist"
                source = "mdblist"
                user_name = config_username or mdblist_watchlist_data.get("user_name", "mdblist_user")
                slug = "mdblist-watchlist"
                name = "MDBList Watchlist"
                description = "Plan to Watch on MDBList"
                movies = mdblist_watchlist_data.get("movies", [])
                shows = mdblist_watchlist_data.get("shows", [])
                item_count_movies = len(movies)
                item_count_shows = len(shows)
                add_to_library = lists_library_settings.get(list_id, 1)

                cursor.execute("""
                    INSERT OR REPLACE INTO lists 
                    (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (list_id, source, user_name, 1, slug, name, description, "1970-01-01T00:00:00.000Z", item_count_movies, item_count_shows, add_to_library))

                lists_to_sync_items.append((list_id, None, "watchlist"))

            conn.commit()
            
        count = len(mdblist_data) if mdblist_data else 0
        if mdblist_external_data:
            count += len(mdblist_external_data)
        if mdblist_watchlist_data:
            count += 1
        log(f"[Orac] **SYNC** Updated {count} MDBList list metadata entries", level=LOGINFO)

        # --- Phase 2: Sync list *items* into list_items ---
        # Only do this if we have the necessary DB paths
        can_sync_items = bool(movies_static_db_path and movies_dynamic_db_path and
                              tvshows_static_db_path and tvshows_dynamic_db_path and
                              trakt_update_queue_path)

        if not can_sync_items:
            log("[Orac] MDBList: Skipping list item sync (static DB paths not provided).", level=LOGDEBUG)
            return

        total_items_synced = 0
        for list_id, numeric_id, kind in lists_to_sync_items:
            try:
                if kind == "watchlist":
                    raw_items = fetch_mdblist_watchlist_items(api_key)
                else:
                    raw_items = fetch_mdblist_list_items(numeric_id, api_key)

                normalised = _normalise_mdblist_items(raw_items)
                log(f"[Orac] MDBList: Syncing {len(normalised)} items for list {list_id}", level=LOGDEBUG)

                await asyncio.to_thread(
                    _sync_items_to_list_items,
                    list_id, normalised, lists_db_path,
                    movies_static_db_path, movies_dynamic_db_path,
                    tvshows_static_db_path, tvshows_dynamic_db_path,
                    trakt_update_queue_path, trakt_handler, tmdb_handler
                )
                total_items_synced += len(normalised)

            except Exception as e:
                log(f"[Orac] MDBList: Error syncing items for list {list_id}: {e}", level=LOGERROR)
                import traceback
                log(traceback.format_exc(), level=LOGERROR)

        log(f"[Orac] **SYNC** MDBList item sync complete. Processed {total_items_synced} items across {len(lists_to_sync_items)} lists.", level=LOGINFO)

    except Exception as e:
        log(f"[Orac] Error in MDBList list sync task: {e}", level=LOGERROR)
        import traceback
        log(traceback.format_exc(), level=LOGERROR)
