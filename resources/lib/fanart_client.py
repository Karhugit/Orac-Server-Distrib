import os
import time
import requests
import sqlite3
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
from resources.lib.config_handler import get_fanart_config, get_config_value, update_config_values
from resources.lib.database_manager import DatabaseManager

BASE_URL = "https://webservice.fanart.tv/v3.2"

def _get_api_headers_or_params(api_key):
    """Returns requests parameters for API authentication."""
    return {"api_key": api_key}

def _select_best_asset(assets_list):
    """
    Selects the best asset from a list of Fanart.tv assets.
    Prioritizes English ('en'), then Textless ('00'), and falls back to the absolute most popular.
    """
    if not assets_list:
        return None
    # 1. Try English
    english = [a for a in assets_list if a.get("lang") == "en"]
    if english:
        return english[0]["url"]
    # 2. Try Textless
    textless = [a for a in assets_list if a.get("lang") == "00"]
    if textless:
        return textless[0]["url"]
    # 3. Fallback to most popular
    return assets_list[0]["url"]

def fetch_fanart_movie_assets(tmdb_id, api_key):
    """Fetches movie assets from Fanart.tv for a given TMDB ID."""
    url = f"{BASE_URL}/movies/{tmdb_id}"
    params = _get_api_headers_or_params(api_key)
    try:
        log(f"[Fanart] Fetching movie assets for TMDB ID: {tmdb_id}", level=LOGDEBUG)
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 404:
            log(f"[Fanart] No assets found for movie TMDB ID: {tmdb_id}", level=LOGINFO)
            return None
        resp.raise_for_status()
        data = resp.json()
        
        poster = _select_best_asset(data.get("movieposter"))
        fanart = _select_best_asset(data.get("moviebackground"))
        
        clearlogo = None
        if "hdmovielogo" in data and data["hdmovielogo"]:
            clearlogo = _select_best_asset(data["hdmovielogo"])
        elif "movielogo" in data and data["movielogo"]:
            clearlogo = _select_best_asset(data["movielogo"])
            
        return {"poster": poster, "fanart": fanart, "clearlogo": clearlogo}
    except Exception as e:
        log(f"[Fanart] Error fetching movie assets for {tmdb_id}: {e}", level=LOGWARNING)
        return None

def fetch_fanart_show_assets(tvdb_id, api_key):
    """Fetches TV show assets from Fanart.tv for a given TVDB ID."""
    url = f"{BASE_URL}/tv/{tvdb_id}"
    params = _get_api_headers_or_params(api_key)
    try:
        log(f"[Fanart] Fetching TV show assets for TVDB ID: {tvdb_id}", level=LOGDEBUG)
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 404:
            log(f"[Fanart] No assets found for TV show TVDB ID: {tvdb_id}", level=LOGINFO)
            return None
        resp.raise_for_status()
        data = resp.json()
        
        poster = _select_best_asset(data.get("tvposter"))
        fanart = _select_best_asset(data.get("showbackground"))
        
        clearlogo = None
        if "hdtvlogo" in data and data["hdtvlogo"]:
            clearlogo = _select_best_asset(data["hdtvlogo"])
        elif "clearlogo" in data and data["clearlogo"]:
            clearlogo = _select_best_asset(data["clearlogo"])
            
        return {"poster": poster, "fanart": fanart, "clearlogo": clearlogo}
    except Exception as e:
        log(f"[Fanart] Error fetching TV show assets for {tvdb_id}: {e}", level=LOGWARNING)
        return None

def download_image(url, dest_dir, filename_prefix):
    """Downloads an image from URL to dest_dir with a given prefix, keeping the extension."""
    if not url:
        return None
    try:
        ext = url.split(".")[-1].split("?")[0]
        if len(ext) > 4 or not ext.isalnum():
            ext = "jpg" # Fallback
        filename = f"{filename_prefix}.{ext}"
        filepath = os.path.join(dest_dir, filename)
        
        log(f"[Fanart] Downloading {url} to {filepath}", level=LOGDEBUG)
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        
        with open(filepath, "wb") as f:
            f.write(resp.content)
            
        return filename
    except Exception as e:
        log(f"[Fanart] Failed to download image {url}: {e}", level=LOGERROR)
        return None

