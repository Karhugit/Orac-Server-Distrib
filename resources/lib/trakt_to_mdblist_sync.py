import sqlite3
import requests
import json
import asyncio
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
from resources.lib.config_handler import get_config_value

def _get_or_create_mdblist_list(name, is_private, api_key, existing_map):
    """
    Checks if an MDBList list with `name` exists in `existing_map` (case-insensitive).
    If not, creates a new static list on MDBList via POST /lists/user/add.
    Returns the MDBList list_id or None if failed.
    """
    key = name.strip().lower()
    if key in existing_map:
        return existing_map[key]

    try:
        url = f"https://api.mdblist.com/lists/user/add?apikey={api_key}"
        payload = {"name": name, "private": bool(is_private)}
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code in (200, 201):
            data = resp.json()
            list_id = data.get("id")
            if list_id:
                log(f"[Trakt->MDBList] Created new static list '{name}' on MDBList (ID: {list_id})", level=LOGINFO)
                existing_map[key] = list_id
                return list_id
            log(f"[Trakt->MDBList] Create list response missing 'id': {resp.text}", level=LOGWARNING)
        else:
            log(f"[Trakt->MDBList] Failed to create list '{name}' on MDBList: {resp.status_code} {resp.text}", level=LOGWARNING)
    except Exception as e:
        log(f"[Trakt->MDBList] Error creating list '{name}' on MDBList: {e}", level=LOGERROR)

    return None


def _push_items_to_mdblist(list_id, movies_payload, shows_payload, api_key):
    """
    Pushes movies and shows payloads to an MDBList static list.
    Endpoint: POST /lists/{listid}/items/add
    """
    if not list_id or (not movies_payload and not shows_payload):
        return False

    try:
        url = f"https://api.mdblist.com/lists/{list_id}/items/add?apikey={api_key}"
        payload = {
            "movies": movies_payload,
            "shows": shows_payload
        }
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            added_movies = result.get('added', {}).get('movies', 0)
            added_shows = result.get('added', {}).get('shows', 0)
            log(f"[Trakt->MDBList] Pushed to MDBList list {list_id}: {added_movies} movies added, {added_shows} shows added", level=LOGINFO)
            return True
        else:
            log(f"[Trakt->MDBList] Failed to push items to MDBList list {list_id}: {resp.status_code} {resp.text}", level=LOGWARNING)
    except Exception as e:
        log(f"[Trakt->MDBList] Error pushing items to MDBList list {list_id}: {e}", level=LOGERROR)

    return False


