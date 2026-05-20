import sqlite3
import requests
import json
import asyncio
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

async def mdblist_list_sync_task(config_db_path, lists_db_path):
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
                
            if mdblist_data:
                for lst in mdblist_data:
                    # mdblist lists have a unique id
                    list_id = f"mdblist:{lst.get('id')}"
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
            
            if mdblist_external_data:
                for lst in mdblist_external_data:
                    # mdblist external lists have a unique id
                    list_id = f"mdblist:{lst.get('id')}"
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

            conn.commit()
            
        count = len(mdblist_data) if mdblist_data else 0
        if mdblist_external_data:
            count += len(mdblist_external_data)
        if mdblist_watchlist_data:
            count += 1
        log(f"[Orac] **SYNC** Updated {count} MDBList lists", level=LOGINFO)
        
    except Exception as e:
        log(f"[Orac] Error in MDBList list sync task: {e}", level=LOGERROR)