def sync_fanart_for_item(item_id, media_type, tmdb_handler, config_db_path, force=False):
    """Fetches and updates Fanart.tv assets for a movie or TV show in the database."""
    config = get_fanart_config(config_db_path)
    if not config["fanart_enabled"] or not config["fanart_api_key"]:
        return False
        
    db_name = "movies_static" if media_type == "movie" else "tvshows_static"
    db_path = DatabaseManager().get_path(db_name)
    if not db_path:
        log(f"[Fanart] No database path found for {db_name}", level=LOGERROR)
        return False
        
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            
            # Check if already updated recently (unless forced)
            if not force:
                if media_type == "movie":
                    cursor.execute("SELECT fanart_last_updated FROM movies WHERE tmdb_id = ?", (item_id,))
                else:
                    cursor.execute("SELECT fanart_last_updated, tvdb_id FROM shows WHERE show_tmdb_id = ?", (item_id,))
                row = cursor.fetchone()
                if row and row[0] is not None:
                    # Skip if updated in the last 24 hours
                    if int(time.time()) - row[0] < 86400:
                        log(f"[Fanart] Skipping {media_type} {item_id} (already updated within 24h)", level=LOGDEBUG)
                        return True
            
            assets = None
            if media_type == "movie":
                assets = fetch_fanart_movie_assets(item_id, config["fanart_api_key"])
            else:
                # Need TVDB ID for TV Shows
                cursor.execute("SELECT tvdb_id, imdb_id FROM shows WHERE show_tmdb_id = ?", (item_id,))
                row = cursor.fetchone()
                tvdb_id = row[0] if row else None
                imdb_id = row[1] if row else None
                
                # Fetch TVDB ID if missing
                if not tvdb_id and tmdb_handler:
                    log(f"[Fanart] TVDB ID missing for show {item_id}, fetching from TMDB...", level=LOGINFO)
                    ext_ids = tmdb_handler._get(f"/tv/{item_id}/external_ids")
                    if ext_ids:
                        tvdb_id = ext_ids.get("tvdb_id")
                        if tvdb_id:
                            cursor.execute("UPDATE shows SET tvdb_id = ? WHERE show_tmdb_id = ?", (str(tvdb_id), item_id))
                            conn.commit()
                            log(f"[Fanart] Updated TVDB ID to {tvdb_id} for show {item_id}", level=LOGINFO)
                            
                if tvdb_id:
                    assets = fetch_fanart_show_assets(tvdb_id, config["fanart_api_key"])
                else:
                    log(f"[Fanart] Cannot sync show {item_id} due to missing TVDB ID.", level=LOGWARNING)
                    return False
            
            if not assets:
                # Mark as updated even if no assets found to avoid looping
                timestamp = int(time.time())
                if media_type == "movie":
                    cursor.execute("UPDATE movies SET fanart_last_updated = ? WHERE tmdb_id = ?", (timestamp, item_id))
                else:
                    cursor.execute("UPDATE shows SET fanart_last_updated = ? WHERE show_tmdb_id = ?", (timestamp, item_id))
                conn.commit()
                return False
                
            poster_path = None
            fanart_path = None
            clearlogo_path = None
            
            if config["fanart_storage_mode"] == "Local":
                # Create directory resources/assets/images/{media_type}_{item_id}/
                lib_dir = os.path.dirname(os.path.abspath(__file__))
                resources_dir = os.path.dirname(lib_dir)
                dest_dir = os.path.join(resources_dir, "assets", "images", f"{media_type}_{item_id}")
                os.makedirs(dest_dir, exist_ok=True)
                
                if assets.get("poster"):
                    f_name = download_image(assets["poster"], dest_dir, "poster")
                    if f_name:
                        poster_path = f"{media_type}_{item_id}/{f_name}"
                if assets.get("fanart"):
                    f_name = download_image(assets["fanart"], dest_dir, "fanart")
                    if f_name:
                        fanart_path = f"{media_type}_{item_id}/{f_name}"
                if assets.get("clearlogo"):
                    f_name = download_image(assets["clearlogo"], dest_dir, "clearlogo")
                    if f_name:
                        clearlogo_path = f"{media_type}_{item_id}/{f_name}"
            else:
                # URL Mode
                poster_path = assets.get("poster")
                fanart_path = assets.get("fanart")
                clearlogo_path = assets.get("clearlogo")
                
            timestamp = int(time.time())
            
            if media_type == "movie":
                cursor.execute("""
                    UPDATE movies SET
                        fanart_poster_path = ?,
                        fanart_fanart_path = ?,
                        fanart_clearlogo_path = ?,
                        fanart_last_updated = ?
                    WHERE tmdb_id = ?
                """, (poster_path, fanart_path, clearlogo_path, timestamp, item_id))
            else:
                cursor.execute("""
                    UPDATE shows SET
                        fanart_poster_path = ?,
                        fanart_fanart_path = ?,
                        fanart_clearlogo_path = ?,
                        fanart_last_updated = ?
                    WHERE show_tmdb_id = ?
                """, (poster_path, fanart_path, clearlogo_path, timestamp, item_id))
                
            conn.commit()
            log(f"[Fanart] Successfully synced assets for {media_type} {item_id}", level=LOGINFO)
            return True
    except Exception as e:
        log(f"[Fanart] Error syncing fanart for {media_type} {item_id}: {e}", level=LOGERROR)
        return False

