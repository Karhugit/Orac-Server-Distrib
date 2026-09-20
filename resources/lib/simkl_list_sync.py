import sqlite3
from resources.lib.db_utils import db_connect
import requests
import json
import asyncio
from datetime import datetime
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
from resources.lib.trakt_list_sync import run_list_sync
from resources.lib.config_handler import get_config_value
from resources.lib.simkl_api import (
    get_simkl_params,
    get_simkl_headers,
    simkl_get,
    get_effective_simkl_client_id,
    SIMKL_APP_NAME
)
from resources.lib.simkl_lists import SIMKL_GENERIC_LISTS
from resources.lib.version import __version__


def get_local_list_updated_at(db_path, list_id):
    with db_connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT last_checked FROM lists WHERE list_id=?", (list_id,))
        row = cur.fetchone()
        return row[0] if row else "1970-01-01T00:00:00.000Z"


def cleanup_list_items(db_path, list_id):
    """Removes all items for a given list_id when not in library to keep DB clean."""
    try:
        with db_connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
            conn.commit()
    except Exception as e:
        log(f"[Orac] Error cleaning up list items for {list_id}: {e}", level=LOGERROR)


def update_simkl_generic_list_metadata(db_path, list_def, count_movies=0, count_shows=0):
    """Registers or updates a Simkl generic list's metadata in the lists table."""
    try:
        slug = list_def['slug']
        list_id = f"simkl:generic:{slug}"
        name = list_def['name']
        description = list_def['description']

        with db_connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO lists (list_id, source, user, slug, name, description, last_checked, item_count_movies, item_count_shows, owned_by_user, add_to_library)
                VALUES (?, 'simkl', 'simkl', ?, ?, ?, ?, ?, ?, 0, 0)
                ON CONFLICT(list_id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    last_checked=excluded.last_checked,
                    item_count_movies=excluded.item_count_movies,
                    item_count_shows=excluded.item_count_shows
            """, (
                list_id, slug, name, description,
                datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                count_movies, count_shows
            ))
            conn.commit()
    except Exception as e:
        log(f"[Orac] Error updating Simkl generic list metadata for {list_def['slug']}: {e}", level=LOGERROR)


def fetch_simkl_generic_items(endpoint, client_id):
    """Fetches static CDN JSON list items from data.simkl.in."""
    url = f"https://data.simkl.in/{endpoint}?client_id={client_id}&app-name={SIMKL_APP_NAME}&app-version={__version__}"
    headers = {"User-Agent": f"{SIMKL_APP_NAME}/{__version__}"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
    except Exception as e:
        log(f"[Orac] Error fetching Simkl generic list feed ({endpoint}): {e}", level=LOGERROR)
    return []


def fetch_simkl_plantowatch(config_db_path):
    from resources.lib.simkl_api import ensure_valid_simkl_token, get_effective_simkl_client_id
    token = ensure_valid_simkl_token(config_db_path) or get_config_value("simkl.token", config_db_path)
    client_id = get_effective_simkl_client_id(config_db_path)
    
    if not token or not client_id:
        log("[Orac] Missing Simkl credentials. Skipping Simkl watchlist sync.", level=LOGINFO)
        return None
        
    headers = get_simkl_headers(token=token, client_id=client_id)
    params = get_simkl_params(client_id, {"extended": "ids_only"})
    
    plantowatch_items = []
    item_count = {"movies": 0, "shows": 0}
    
    try:
        # 1. Fetch movies plantowatch
        m_resp = simkl_get('https://api.simkl.com/sync/all-items/movies/plantowatch', params=params, headers=headers, timeout=20)
        if m_resp.status_code == 200:
            m_data = m_resp.json() if m_resp.text else {}
            for movie in m_data.get('movies', []):
                ids = movie.get('movie', {}).get('ids', {})
                tmdb_id = ids.get('tmdb')
                trakt_id = ids.get('trakt')
                if tmdb_id:
                    if not trakt_id:
                        trakt_id = -int(tmdb_id)
                    plantowatch_items.append({
                        "type": "movie",
                        "movie": {"ids": {"tmdb": int(tmdb_id), "trakt": int(trakt_id)}}
                    })
                    item_count["movies"] += 1

        # 2. Fetch shows plantowatch
        s_resp = simkl_get('https://api.simkl.com/sync/all-items/shows/plantowatch', params=params, headers=headers, timeout=20)
        if s_resp.status_code == 200:
            s_data = s_resp.json() if s_resp.text else {}
            for show in s_data.get('shows', []):
                ids = show.get('show', {}).get('ids', {})
                show_tmdb_id = ids.get('tmdb')
                show_trakt_id = ids.get('trakt')
                if show_tmdb_id:
                    if not show_trakt_id:
                        show_trakt_id = -int(show_tmdb_id)
                    plantowatch_items.append({
                        "type": "show",
                        "show": {"ids": {"tmdb": int(show_tmdb_id), "trakt": int(show_trakt_id)}}
                    })
                    item_count["shows"] += 1
        
        return {
            "items": plantowatch_items,
            "counts": item_count
        }

    except Exception as e:
         log(f"[Orac] Simkl fetch error during watchlist sync: {e}", level=LOGERROR)
         return None


async def simkl_list_sync_task(config_db_path, lists_db_path, trakt_handler, tmdb_handler, movies_static_db_path, movies_dynamic_db_path, tvshows_static_db_path, tvshows_dynamic_db_path, trakt_queue_path):
    try:
        from resources.lib.simkl_api import ensure_valid_simkl_token, get_effective_simkl_client_id
        client_id = get_effective_simkl_client_id(config_db_path)

        # Check existing list library settings
        lists_library_settings = {}
        with db_connect(lists_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT list_id, add_to_library FROM lists")
            for row in cursor.fetchall():
                db_list_id, add_to_library = row
                lists_library_settings[db_list_id] = add_to_library

        # -------------------------------------------------------------
        # 1. User Watchlist Sync (simkl:watchlist)
        # -------------------------------------------------------------
        watchlist_id = "simkl:watchlist"
        watchlist_slug = "simkl-watchlist"
        should_sync_wl = lists_library_settings.get(watchlist_id, None)
        username = get_config_value("simkl.user", config_db_path) or "simkl_user"

        if should_sync_wl is None:
            with db_connect(lists_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO lists (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (watchlist_id, "simkl", username, 1, watchlist_slug, "Simkl Watchlist", "Plan to Watch on Simkl", "1970-01-01T00:00:00.000Z", 0, 0, 1)
                )
                conn.commit()
                should_sync_wl = 1

        if should_sync_wl == 1:
            last_checked = get_local_list_updated_at(lists_db_path, watchlist_id)
            token = ensure_valid_simkl_token(config_db_path) or get_config_value("simkl.token", config_db_path)
            skip_wl_fetch = False

            if token and client_id and last_checked and last_checked != "1970-01-01T00:00:00.000Z":
                try:
                    act_resp = simkl_get(
                        'https://api.simkl.com/sync/activities',
                        params=get_simkl_params(client_id),
                        headers=get_simkl_headers(token=token, client_id=client_id),
                        timeout=10
                    )
                    if act_resp.status_code == 200:
                        act_data = act_resp.json()
                        tv_ptw = act_data.get("tv_shows", {}).get("plantowatch", "") or ""
                        mov_ptw = act_data.get("movies", {}).get("plantowatch", "") or ""
                        if (not tv_ptw or tv_ptw <= last_checked) and (not mov_ptw or mov_ptw <= last_checked):
                            log(f"[Orac] Simkl watchlist unchanged since {last_checked} — skipping sync.", level=LOGINFO)
                            skip_wl_fetch = True
                except Exception as act_err:
                    log(f"[Orac] Simkl activities check for watchlist failed: {act_err}", level=LOGWARNING)

            if not skip_wl_fetch:
                simkl_data = fetch_simkl_plantowatch(config_db_path)
                if simkl_data:
                    items = simkl_data["items"]
                    counts = simkl_data["counts"]
                    list_meta = {
                        "ids": {"slug": watchlist_slug},
                        "name": "Simkl Watchlist",
                        "description": "Plan to Watch on Simkl",
                        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "item_count": counts,
                        "user": {"ids": {"slug": username}},
                        "source": "simkl",
                        "list_id": watchlist_id
                    }

                    log(f"[Orac] **SYNC** Updating {watchlist_slug}", level=LOGINFO)
                    log(f"[Orac]   -  TV shows : {counts['shows']}", level=LOGINFO)
                    log(f"[Orac]   -  Movies   : {counts['movies']}", level=LOGINFO)

                    await run_list_sync(
                        lists_db_path,
                        username,
                        items,
                        watchlist_slug,
                        movies_static_db_path,
                        movies_dynamic_db_path,
                        tvshows_static_db_path,
                        tvshows_dynamic_db_path,
                        trakt_queue_path,
                        trakt_handler,
                        tmdb_handler,
                        list_meta
                    )
                    log(f"[Orac] **SYNC** Updated {watchlist_slug}", level=LOGINFO)

        # -------------------------------------------------------------
        # 2. Simkl Generic Lists (Trending & Popular)
        # -------------------------------------------------------------
        for list_def in SIMKL_GENERIC_LISTS:
            slug = list_def['slug']
            list_id = f"simkl:generic:{slug}"
            media_type = list_def['type']
            is_in_library = lists_library_settings.get(list_id, 0) == 1

            if not is_in_library:
                count_m = 100 if media_type == 'movie' else 0
                count_s = 100 if media_type == 'show' else 0
                update_simkl_generic_list_metadata(lists_db_path, list_def, count_movies=count_m, count_shows=count_s)
                cleanup_list_items(lists_db_path, list_id)
                log(f"[Orac] Simkl generic list '{slug}' not in library. Updated metadata only.", level=LOGDEBUG)
                continue

            # In library: fetch items from Simkl CDN feed
            raw_items = fetch_simkl_generic_items(list_def['endpoint'], client_id)
            if not raw_items:
                log(f"[Orac] No items returned for Simkl generic list '{slug}'", level=LOGWARNING)
                continue

            normalized_items = []
            for raw in raw_items:
                ids = raw.get("ids", {})
                tmdb_id = ids.get("tmdb")
                trakt_id = ids.get("trakt")
                if not tmdb_id:
                    continue
                if not trakt_id:
                    trakt_id = -int(tmdb_id)

                if media_type == "movie":
                    normalized_items.append({
                        "type": "movie",
                        "movie": {"ids": {"tmdb": int(tmdb_id), "trakt": int(trakt_id)}}
                    })
                else:
                    normalized_items.append({
                        "type": "show",
                        "show": {"ids": {"tmdb": int(tmdb_id), "trakt": int(trakt_id)}}
                    })

            counts = {
                "movies": len(normalized_items) if media_type == "movie" else 0,
                "shows": len(normalized_items) if media_type == "show" else 0
            }
            list_meta = {
                "ids": {"slug": slug},
                "name": list_def["name"],
                "description": list_def["description"],
                "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "item_count": counts,
                "user": {"ids": {"slug": "simkl"}},
                "source": "simkl",
                "owned_by_user": False,
                "list_id": list_id
            }

            log(f"[Orac] **SYNC** Updating Simkl generic list {slug} ({len(normalized_items)} items)", level=LOGINFO)
            await run_list_sync(
                lists_db_path,
                "simkl",
                normalized_items,
                slug,
                movies_static_db_path,
                movies_dynamic_db_path,
                tvshows_static_db_path,
                tvshows_dynamic_db_path,
                trakt_queue_path,
                trakt_handler,
                tmdb_handler,
                list_meta
            )
            log(f"[Orac] **SYNC** Updated Simkl generic list {slug}", level=LOGINFO)

    except Exception as e:
        log(f"[Orac] Error in Simkl list sync task: {e}", level=LOGERROR)