async def sync_trakt_lists_to_mdblist_task(config_db_path, trakt_handler, lists_db_path=None, tmdb_handler=None):
    """
    Asynchronously syncs all Trakt custom user lists and Trakt Movie/TV Show Collections to MDBList.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, sync_trakt_lists_to_mdblist, config_db_path, trakt_handler, lists_db_path, tmdb_handler)


def sync_trakt_lists_to_mdblist(config_db_path, trakt_handler, lists_db_path=None, tmdb_handler=None):
    """
    Synchronizes Trakt user custom lists and Trakt Collections to MDBList static lists.
    """
    api_key = get_config_value("mdblist_api", config_db_path)
    if not api_key or api_key == "empty_setting":
        log("[Trakt->MDBList] Missing or empty MDBList API key. Skipping Trakt->MDBList list sync.", level=LOGINFO)
        return False

    from resources.lib.config_handler import get_trakt_access_token, get_trakt_client_id
    t_token = get_trakt_access_token(config_db_path) if config_db_path else None
    t_client = get_trakt_client_id(config_db_path) if config_db_path else None
    is_trakt_authed = bool(t_token and t_token not in ("empty_setting", "") and t_client and t_client not in ("empty_setting", ""))
    if not is_trakt_authed:
        log("[Trakt->MDBList] Trakt is not authorized. Skipping Trakt->MDBList list sync.", level=LOGINFO)
        return False


    log("[Trakt->MDBList] Starting Trakt lists and collection sync to MDBList...", level=LOGINFO)

    # 1. Fetch user's existing MDBList lists to build lookup map
    from resources.lib.mdblist_list_sync import fetch_mdblist_lists
    mdblist_lists = fetch_mdblist_lists(config_db_path) or []
    existing_mdblist_map = {}
    for l in mdblist_lists:
        l_name = l.get("name")
        l_id = l.get("id")
        if l_name and l_id:
            existing_mdblist_map[l_name.strip().lower()] = l_id

    # 2. Fetch Trakt custom user lists
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        trakt_lists_resp = loop.run_until_complete(trakt_handler.get("/users/me/lists"))
        loop.close()
    except Exception as e:
        log(f"[Trakt->MDBList] Error fetching Trakt custom lists: {e}", level=LOGERROR)
        trakt_lists_resp = None

    if trakt_lists_resp and trakt_lists_resp.status_code == 200:
        custom_lists = trakt_lists_resp.json()
        log(f"[Trakt->MDBList] Found {len(custom_lists)} custom lists on Trakt", level=LOGINFO)

        for t_list in custom_lists:
            list_name = t_list.get("name")
            if not list_name:
                continue

            is_private = (t_list.get("privacy") == "private")
            list_id_or_slug = t_list.get("ids", {}).get("slug") or t_list.get("ids", {}).get("trakt")
            if not list_id_or_slug:
                continue

            # Fetch items for this Trakt list
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                items_resp = loop.run_until_complete(trakt_handler.get(f"/users/me/lists/{list_id_or_slug}/items"))
                loop.close()
            except Exception as e:
                log(f"[Trakt->MDBList] Error fetching items for Trakt list '{list_name}': {e}", level=LOGERROR)
                items_resp = None

            if items_resp and items_resp.status_code == 200:
                items_data = items_resp.json()
                movies_payload = []
                shows_payload = []

                for item in items_data:
                    media_type = item.get("type")
                    if media_type == "movie":
                        ids = item.get("movie", {}).get("ids", {})
                        m_obj = {}
                        if ids.get("tmdb"): m_obj["tmdb"] = ids["tmdb"]
                        if ids.get("imdb"): m_obj["imdb"] = ids["imdb"]
                        if m_obj: movies_payload.append(m_obj)
                    elif media_type == "show":
                        ids = item.get("show", {}).get("ids", {})
                        s_obj = {}
                        if ids.get("tmdb"): s_obj["tmdb"] = ids["tmdb"]
                        if ids.get("imdb"): s_obj["imdb"] = ids["imdb"]
                        if s_obj: shows_payload.append(s_obj)

                mdblist_id = _get_or_create_mdblist_list(list_name, is_private, api_key, existing_mdblist_map)
                if mdblist_id:
                    _push_items_to_mdblist(mdblist_id, movies_payload, shows_payload, api_key)

    # 3. Fetch and Sync Trakt Movie Collection
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        coll_movies_resp = loop.run_until_complete(trakt_handler.get("/sync/collection/movies"))
        loop.close()
    except Exception as e:
        log(f"[Trakt->MDBList] Error fetching Trakt movie collection: {e}", level=LOGERROR)
        coll_movies_resp = None

    if coll_movies_resp and coll_movies_resp.status_code == 200:
        movies_data = coll_movies_resp.json()
        movies_payload = []
        for m in movies_data:
            ids = m.get("movie", {}).get("ids", {})
            m_obj = {}
            if ids.get("tmdb"): m_obj["tmdb"] = ids["tmdb"]
            if ids.get("imdb"): m_obj["imdb"] = ids["imdb"]
            if m_obj: movies_payload.append(m_obj)

        if movies_payload:
            coll_movie_list_id = _get_or_create_mdblist_list("Trakt Movie Collection", False, api_key, existing_mdblist_map)
            if coll_movie_list_id:
                _push_items_to_mdblist(coll_movie_list_id, movies_payload, [], api_key)

    # 4. Fetch and Sync Trakt TV Show Collection
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        coll_shows_resp = loop.run_until_complete(trakt_handler.get("/sync/collection/shows"))
        loop.close()
    except Exception as e:
        log(f"[Trakt->MDBList] Error fetching Trakt TV show collection: {e}", level=LOGERROR)
        coll_shows_resp = None

    if coll_shows_resp and coll_shows_resp.status_code == 200:
        shows_data = coll_shows_resp.json()
        shows_payload = []
        for s in shows_data:
            ids = s.get("show", {}).get("ids", {})
            s_obj = {}
            if ids.get("tmdb"): s_obj["tmdb"] = ids["tmdb"]
            if ids.get("imdb"): s_obj["imdb"] = ids["imdb"]
            if s_obj: shows_payload.append(s_obj)

        if shows_payload:
            coll_show_list_id = _get_or_create_mdblist_list("Trakt TV Show Collection", False, api_key, existing_mdblist_map)
            if coll_show_list_id:
                _push_items_to_mdblist(coll_show_list_id, [], shows_payload, api_key)

    log("[Trakt->MDBList] Completed Trakt lists and collection sync to MDBList.", level=LOGINFO)
    return True
