import asyncio
import time
import sqlite3
import json
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
from concurrent.futures import ThreadPoolExecutor, as_completed
from resources.lib.trakt_list_sync import run_list_sync
from resources.lib.trakt_utils import get_trakt_watchlist, get_trakt_favorites
from datetime import datetime, timedelta
import calendar
from resources.lib.db_utils import add_tvshow
from resources.lib.indexing import get_active_external_indexes
from resources.lib.date_utils import parse_date_param


def get_local_list_updated_at(db_path, list_id):
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT last_checked FROM lists WHERE list_id=?", (list_id,))
        row = cur.fetchone()
        return row[0] if row else "1970-01-01T00:00:00.000Z"


async def trakt_list_sync_task(trakt_auth, tmdb_handler, lists_db_path, movie_static_db_path, movie_dynamic_db_path, tvshows_static_db_path, tvshows_dynamic_db_path, 
                                trakt_queue_path, username=None, external_indexes_db_path=None):
    try:
        # Get the add_to_library settings for each list
        lists_library_settings = {}
        with sqlite3.connect(lists_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT list_id, add_to_library FROM lists")
            for row in cursor.fetchall():
                list_id, add_to_library = row
                lists_library_settings[list_id] = add_to_library



        # Get the last activity from trakt
        last_activity_resp = await trakt_auth.get("/sync/last_activities")
        if last_activity_resp is None:
            log(f"[Orac] No response received when fetching last activity", level=LOGERROR)
            return
        if last_activity_resp.status_code != 200:
            log(f"[Orac] Failed to fetch last activity: {last_activity_resp.status_code}", level=LOGWARNING)
            return

        tasks = []
        task_names = []

        # Check if static DBs are empty while we have lists configured for library.
        # This identifies cases where the user wiped the cache.
        force_all_sync = False
        try:
            with sqlite3.connect(movie_static_db_path) as m_conn, sqlite3.connect(tvshows_static_db_path) as s_conn:
                m_count = m_conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                s_count = s_conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
                
                # Check if we have items in list_items that should be in library
                with sqlite3.connect(lists_db_path) as l_conn:
                    library_items_count = l_conn.execute("""
                        SELECT COUNT(*) FROM list_items li 
                        JOIN lists l ON li.list_id = l.list_id 
                        WHERE l.add_to_library = 1
                    """).fetchone()[0]
                    
                    if library_items_count > 0 and (m_count == 0 or s_count == 0):
                        log(f"[Orac] Detected empty static DBs ({m_count} movies, {s_count} shows) while {library_items_count} library items exist. Forcing re-sync.", level=LOGWARNING)
                        force_all_sync = True
        except Exception as e:
            log(f"[Orac] Error checking for empty DBs: {e}", level=LOGDEBUG)

        #get the updated timestamp from the DB, if the watchlist is in lists and library sync is enabled
        last_activity_data = last_activity_resp.json()
        last_activity_str = last_activity_data.get("watchlist", {}).get("updated_at")
        if not last_activity_str:
            log("[Orac] No last activity found for lists", level=LOGWARNING)
            return

        # Check if watchlist should be synced
        list_id = "trakt:personal:watchlist"
        should_sync = lists_library_settings.get(list_id, None)
        
        # If not in settings yet, check if it exists in DB (it might have just been created)
        if should_sync is None:
            with sqlite3.connect(lists_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT add_to_library FROM lists WHERE list_id=?", (list_id,))
                row = cursor.fetchone()
                if row:
                    should_sync = row[0]
                else:
                    # List doesn't exist yet, create it as a personal owned list
                    cursor.execute("INSERT OR REPLACE INTO lists (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (list_id, "trakt", username, 1, "watchlist", "Watchlist", "Watchlist on Trakt", "1970-01-01T00:00:00.000Z", 0, 0, 1))
                    conn.commit()
                    should_sync = 1  # Personal lists should always sync
        
        if should_sync == 1:
            lists_updated_at = get_local_list_updated_at(lists_db_path, list_id) or ""
            if force_all_sync or last_activity_str > lists_updated_at:
                tasks.append(get_trakt_watchlist(trakt_auth))
                task_names.append("watchlist")
        else:
            log("[Orac] Skipping watchlist sync as per user settings", level=LOGINFO)

        #get the updated timestamp from the DB, if the favorites is in lists and library sync is enabled
        last_activity_str = last_activity_data.get("favorites", {}).get("updated_at")
        if not last_activity_str:
            log("[Orac] No last activity found for favorites", level=LOGWARNING)
        else:
            # Check if favorites should be synced
            list_id = "trakt:personal:favorites"
            should_sync = lists_library_settings.get(list_id, None)
            
            # If not in settings yet, check if it exists in DB (it might have just been created)
            if should_sync is None:
                with sqlite3.connect(lists_db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT add_to_library FROM lists WHERE list_id=?", (list_id,))
                    row = cursor.fetchone()
                    if row:
                        should_sync = row[0]
                    else:
                        # List doesn't exist yet, create it as a personal owned list
                        cursor.execute("INSERT OR REPLACE INTO lists (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        (list_id, "trakt", username, 1, "favorites", "Favorites", "Favorites on Trakt", "1970-01-01T00:00:00.000Z", 0, 0, 1))
                        conn.commit()
                        should_sync = 1  # Personal lists should always sync
            
            if should_sync == 1:
                lists_updated_at = get_local_list_updated_at(lists_db_path, list_id) or ""
                if force_all_sync or last_activity_str > lists_updated_at:
                    tasks.append(get_trakt_favorites(trakt_auth))
                    task_names.append("favorites")
            else:
                log("[Orac] Skipping favorites sync as per user settings", level=LOGINFO)


        #get the updated timestamp from the DB, if the collection is in lists
        last_activity_str = last_activity_data.get("movies", {}).get("collected_at")
        if not last_activity_str:
            log("[Orac] No last activity found for collection-movies", level=LOGWARNING)
        else:
            # Check if collection-movies should be synced
            list_id = "trakt:personal:collection-movies"
            should_sync = lists_library_settings.get(list_id, None)
        
        # If not in settings yet, check if it exists in DB (it might have just been created)
        if should_sync is None:
            with sqlite3.connect(lists_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT add_to_library FROM lists WHERE list_id=?", (list_id,))
                row = cursor.fetchone()
                if row:
                    should_sync = row[0]
            # If not in settings yet, check if it exists in DB (it might have just been created)
            if should_sync is None:
                with sqlite3.connect(lists_db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT add_to_library FROM lists WHERE list_id=?", (list_id,))
                    row = cursor.fetchone()
                    if row:
                        should_sync = row[0]
                    else:
                        # List doesn't exist yet, create it as a personal owned list
                        cursor.execute("INSERT OR REPLACE INTO lists (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        (list_id, "trakt", username, 1, "collection-movies", "Collection Movies", "Movie Collection on Trakt", "1970-01-01T00:00:00.000Z", 0, 0, 1))
                        conn.commit()
                        should_sync = 1  # Personal lists should always sync
            
            if should_sync == 1:
                lists_updated_at = get_local_list_updated_at(lists_db_path, list_id) or ""
                if force_all_sync or last_activity_str > lists_updated_at:
                    tasks.append(get_trakt_collection_movies(trakt_auth))
                    task_names.append("collection-movies")
            else:
                log("[Orac] Skipping collection-movies sync as per user settings", level=LOGINFO)

        #get the updated timestamp from the DB, if the collection is in lists
        last_activity_str = last_activity_data.get("episodes", {}).get("collected_at")
        if not last_activity_str:
            log("[Orac] No last activity found for collection-tvshows", level=LOGWARNING)
        else:
            # Check if collection-tvshows should be synced
            list_id = "trakt:personal:collection-tvshows"
            should_sync = lists_library_settings.get(list_id, None)
            
            # If not in settings yet, check if it exists in DB (it might have just been created)
            if should_sync is None:
                 with sqlite3.connect(lists_db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT add_to_library FROM lists WHERE list_id=?", (list_id,))
                    row = cursor.fetchone()
                    if row:
                        should_sync = row[0]
                    else:
                        # List doesn't exist yet, create it as a personal owned list
                        cursor.execute("INSERT OR REPLACE INTO lists (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        (list_id, "trakt", username, 1, "collection-tvshows", "Collection TV Shows", "TV Show Collection on Trakt", "1970-01-01T00:00:00.000Z", 0, 0, 1))
                        conn.commit()
                        should_sync = 1  # Personal lists should always sync
            
            if should_sync == 1:
                lists_updated_at = get_local_list_updated_at(lists_db_path, list_id) or ""
                if force_all_sync or last_activity_str > lists_updated_at:
                    tasks.append(get_trakt_collection_tvshows(trakt_auth))
                    task_names.append("collection-tvshows")
            else:
                log("[Orac] Skipping collection-tvshows sync as per user settings", level=LOGINFO)

        tasks.append(get_my_trakt_lists(trakt_auth, lists_db_path, lists_library_settings, force_sync=force_all_sync))
        task_names.append("my-lists")

        tasks.append(get_liked_trakt_lists(trakt_auth, lists_db_path, lists_library_settings, force_sync=force_all_sync))
        task_names.append("liked-lists")

        tasks.append(get_trakt_generic_lists(trakt_auth, lists_db_path, lists_library_settings))
        task_names.append("trakt-generic-lists")

        if external_indexes_db_path:
            tasks.append(get_external_index_lists(trakt_auth, tmdb_handler, external_indexes_db_path, movie_static_db_path, tvshows_static_db_path, lists_db_path, lists_library_settings))
            task_names.append("external-index-lists")

        results = await asyncio.gather(*tasks)
        watchlist = results[task_names.index("watchlist")] if "watchlist" in task_names else None
        favorites = results[task_names.index("favorites")] if "favorites" in task_names else None
        collection_movies = results[task_names.index("collection-movies")] if "collection-movies" in task_names else None
        collection_tvshows = results[task_names.index("collection-tvshows")] if "collection-tvshows" in task_names else None
        my_lists = results[task_names.index("my-lists")] if "my-lists" in task_names else []
        liked_lists = results[task_names.index("liked-lists")] if "liked-lists" in task_names else []
        trakt_generic_lists = results[task_names.index("trakt-generic-lists")] if "trakt-generic-lists" in task_names else []
        external_index_lists = results[task_names.index("external-index-lists")] if "external-index-lists" in task_names else []

        if not username:
            log("[Orac] Aborting sync: Failed to fetch Trakt username", level=LOGERROR)
            return

        lists_to_sync = []
        custom_lists_to_sync = []

        # Normalize my lists into (slug, list_data)
        if watchlist is not None:
            lists_to_sync.append(normalize_watchlist(watchlist, username))
        if favorites is not None:
            lists_to_sync.append(normalize_favorites(favorites, username))
        if collection_movies is not None:
            lists_to_sync.append(normalize_collection_movies(collection_movies, username))
        if collection_tvshows is not None:
            lists_to_sync.append(normalize_collection_tvshows(collection_tvshows, username))
# Build stats for logging
        if lists_to_sync:
            for slug, list_data in lists_to_sync:
                tvshow_count = sum(1 for item in list_data if item.get("type") == "show")
                movie_count = sum(1 for item in list_data if item.get("type") == "movie")
                if len(list_data) > 0 and movie_count == 0:
                     log(f"[Orac] [DEBUG] First items in {slug} list: {json.dumps(list_data[:2])}", level=LOGDEBUG)
                log(f"[Orac] **SYNC** Updating {slug} ", level=LOGINFO)
                log(f"[Orac]   -  TV shows : {tvshow_count}", level=LOGINFO)
                log(f"[Orac]   -  Movies   : {movie_count}", level=LOGINFO)
        else:
            log("[Orac] **SYNC**No system lists to sync", level=LOGINFO)

        synced = False

        if lists_to_sync:
            for slug, list_data in lists_to_sync:
                result = await run_list_sync(
                    lists_db_path,
                    username,
                    list_data,
                    slug,
                    movie_static_db_path,
                    movie_dynamic_db_path,
                    tvshows_static_db_path,
                    tvshows_dynamic_db_path,
                    trakt_queue_path,
                    trakt_auth,
                    tmdb_handler
                )
                tvshow_count = sum(1 for item in list_data if item.get("type") == "show")
                movie_count = sum(1 for item in list_data if item.get("type") == "movie")
                log(f"[Orac] **SYNC** Updated {slug}", level=LOGINFO)
                log(f"[Orac]   -  TV shows : {tvshow_count}", level=LOGINFO)
                log(f"[Orac]   -  Movies   : {movie_count}", level=LOGINFO)
                synced = True

        # Normalize custom lists into (slug, list_meta, items)
        if my_lists:
            custom_lists_to_sync.extend(normalize_custom_lists(my_lists, username, owned_by_user=True))
        if liked_lists:
            # Normalize liked lists
            custom_lists_to_sync.extend(normalize_custom_lists(liked_lists, username, owned_by_user=False))

        if trakt_generic_lists:
            custom_lists_to_sync.extend(normalize_custom_lists(trakt_generic_lists, username, owned_by_user=False))

        if external_index_lists:
            custom_lists_to_sync.extend(normalize_custom_lists(external_index_lists, username, owned_by_user=False))

# Build stats for logging
        if custom_lists_to_sync:
            for slug, list_meta, list_data in custom_lists_to_sync:
                tvshow_count = sum(1 for item in list_data if item.get("type") == "show")
                movie_count = sum(1 for item in list_data if item.get("type") == "movie")
                log(f"[Orac] **SYNC** Updating {slug} ", level=LOGINFO)
                log(f"[Orac]   -  TV shows : {tvshow_count}", level=LOGINFO)
                log(f"[Orac]   -  Movies   : {movie_count}", level=LOGINFO)
        else:
            log("[Orac] **SYNC**No system lists to sync", level=LOGINFO)
        # Call sync on all lists

        if custom_lists_to_sync:
            for slug, list_meta, list_data in custom_lists_to_sync:
#                log(f"[Orac] Syncing list: {slug}", level=LOGINFO)
                result = await run_list_sync(
                    lists_db_path,
                    username,
                    list_data,
                    slug,
                    movie_static_db_path,
                    movie_dynamic_db_path,
                    tvshows_static_db_path,
                    tvshows_dynamic_db_path,
                    trakt_queue_path,
                    trakt_auth,
                    tmdb_handler,
                    list_meta
                )
                tvshow_count = sum(1 for item in list_data if item.get("type") == "show")
                movie_count = sum(1 for item in list_data if item.get("type") == "movie")
                log(f"[Orac] **SYNC** Updated {slug}", level=LOGINFO)
                log(f"[Orac]   -  TV shows : {tvshow_count}", level=LOGINFO)
                log(f"[Orac]   -  Movies   : {movie_count}", level=LOGINFO)
                synced = True

    except Exception as e:
        log(f"[Orac] Error in Trakt list sync task: {e}", level=LOGERROR)

def normalize_watchlist(watchlist_data, username):
    """
    Normalizes Trakt watchlist items to (slug, list_data) format with 'type' included.
    """
    normalized = []
    item_count = {"movies": 0, "shows": 0}
    for item in watchlist_data:
        media_type = item.get("type")
        if media_type in ("movie", "show"):
            media = item.get(media_type)
            if media:
                normalized.append({
                    "type": media_type,
                    media_type: media
                })
                if media_type == "movie":
                    item_count["movies"] += 1
                elif media_type == "show":
                    item_count["shows"] += 1
            else:
                log(f"[Orac] [DEBUG] Watchlist item missing media data for {media_type}: {json.dumps(item)}", level=LOGDEBUG)
        else:
            log(f"[Orac] [DEBUG] Watchlist item unknown type: {item.get('type')} - {json.dumps(item)}", level=LOGDEBUG)

# List info not present in watchlist_data, so we build it
    list_meta = {
        "ids": {"slug": "watchlist"},
        "name": "Watchlist",
        "description": ("Watchlist on Trakt"),
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "item_count": item_count,
        "user": {"ids": {"slug": username}}
    }


    return "watchlist", normalized

def normalize_favorites(favorites_data, username):
    """
    Normalizes Trakt favorites items to (slug, list_data) format with 'type' included.
    """
    from datetime import datetime
    normalized = []
    item_count = {"movies": 0, "shows": 0}
    for item in favorites_data:
        media_type = item.get("type")
        if media_type in ("movie", "show"):
            media = item.get(media_type)
            if media:
                normalized.append({
                    "type": media_type,
                    media_type: media
                })
                if media_type == "movie":
                    item_count["movies"] += 1
                elif media_type == "show":
                    item_count["shows"] += 1
            else:
                log(f"[Orac] [DEBUG] Favorites item missing media data for {media_type}: {json.dumps(item)}", level=LOGDEBUG)
        else:
            log(f"[Orac] [DEBUG] Favorites item unknown type: {item.get('type')} - {json.dumps(item)}", level=LOGDEBUG)

# List info not present in favorites_data, so we build it
    list_meta = {
        "ids": {"slug": "favorites"},
        "name": "Favorites",
        "description": "Favorites on Trakt",
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "item_count": item_count,
        "user": {"ids": {"slug": username}}
    }

    return "favorites", normalized

def normalize_collection_movies(collection_data, username):
    return "collection-movies", [
        {
            "type": "movie",
            "movie": item["movie"]
        }
        for item in collection_data
        if "movie" in item
    ]

def normalize_collection_tvshows(collection_data, username):
    return "collection-tvshows", [
        {
            "type": "show",
            "show": item["show"]
        }
        for item in collection_data
        if "show" in item
    ]



def normalize_custom_lists(raw_lists: list, user: str, owned_by_user: bool) -> list[tuple]:
    """
    Normalizes a batch of custom Trakt lists into a format suitable for sync.

    :param raw_lists: A list of dicts, each representing a Trakt custom list with `items` included.
    :param user: Trakt username
    :param owned_by_user: Boolean indicating if the user owns these lists.
    :return: List of (slug, list_meta, items) tuples
    """
    normalized = []

    for lst in raw_lists:
        list_slug = lst["ids"]["slug"]
        list_name = lst["name"]
        item_count = {"movies": 0, "shows": 0}

        raw_items = lst.get("items")
        if not isinstance(raw_items, list):
            continue

        items = []
        for entry in raw_items:
            entry_type = entry.get("type")
            if not entry_type:
                continue

            media_data = entry.get(entry_type)
            if not media_data:
                continue

            items.append({
                "type": entry_type,
                entry_type: media_data,
                "listed_at": entry.get("listed_at")
            })

            if entry_type == "movie":
                item_count["movies"] += 1
            elif entry_type == "show":
                item_count["shows"] += 1
            else:
                log(f"[Orac] [DEBUG] Custom list item unknown type: {entry_type} - {json.dumps(entry)}", level=LOGDEBUG)

        user_info = lst.get("user", {"ids": {"slug": user}})

        list_meta = {
            "ids": {"slug": list_slug},
            "name": list_name,
            "description": lst.get("description", ""),
            "updated_at": lst.get("updated_at"),
            "item_count": item_count,
            "user": user_info,
            "owned_by_user": owned_by_user
        }
        
        # If the input list already has a list_id (e.g. from external index), preserve it.
        # Otherwise, the caller (sync logic) will fallback to generating one based on user:slug.
        if "list_id" in lst:
            list_meta["list_id"] = lst["list_id"]
        
        if "source" in lst:
            list_meta["source"] = lst["source"]

        normalized.append((list_slug, list_meta, items))

    return normalized


async def _fetch_all_list_items(trakt_handler, user_slug, slug, page_size=250):
    """
    Fetches ALL items from a Trakt custom list, handling pagination transparently.
    Trakt caps a single response at 250 items (June 2026 hard limit); lists larger
    than that require multiple requests using the ?page= parameter.
    Returns a flat list of all items across all pages.
    """
    all_items = []
    page = 1
    max_pages = 50  # Safety cap to prevent runaway loops

    while page <= max_pages:
        path = f"/users/{user_slug}/lists/{slug}/items?extended=full&limit={page_size}&page={page}"
        resp = await trakt_handler.get(path)

        if resp is None:
            log(f"[Orac] No response fetching page {page} of list '{slug}'", level=LOGERROR)
            break
        if resp.status_code != 200:
            log(f"[Orac] Failed to fetch page {page} of list '{slug}': {resp.status_code}", level=LOGWARNING)
            break

        page_items = resp.json()
        if not page_items:
            break

        all_items.extend(page_items)

        # Check how many pages exist via the Trakt pagination header
        total_pages = int(resp.headers.get("X-Pagination-Page-Count", 1))
        log(f"[Orac] Fetched page {page}/{total_pages} of list '{slug}' ({len(page_items)} items)", level=LOGINFO)

        if page >= total_pages:
            break
        page += 1

    return all_items


async def get_trakt_collection_movies(trakt_handler):
    try:
        all_items = []
        page = 1
        while True:
            collection_resp = await trakt_handler.get(
                f"/users/me/collection/movies?limit=250&page={page}"
            )
            if collection_resp is None:
                log(f"[Orac] No response received when fetching collection movies page {page}", level=LOGERROR)
                break
            if collection_resp.status_code != 200:
                log(f"[Orac] Failed to fetch collection movies: {collection_resp.status_code}", level=LOGWARNING)
                break
            page_items = collection_resp.json()
            all_items.extend(page_items)
            total_pages = int(collection_resp.headers.get("X-Pagination-Page-Count", 1))
            if page >= total_pages:
                break
            page += 1
        return all_items if all_items else None
    except Exception as e:
        log(f"[Orac] Error fetching Trakt collection movies: {e}", level=LOGERROR)
        return None

async def get_trakt_collection_tvshows(trakt_handler):
    try:
        all_items = []
        page = 1
        while True:
            collection_resp = await trakt_handler.get(
                f"/users/me/collection/shows?limit=250&page={page}"
            )
            if collection_resp is None:
                log(f"[Orac] No response received when fetching collection tvshows page {page}", level=LOGERROR)
                break
            if collection_resp.status_code != 200:
                log(f"[Orac] Failed to fetch collection tvshows: {collection_resp.status_code}", level=LOGWARNING)
                break
            page_items = collection_resp.json()
            all_items.extend(page_items)
            total_pages = int(collection_resp.headers.get("X-Pagination-Page-Count", 1))
            if page >= total_pages:
                break
            page += 1
        return all_items if all_items else None
    except Exception as e:
        log(f"[Orac] Error fetching Trakt collection tvshows: {e}", level=LOGERROR)
        return None


async def get_trakt_generic_lists(trakt_handler, lists_db_path, lists_library_settings=None):
    try:
        from resources.lib.trakt_lists import TRAKT_GENERIC_LISTS
        
        lists_to_fetch = TRAKT_GENERIC_LISTS

        tasks = [trakt_handler.get(details["endpoint"], authenticated=details.get("requires_auth", False)) for details in lists_to_fetch.values()]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        enriched_lists = []
        for (slug, details), resp in zip(lists_to_fetch.items(), responses):
            if isinstance(resp, Exception) or resp.status_code != 200:
                log(f"[Orac] Failed to fetch Trakt generic list '{slug}': {resp}", level=LOGWARNING)
                continue

            # If the list is set to be skipped in user settings, continue
            # Use 'generic' type for generic lists to distinguish from personal/official
            list_id = f"trakt:generic:{slug}"
            if lists_library_settings.get(list_id, 0) == 0: # 0 means skip this list or not present
#            if lists_library_settings and lists_library_settings.get(slug, 1) == 0:
                log(f"[Orac] Skipping generic list '{slug}' sync as per user settings", level=LOGINFO)
                # We still need to add the list to the lists table if not present
                with sqlite3.connect(lists_db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1 FROM lists WHERE list_id=?", (list_id,))
                    if not cursor.fetchone():
                        cursor.execute("INSERT OR REPLACE INTO lists (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        (list_id, "trakt", "trakt", 0, slug, details["name"], details["description"], "1970-01-01T00:00:00.000Z", 0, 0, 0))
                        conn.commit()
                continue

            shows_data = resp.json()
            
            list_meta = {
                "name": details["name"],
                "description": details["description"],
                "user": {"ids": {"slug": "trakt"}},
                "ids": {"slug": slug},
                "list_id": list_id, # EXPLICITLY set the ID
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "items": []
            }

            for item_data in shows_data:
                # Handle both wrapped (trending/popular) and unwrapped (recommendations) items
                media_type = item_data.get("type") or details.get("media_type")
                
                if media_type == "show" or "show" in item_data:
                    show_obj = item_data.get("show") or item_data
                    list_item = {"type": "show", "show": show_obj}
                    # Add extra metadata if available
                    for key in ["watchers", "watcher_count", "play_count", "collected_count"]:
                        if key in item_data:
                            list_item[key] = item_data[key]
                    list_meta["items"].append(list_item)
                elif media_type == "movie" or "movie" in item_data:
                    movie_obj = item_data.get("movie") or item_data
                    list_item = {"type": "movie", "movie": movie_obj}
                    # Add extra metadata if available
                    for key in ["watchers", "watcher_count", "play_count", "collected_count"]:
                        if key in item_data:
                            list_item[key] = item_data[key]
                    list_meta["items"].append(list_item)

            if list_meta["items"]:
                enriched_lists.append(list_meta)

        return enriched_lists

    except Exception as e:
        log(f"[Orac] Error fetching trakt generic TV show lists: {e}", level=LOGERROR)
        return []

async def get_my_trakt_lists(trakt_handler, lists_db_path, lists_library_settings, force_sync=False):
    try:

        # 1. Fetch custom lists
        lists_resp = await trakt_handler.get("/users/me/lists")
        if lists_resp is None:
            log(f"[Orac] No response received when fetching My Trakt lists", level=LOGERROR)
            return []
        if lists_resp.status_code != 200:
            log(f"[Orac] Failed to fetch Trakt lists: {lists_resp.status_code}", level=LOGWARNING)
            return []

        lists = lists_resp.json()

        # 2. For each list, fetch items and attach
        enriched_lists = []
        for lst in lists:
            # Check if this list should be synced
            user_slug = lst["user"]["ids"]["slug"]
            slug = lst["ids"]["slug"]
            # Personal lists: trakt:personal:{slug}
            # We assume "My Lists" are personal, owned by the user.
            list_id = f"trakt:personal:{slug}"
            should_sync = lists_library_settings.get(list_id, None)
            
            # If not in settings yet, check if it exists in DB (it might have just been created)
            if should_sync is None:
                with sqlite3.connect(lists_db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT add_to_library FROM lists WHERE list_id=?", (list_id,))
                    row = cursor.fetchone()
                    if row:
                        should_sync = row[0]
                    else:
                        # List doesn't exist yet, create it as a personal owned list
                        cursor.execute("INSERT OR REPLACE INTO lists (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        (list_id, "trakt", user_slug, 1, slug, lst.get("name", ""), lst.get("description", ""), "1970-01-01T00:00:00.000Z", 0, 0, 1))
                        conn.commit()
                        should_sync = 1  # Personal lists should always sync
            
            if should_sync == 0:
                log(f"[Orac] Skipping list '{slug}' sync as per user settings", level=LOGINFO)
                continue
            #get the updated timestamp from the DB, if the watchlist is in lists
            last_activity_str = lst.get("updated_at")
            if not last_activity_str:
                log("[Orac] No last activity found for lists", level=LOGWARNING)
                return
#            user_slug = lst["user"]["ids"]["slug"]
#            slug = lst["ids"]["slug"]
#            list_id = f"{user_slug}:{slug}"
#            if lists_library_settings.get(list_id, 0) == 0: # 0 means skip this list
#                log(f"[Orac] Skipping list '{slug}' sync as per user settings", level=LOGINFO)
#                continue
            lists_updated_at = get_local_list_updated_at(lists_db_path, list_id) or ""
            if force_sync or last_activity_str > lists_updated_at:

                items = await _fetch_all_list_items(trakt_handler, user_slug, slug)
                if items is None:
                    log(f"[Orac] No items received for list '{slug}'", level=LOGERROR)
                    continue
                lst["items"] = items
                log(f"[Orac] Fetched {len(items)} total items for my list '{slug}'", level=LOGINFO)

                enriched_lists.append(lst)

        return enriched_lists

    except Exception as e:
        log(f"[Orac] Error fetching Trakt lists: {e}", level=LOGERROR)
        return []

async def get_liked_trakt_lists(trakt_handler, lists_db_path, lists_library_settings, force_sync=False):
    try:

        my_liked_lists = []
        # 1. Fetch liked lists and add them to the mix
        liked_lists_resp = await trakt_handler.get("/users/me/likes/lists/?limit=100")
        if liked_lists_resp is None:
            log(f"[Orac] No response received when fetching liked Trakt lists", level=LOGERROR)
            return []
        if liked_lists_resp.status_code == 200:
            liked_lists = liked_lists_resp.json()
            for liked in liked_lists:
                item = liked["list"]
                my_liked_lists.append(item)
        else:
            log(f"[Orac] Failed to fetch liked lists: {liked_lists_resp.status_code}", level=LOGWARNING)


        # 2. For each list, fetch items and attach
        enriched_lists = []
        for lst in my_liked_lists:

            #get the updated timestamp from the DB, if the liked list is in lists
            last_activity_str = lst.get("updated_at")
            if not last_activity_str:
                log("[Orac] No last activity found for lists", level=LOGWARNING)
                continue
            user_slug = lst["user"]["ids"]["slug"]
            slug = lst["ids"]["slug"]
            # Liked lists are essentially personal lists of other users or our user.
            # But the schema 'trakt:personal:{slug}' implies uniqueness by slug. 
            # If two users have 'sci-fi', we have a collision if we ignore user?
            # User requirement: "limit server to single user... we probably don't need username".
            # If I like someone else's list 'sci-fi', and I have my own 'sci-fi', they collide.
            # However, the user said "limit the server to a single user, for both trakt and tmdb".
            # This implies ONLY the main user's lists matter?
            # But "liked lists" are lists I like. They might be from others.
            # If I like 'karhu69/sci-fi', and I have 'my/sci-fi'.
            # If we drop username, we have 2 lists named 'sci-fi'.
            # But the user APPROVED removing the username.
            # Assuming "single user" means we only care about ONE set of lists.
            # If there is a collision, maybe that's acceptable or we prefix lived lists differently?
            # Schema was: source:type:identifier.
            # For liked lists, type could be 'liked'? Or 'personal'?
            # Let's stick to 'trakt:personal:{slug}' as per approval.
            list_id = f"trakt:personal:{slug}"

            if lists_library_settings.get(list_id, 0) == 0: # 0 means skip this list
                log(f"[Orac] Skipping liked list '{slug}' sync as per user settings", level=LOGINFO)
                # We still need to add the list to the lists table if not present
                with sqlite3.connect(lists_db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1 FROM lists WHERE list_id=?", (list_id,))
                    if not cursor.fetchone():
                        cursor.execute("INSERT OR REPLACE INTO lists (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        (list_id, "trakt", user_slug, 0, slug, lst.get("name", ""), lst.get("description", ""), "1970-01-01T00:00:00.000Z", 0, 0, 0))
                        conn.commit()
                continue
            lists_updated_at = get_local_list_updated_at(lists_db_path, list_id) or ""
            if force_sync or last_activity_str > lists_updated_at:

                items = await _fetch_all_list_items(trakt_handler, user_slug, slug)
                if items is None:
                    log(f"[Orac] No items received for liked list '{slug}'", level=LOGERROR)
                    continue
                lst["items"] = items
                log(f"[Orac] Fetched {len(items)} total items for liked list '{slug}'", level=LOGINFO)

                enriched_lists.append(lst)

        if not enriched_lists:
            return []
        return enriched_lists

    except Exception as e:
        log(f"[Orac] Error fetching liked Trakt lists: {e}", level=LOGERROR)
        return []


async def get_external_index_lists(trakt_handler, tmdb_handler, external_indexes_db_path, movie_static_db_path, tvshows_static_db_path, lists_db_path, lists_library_settings=None):
    """
    Fetches items for all active external indexes, resolves their Trakt IDs, and formats them as lists.
    """
    active_indexes = get_active_external_indexes(external_indexes_db_path)
    if not active_indexes:
        return []

    log(f"[Orac] Found {len(active_indexes)} active external indexes to sync.", level=LOGINFO)
    
    tasks = []
    for index in active_indexes:
        list_id = index['id'].lower().replace(' ', '-').replace('_', '-')
        
        # We only sync if the setting is explicitly 1. Otherwise, we skip.
        # The original check `if lists_library_settings and ...` was buggy because an empty dict is False,
        # causing the condition to fail and the sync to proceed incorrectly when no lists were configured.
        if not (lists_library_settings and lists_library_settings.get(f"tmdb:index:{list_id}") == 1):
            log(f"[Orac] Skipping external index '{list_id}' as it's not enabled for library sync.", level=LOGINFO)
            # We still need to add the list to the lists table if not present
            with sqlite3.connect(lists_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM lists WHERE list_id=?", (f"tmdb:index:{list_id}",))
                if not cursor.fetchone():
                    cursor.execute("INSERT OR REPLACE INTO lists (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (f"tmdb:index:{list_id}", "tmdb", "tmdb", 0, list_id, index['id'].replace('_', ' ').title(), f"External Index: {index['id']}", "1970-01-01T00:00:00.000Z", 0, 0, 0))
                    conn.commit()
            continue

        if index['media_type'] == 'movie':
            tasks.append(fetch_external_movies_index(trakt_handler, tmdb_handler, index, movie_static_db_path))
        elif index['media_type'] in ['show', 'tvshow']:
             tasks.append(fetch_external_shows_index(trakt_handler, tmdb_handler, index, tvshows_static_db_path))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    valid_lists = []
    for res in results:
        if isinstance(res, dict) and res.get('items'):
            valid_lists.append(res)
        elif isinstance(res, Exception):
            log(f"[Orac] Error processing external index: {res}", level=LOGERROR)

    return valid_lists


async def resolve_tmdb_to_trakt(tmdb_id, media_type, trakt_handler, static_cursor):
    """
    Resolves a TMDb ID to a Trakt ID using local cache first, then Trakt Search API.
    """
    try:
        # 1. Check local DB
        if media_type == 'movie':
            static_cursor.execute("SELECT trakt_id FROM movies WHERE tmdb_id = ?", (tmdb_id,))
        elif media_type == 'show':
             static_cursor.execute("SELECT show_trakt_id FROM shows WHERE show_tmdb_id = ?", (tmdb_id,))
        
        row = static_cursor.fetchone()
        if row and row[0]:
            return row[0]

        # 2. Fallback to Trakt API
        # GET /search/tmdb/:id?type=movie or show
        search_type = 'movie' if media_type == 'movie' else 'show'
        resp = await trakt_handler.get(f"/search/tmdb/{tmdb_id}", params={"type": search_type})
        
        if resp and resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                # The result is a list of search results. Take the first one.
                # structure: [{"type": "movie", "movie": {"ids": {"trakt": 123, ...}}}]
                item = data[0]
                if search_type in item:
                     return item[search_type]['ids'].get('trakt')
        
        return None

    except Exception as e:
        log(f"[Orac] Error resolving TMDB ID {tmdb_id} to Trakt ID: {e}", level=LOGDEBUG)
        return None

async def fetch_external_movies_index(trakt_handler, tmdb_handler, index, movie_static_db_path):
    try:
        params = index['parameters']
        # Parse date parameters
        for key, value in params.items():
            parsed_date = parse_date_param(value)
            if parsed_date:
                params[key] = parsed_date

        # We want to fetch enough items. Discover usually returns 20 per page.
        # Let's fetch 5 pages to get up to 100 items.
        
        all_tmdb_items = []
        for page in range(1, 6):
            p = params.copy()
            p['page'] = page
            data = tmdb_handler.discover_media('movie', p)
            if data and 'results' in data:
                all_tmdb_items.extend(data['results'])
            else:
                break
        
        # Deduplicate by ID just in case
        unique_items = {item['id']: item for item in all_tmdb_items}
        
        resolved_items = []
        with sqlite3.connect(movie_static_db_path) as conn:
            cursor = conn.cursor()
            
            for tmdb_id, item in unique_items.items():
                trakt_id = await resolve_tmdb_to_trakt(tmdb_id, 'movie', trakt_handler, cursor)
                
                # If Trakt ID not found, use a placeholder based on TMDB ID
                # This ensures movies from external indexes are still added to the database
                if not trakt_id:
                    # Use negative TMDB ID as placeholder Trakt ID to avoid conflicts
                    # Real Trakt IDs are always positive integers
                    trakt_id = -(tmdb_id)
                    log(f"[Orac] Could not resolve TMDB ID {tmdb_id} to Trakt. Using placeholder Trakt ID {trakt_id}", LOGDEBUG)
                
                resolved_items.append({
                    "type": "movie",
                    "movie": {
                        "ids": {
                            "trakt": trakt_id,
                            "tmdb": tmdb_id
                        },
                        "title": item.get('title'),
                        "year": int(item.get('release_date')[:4]) if item.get('release_date') else None,
                        # Add minimal metadata required for add_movie if needed, 
                        # but add_movie fetches from TMDB anyway based on ID.
                    }
                })

        slug = index['id'].lower().replace(' ', '-').replace('_', '-')
        list_meta = {
            "ids": {"slug": slug},
            "name": index['id'].replace('_', ' ').title(),  # Use ID as name for now
            "description": f"External Index: {index['id']}",
            "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "user": {"ids": {"slug": "tmdb"}}, # System user
            "list_id": f"tmdb:index:{slug}",
            "source": "tmdb",
            "items": resolved_items
        }
        return list_meta

    except Exception as e:
        log(f"[Orac] Failed to fetch external movie index {index['id']}: {e}", level=LOGERROR)
        return None

async def fetch_external_shows_index(trakt_handler, tmdb_handler, index, tvshows_static_db_path):
    try:
        params = index['parameters']
        # Parse date parameters
        for key, value in params.items():
            parsed_date = parse_date_param(value)
            if parsed_date:
                params[key] = parsed_date

        all_tmdb_items = []
        for page in range(1, 6):
            p = params.copy()
            p['page'] = page
            data = tmdb_handler.discover_media('tv', p)
            if data and 'results' in data:
                all_tmdb_items.extend(data['results'])
            else:
                break
        
        unique_items = {item['id']: item for item in all_tmdb_items}
        
        resolved_items = []
        with sqlite3.connect(tvshows_static_db_path) as conn:
            cursor = conn.cursor()
            
            for tmdb_id, item in unique_items.items():
                trakt_id = await resolve_tmdb_to_trakt(tmdb_id, 'show', trakt_handler, cursor)
                
                # If Trakt ID not found, use a placeholder based on TMDB ID
                # This ensures shows from external indexes are still added to the database
                if not trakt_id:
                    # Use negative TMDB ID as placeholder Trakt ID to avoid conflicts
                    # Real Trakt IDs are always positive integers
                    trakt_id = -(tmdb_id)
                    log(f"[Orac] Could not resolve TMDB ID {tmdb_id} to Trakt. Using placeholder Trakt ID {trakt_id}", LOGDEBUG)
                
                resolved_items.append({
                    "type": "show",
                    "show": {
                        "ids": {
                            "trakt": trakt_id,
                            "tmdb": tmdb_id
                        },
                        "title": item.get('name'),
                        "year": int(item.get('first_air_date')[:4]) if item.get('first_air_date') else None,
                    }
                })

        slug = index['id'].lower().replace(' ', '-').replace('_', '-')
        list_meta = {
            "ids": {"slug": slug},
            "name": index['id'].replace('_', ' ').title(),
            "description": f"External Index: {index['id']}",
            "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "user": {"ids": {"slug": "tmdb"}},
            "list_id": f"tmdb:index:{slug}",
            "source": "tmdb",
            "items": resolved_items
        }
        return list_meta

    except Exception as e:
        log(f"[Orac] Failed to fetch external show index {index['id']}: {e}", level=LOGERROR)
        return None




async def sync_trakt_list_metadata(dynamic_db_path):
    try:
        conn = sqlite3.connect(dynamic_db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.cursor()

        # Get all list_ids from list_items
        cursor.execute("""
            SELECT list_id, media_type, COUNT(*) AS total
            FROM list_items
            GROUP BY list_id, media_type;
        """)

        results = cursor.fetchall()

        list_counts = {}
        for list_id, media_type, item_count in results:
            if list_id not in list_counts:
                list_counts[list_id] = {'movie': 0, 'show': 0}
            list_counts[list_id][media_type] = item_count


        for list_id, counts in list_counts.items():
            # Handle new schema: source:type:identifier
            parts = list_id.split(":")
            if len(parts) >= 3:
                source, ltype, identifier = parts[0], parts[1], parts[2:]
                slug = ":".join(identifier)
                name = slug.replace("-", " ").title()
                
                if source == 'trakt' and ltype == 'generic':
                     username = 'trakt'
                elif source == 'tmdb' and (ltype == 'index' or ltype == 'generic'):
                     username = 'tmdb'
                else:
                     # For personal lists, we can't derive user from ID easily if we dropped it.
                     # Default to current user? Or 'me'?
                     # For now, let's use 'me' as placeholder if unknown.
                     username = 'me'
            else:
                # Legacy: user:slug
                username, slug = list_id.split(":", 1)
                name = slug.replace("-", " ").title()
            
            log(f"[Orac] Updating metadata for {list_id} - movies: {counts.get('movie', 0)}, shows: {counts.get('show', 0)}", level=LOGINFO)

            list_obj = {
                'ids': {'trakt': list_id, 'slug': slug},
                'name': name,
                'item_count_movies': counts.get('movie', 0),
                'item_count_shows': counts.get('show', 0),
                'user': {'ids': {'slug': username}},
            }

            insert_list_metadata(cursor, list_obj, username)

        conn.commit()
        log("[Orac] List metadata updated from local DB", level=LOGINFO)

    except Exception as e:
        log(f"[Orac] Failed to sync list metadata from DB: {e}", level=LOGERROR)
    finally:
        conn.close()

def insert_list_metadata(cursor, list_data, default_user):
    list_id = list_data["ids"]["trakt"]
    log(f"[Orac] Inserting list metadata for {list_id}", level=LOGINFO)
    list_source = "trakt"
    slug = list_data["ids"]["slug"]
    name = list_data.get("name", "")
    description = list_data.get("description", "")
    updated_at = list_data.get("updated_at", "")
    user = list_data.get("user", {}).get("ids", {}).get("slug", default_user)
    item_count_movies = list_data.get("item_count_movies", 0)
    item_count_shows = list_data.get("item_count_shows", 0)

    # Insert the row only if it doesn't already exist
    cursor.execute("""
        INSERT OR IGNORE INTO lists (list_id, source, user, slug, name, description, last_checked, item_count_movies, item_count_shows)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (list_id, list_source, user, slug, name, description, updated_at, item_count_movies, item_count_shows))

    # Always update the item_count_movies and item_count_shows
    cursor.execute("""
        UPDATE lists
        SET item_count_movies = ?, item_count_shows = ?
        WHERE list_id = ?
    """, (item_count_movies, item_count_shows, list_id))


def _update_show_in_thread(show_info, trakt_handler, tmdb_handler, tvshows_static_db_path, trakt_queue_path):
    """
    Worker function to be executed in a thread. It handles the full update for a single show.
    It opens its own database connections to ensure thread safety.
    """
    static_conn_thread = None
    trakt_queue_conn_thread = None
    try:
        show_trakt_id = show_info["ids"]["trakt"]
        show_tmdb_id = show_info["ids"].get("tmdb")
        media_id = {"trakt": show_trakt_id, "tmdb": show_tmdb_id}

        # Each thread needs its own connection and cursors
        static_conn_thread = sqlite3.connect(tvshows_static_db_path, timeout=10)
        static_cursor_thread = static_conn_thread.cursor()
        
        trakt_queue_conn_thread = sqlite3.connect(trakt_queue_path, timeout=10)
        trakt_queue_cursor_thread = trakt_queue_conn_thread.cursor()

        # The dynamic cursor is not used in add_tvshow, so we can pass None
        add_tvshow(
            static_cursor_thread,
            None,  # dynamic_cursor is not used
            trakt_queue_cursor_thread,
            media_id,
            trakt_handler,
            tmdb_handler,
            show_info
        )
        static_conn_thread.commit()
        trakt_queue_conn_thread.commit()
        return True
    except Exception as e:
        log(f"Error updating show '{show_info.get('title', 'Unknown')}' in thread: {e}", level=LOGERROR)
        return False
    finally:
        if static_conn_thread: static_conn_thread.close()
        if trakt_queue_conn_thread: trakt_queue_conn_thread.close()


async def sync_recent_tvshow_updates(trakt_handler, tmdb_handler, tvshows_static_db_path, trakt_queue_path, config_db_path=None):
    """
    Sync TV shows that have been updated on Trakt since the last sync to static DB
    """
    try:
        # Step 1: Determine start date
        start_date_str = None
        if config_db_path:
            from resources.lib.config_handler import get_config_value, update_config_values
            start_date_str = get_config_value('last_tvshow_sync', config_db_path)
            
        if start_date_str:
            try:
                # Ensure it's not older than 7 days to prevent massive payloads or timeouts
                # Trakt dates are like "2026-02-27T00:33:04.000Z", we take the first 19 chars
                last_sync_dt = datetime.strptime(start_date_str[:19], "%Y-%m-%dT%H:%M:%S")
                if (datetime.utcnow() - last_sync_dt).days > 7:
                    start_date_str = (datetime.utcnow() - timedelta(days=7)).isoformat()[:19] + '.000Z'
            except Exception:
                start_date_str = None

        if not start_date_str:
            # Fallback to 24 hours ago if no previous sync time exists
            start_date_str = (datetime.utcnow() - timedelta(hours=24)).isoformat()[:19] + '.000Z'
            
        log(f"[Orac] Checking for shows updated since {start_date_str}", level=LOGINFO)
        
        # Update the sync time for the next run (use current time)
        now_str = datetime.utcnow().isoformat()[:19] + '.000Z'
        if config_db_path:
            from resources.lib.config_handler import update_config_values
            update_config_values({'last_tvshow_sync': now_str}, config_db_path)
        
        # Step 2: Get recently updated shows from Trakt
        # Using the /shows/updates/{start_date} endpoint
        # Trakt expects start_date in YYYY-MM-DD format for this endpoint
        trakt_date = start_date_str[:10]
        params = {
            'extended': 'full',  # Get full show data
            'limit': 100  # Limit to 100 results per page
        }
        
        endpoint = f"/shows/updates/{trakt_date}"
        updates_resp = await trakt_handler.get(endpoint, params=params)
        if updates_resp is None:
            log(f"[Orac] No response received when fetching recent show updates", level=LOGERROR)
            return
        if updates_resp.status_code != 200:
            log(f"[Orac] Failed to fetch recent show updates from Trakt (404 likely due to invalid date/endpoint): {updates_resp.status_code}", level=LOGERROR)
            return
        
        updated_shows = updates_resp.json()
        
        if not updated_shows:
            log(f"[Orac] No shows updated on Trakt since {trakt_date}", level=LOGINFO)
            return
            
        log(f"[Orac] Found {len(updated_shows)} shows updated on Trakt since {trakt_date}", level=LOGINFO)
        
        # Step 3: Open DB connections
        static_conn = sqlite3.connect(tvshows_static_db_path)
        static_cursor = static_conn.cursor()
        
        updated_count = 0
        shows_to_update = []

        # Step 4: Filter shows that need updating (this part is fast and can be done sequentially)
        for show_data in updated_shows:
            updated_at = show_data.get("updated_at")
            show_info = show_data["show"]
            show_trakt_id = show_info["ids"]["trakt"]
            show_title = show_info["title"]

            static_cursor.execute("SELECT last_updated FROM shows WHERE show_trakt_id = ?", (show_trakt_id,))
            existing_show = static_cursor.fetchone()

            if not existing_show:
                log(f"[Orac] Skipping new show not in DB: {show_title}", level=LOGDEBUG)
                continue

            existing_last_updated = existing_show[0]
            if existing_last_updated and updated_at <= existing_last_updated:
                log(f"[Orac] Show {show_title} already up to date in static DB", level=LOGDEBUG)
                continue

            log(f"[Orac] Queuing update for existing show: {show_title}", level=LOGINFO)
            shows_to_update.append(show_info)

        # Close the main connections before starting threads
        static_conn.close()

        # Step 5: Process the filtered shows in parallel
        if shows_to_update:
            with ThreadPoolExecutor(max_workers=5) as executor:
                # Submit all tasks to the executor
                future_to_show = {executor.submit(_update_show_in_thread, show_info, trakt_handler, tmdb_handler, tvshows_static_db_path, trakt_queue_path): show_info for show_info in shows_to_update}
                
                for future in as_completed(future_to_show):
                    if future.result():
                        updated_count += 1

        # Step 6: Update sync timestamp
        with sqlite3.connect(tvshows_static_db_path) as conn:
            cursor = conn.cursor()
            current_time = datetime.utcnow().isoformat() + 'Z'
            cursor.execute("""
                INSERT OR REPLACE INTO sync_metadata (key, value)
                VALUES ('last_updates_sync', ?)
            """, (current_time,))
        
        log(f"[Orac] Recent updates sync complete: {updated_count} shows updated.", level=LOGINFO)
        
    except Exception as e:
        log(f"[Orac] Error during recent updates sync: {e}", level=LOGERROR)
        import traceback
        log(f"[Orac] Traceback: {traceback.format_exc()}", level=LOGERROR)


async def sync_recent_tmdb_tv_changes(trakt_handler, tmdb_handler, tvshows_static_db_path, trakt_queue_path, config_db_path=None):
    """
    Sync TV shows that have been updated on TMDB since the last sync.
    This complements the Trakt sync by ensuring metadata updates are captured directly from the source.
    """
    try:
        # Step 1: Determine start date
        from resources.lib.config_handler import get_config_value, update_config_values
        start_date_str = get_config_value('last_tmdb_tv_sync', config_db_path)
        
        # If no last sync, default to 24 hours ago
        if not start_date_str:
            start_date_str = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d")
        else:
            # TMDB expects YYYY-MM-DD
            start_date_str = start_date_str[:10]

        log(f"[Orac] Checking for TMDB TV changes since {start_date_str}", level=LOGINFO)
        
        # Step 2: Fetch changes from TMDB
        # TMDB /tv/changes returns a list of show IDs
        all_changed_ids = []
        page = 1
        while page <= 10: # Safety limit for pages
            data = tmdb_handler.get_tv_changes(start_date=start_date_str, page=page)
            if not data:
                break
            
            results = data.get("results", [])
            for res in results:
                all_changed_ids.append(res["id"])
            
            if page >= data.get("total_pages", 1):
                break
            page += 1
            
        if not all_changed_ids:
            log(f"[Orac] No TV changes found on TMDB since {start_date_str}", level=LOGINFO)
            # Still update the sync time
            now_str = datetime.utcnow().strftime("%Y-%m-%d")
            update_config_values({'last_tmdb_tv_sync': now_str}, config_db_path)
            return

        log(f"[Orac] Found {len(all_changed_ids)} TV show changes on TMDB", level=LOGINFO)

        # Step 3: Filter changes to only update shows we already have in our DB
        static_conn = sqlite3.connect(tvshows_static_db_path)
        cursor = static_conn.cursor()
        
        # Build lookup for existing shows
        cursor.execute("SELECT show_tmdb_id, show_trakt_id, title FROM shows")
        existing_shows = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        static_conn.close()

        shows_to_update = []
        for tmdb_id in all_changed_ids:
            if tmdb_id in existing_shows:
                trakt_id, title = existing_shows[tmdb_id]
                # Construct a minimal show_info object for add_tvshow
                show_info = {
                    "title": title,
                    "ids": {
                        "trakt": trakt_id,
                        "tmdb": tmdb_id
                    }
                }
                shows_to_update.append(show_info)

        if not shows_to_update:
            log(f"[Orac] None of the changed TMDB shows are in the local database.", level=LOGINFO)
        else:
            log(f"[Orac] Updating {len(shows_to_update)} existing shows found in TMDB changes.", level=LOGINFO)
            updated_count = 0
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_show = {executor.submit(_update_show_in_thread, show_info, trakt_handler, tmdb_handler, tvshows_static_db_path, trakt_queue_path): show_info for show_info in shows_to_update}
                for future in as_completed(future_to_show):
                    if future.result():
                        updated_count += 1
            log(f"[Orac] TMDB TV changes sync complete: {updated_count} shows updated.", level=LOGINFO)

        # Step 4: Update sync timestamp
        now_str = datetime.utcnow().strftime("%Y-%m-%d")
        update_config_values({'last_tmdb_tv_sync': now_str}, config_db_path)

    except Exception as e:
        log(f"[Orac] Error during TMDB TV changes sync: {e}", level=LOGERROR)
        import traceback
        log(f"[Orac] Traceback: {traceback.format_exc()}", level=LOGERROR)
        