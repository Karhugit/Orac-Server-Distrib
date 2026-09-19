import sqlite3
from resources.lib.db_utils import db_connect
import requests
import json
import asyncio
from datetime import datetime
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
from resources.lib.trakt_list_sync import run_list_sync
from resources.lib.config_handler import get_config_value
from resources.lib.simkl_api import get_simkl_params, get_simkl_headers, simkl_get

def get_local_list_updated_at(db_path, list_id):
    with db_connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT last_checked FROM lists WHERE list_id=?", (list_id,))
        row = cur.fetchone()
        return row[0] if row else "1970-01-01T00:00:00.000Z"

def fetch_simkl_plantowatch(config_db_path):
    token = get_config_value("simkl.token", config_db_path)
    client_id = get_config_value("simkl.client", config_db_path)
    
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
        list_id = "simkl:watchlist"
        slug = "simkl-watchlist"
        
        # Check settings
        lists_library_settings = {}
        with db_connect(lists_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT list_id, add_to_library FROM lists")
            for row in cursor.fetchall():
                db_list_id, add_to_library = row
                lists_library_settings[db_list_id] = add_to_library
                
        should_sync = lists_library_settings.get(list_id, None)
        username = get_config_value("simkl.user", config_db_path) or "simkl_user"
        
        if should_sync is None:
            # Create list entry if it doesn't exist
            with db_connect(lists_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT OR REPLACE INTO lists (list_id, source, user, owned_by_user, slug, name, description, last_checked, item_count_movies, item_count_shows, add_to_library) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (list_id, "simkl", username, 1, slug, "Simkl Watchlist", "Plan to Watch on Simkl", "1970-01-01T00:00:00.000Z", 0, 0, 1))
                conn.commit()
                should_sync = 1
                
        if should_sync == 0:
            log(f"[Orac] Skipping '{slug}' sync as per user settings", level=LOGINFO)
            return

        # Check /sync/activities first to see if plantowatch moved since last_checked
        last_checked = get_local_list_updated_at(lists_db_path, list_id)
        token = get_config_value("simkl.token", config_db_path)
        client_id = get_config_value("simkl.client", config_db_path)
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
                        return
            except Exception as act_err:
                log(f"[Orac] Simkl activities check for watchlist failed: {act_err}", level=LOGWARNING)

        # Fetch Simkl Data (Synchronous inside Async Task, but it's acceptable here)
        simkl_data = fetch_simkl_plantowatch(config_db_path)
        if not simkl_data:
            return
            
        items = simkl_data["items"]
        counts = simkl_data["counts"]
            
        list_meta = {
            "ids": {"slug": slug},
            "name": "Simkl Watchlist",
            "description": "Plan to Watch on Simkl",
            "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "item_count": counts,
            "user": {"ids": {"slug": username}},
            "source": "simkl",
            "list_id": list_id
        }
        
        log(f"[Orac] **SYNC** Updating {slug} ", level=LOGINFO)
        log(f"[Orac]   -  TV shows : {counts['shows']}", level=LOGINFO)
        log(f"[Orac]   -  Movies   : {counts['movies']}", level=LOGINFO)

        result = await run_list_sync(
            lists_db_path,
            username,
            items,
            slug,
            movies_static_db_path,
            movies_dynamic_db_path,
            tvshows_static_db_path,
            tvshows_dynamic_db_path,
            trakt_queue_path,
            trakt_handler,  # run_list_sync still uses this to fallback logic
            tmdb_handler,
            list_meta
        )
        
        log(f"[Orac] **SYNC** Updated {slug}", level=LOGINFO)
        
    except Exception as e:
        log(f"[Orac] Error in Simkl list sync task: {e}", level=LOGERROR)