def run_fanart_latest_sync(config_db_path, tmdb_handler):
    """Runs the background Latest delta sync for movies and TV shows."""
    config = get_fanart_config(config_db_path)
    if not config["fanart_enabled"] or not config["fanart_api_key"]:
        log("[Fanart] Sync skipped: Fanart.tv integration is not enabled or API key is missing.", level=LOGINFO)
        return False
        
    try:
        # 1. Get the global sync timestamp
        # Default to 3 days ago if not set
        global_ts = get_config_value("fanart_global_sync_timestamp", config_db_path)
        if global_ts is None:
            # 3 days ago in UNIX timestamp
            global_ts = int(time.time()) - 3 * 86400
        else:
            global_ts = int(global_ts)
            
        current_time = int(time.time())
        log(f"[Fanart] Running delta sync since timestamp {global_ts} (Date: {time.ctime(global_ts)})", level=LOGINFO)
        
        # 2. Query Fanart.tv latest movies
        movie_url = f"{BASE_URL}/movies/latest"
        movie_params = {"api_key": config["fanart_api_key"], "date": global_ts}
        changed_movie_ids = []
        try:
            resp = requests.get(movie_url, params=movie_params, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    changed_movie_ids = [str(item.get("id")) for item in data if item.get("id")]
                elif isinstance(data, dict):
                    changed_movie_ids = [str(k) for k in data.keys()]
            else:
                log(f"[Fanart] Latest movies request failed with status: {resp.status_code}", level=LOGWARNING)
        except Exception as e:
            log(f"[Fanart] Error fetching latest movies: {e}", level=LOGWARNING)
            
        # 3. Query Fanart.tv latest TV shows
        tv_url = f"{BASE_URL}/tv/latest"
        tv_params = {"api_key": config["fanart_api_key"], "date": global_ts}
        changed_tv_ids = []
        try:
            resp = requests.get(tv_url, params=tv_params, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    changed_tv_ids = [str(item.get("id")) for item in data if item.get("id")]
                elif isinstance(data, dict):
                    changed_tv_ids = [str(k) for k in data.keys()]
            else:
                log(f"[Fanart] Latest TV shows request failed with status: {resp.status_code}", level=LOGWARNING)
        except Exception as e:
            log(f"[Fanart] Error fetching latest TV shows: {e}", level=LOGWARNING)
            
        log(f"[Fanart] Found {len(changed_movie_ids)} changed movies and {len(changed_tv_ids)} changed TV shows on Fanart.tv", level=LOGINFO)
        
        # 4. Cross-reference changed movies against DB
        movies_static_path = DatabaseManager().get_path("movies_static")
        movies_to_update = []
        if changed_movie_ids and movies_static_path:
            with sqlite3.connect(movies_static_path) as conn:
                cursor = conn.cursor()
                placeholders = ",".join("?" for _ in changed_movie_ids)
                # Check match on tmdb_id or imdb_id
                cursor.execute(f"""
                    SELECT tmdb_id FROM movies 
                    WHERE tmdb_id IN ({placeholders}) OR imdb_id IN ({placeholders})
                """, changed_movie_ids + changed_movie_ids)
                movies_to_update = [row[0] for row in cursor.fetchall()]
                
        # 5. Cross-reference changed TV shows against DB
        tvshows_static_path = DatabaseManager().get_path("tvshows_static")
        shows_to_update = []
        if changed_tv_ids and tvshows_static_path:
            with sqlite3.connect(tvshows_static_path) as conn:
                cursor = conn.cursor()
                placeholders = ",".join("?" for _ in changed_tv_ids)
                # Check match on show_tmdb_id, imdb_id, or tvdb_id
                cursor.execute(f"""
                    SELECT show_tmdb_id FROM shows 
                    WHERE show_tmdb_id IN ({placeholders}) OR imdb_id IN ({placeholders}) OR tvdb_id IN ({placeholders})
                """, changed_tv_ids + changed_tv_ids + changed_tv_ids)
                shows_to_update = [row[0] for row in cursor.fetchall()]
                
        log(f"[Fanart] Database matches to refresh: {len(movies_to_update)} movies, {len(shows_to_update)} TV shows", level=LOGINFO)
        
        # 6. Perform asset refresh
        refreshed_count = 0
        for tmdb_id in movies_to_update:
            if sync_fanart_for_item(tmdb_id, "movie", tmdb_handler, config_db_path, force=True):
                refreshed_count += 1
                
        for tmdb_id in shows_to_update:
            if sync_fanart_for_item(tmdb_id, "show", tmdb_handler, config_db_path, force=True):
                refreshed_count += 1
                
        log(f"[Fanart] Delta sync complete. Refreshed {refreshed_count} database items.", level=LOGINFO)
        
        # 7. Update global sync timestamp to current execution start time
        update_config_values({"fanart_global_sync_timestamp": str(current_time)}, config_db_path)
        return True
    except Exception as e:
        log(f"[Fanart] Error in background latest sync: {e}", level=LOGERROR)
        return False
