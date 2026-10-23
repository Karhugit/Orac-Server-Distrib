"""
simkl_api.py
------------
Helper module to build standardized URL parameters and HTTP headers
for Simkl API calls, conforming to https://api.simkl.org/conventions/headers.
Includes temporary debug logging for outgoing calls and responses.
"""

import os
import time
import requests
from resources.lib.version import __version__
from resources.lib.log_utils import log, LOGINFO, LOGERROR, LOGWARNING

SIMKL_APP_NAME = "orac-server"

# Default AUTH V2 Client ID (registered under TV, devices & command line)
DEFAULT_SIMKL_CLIENT_ID = "57ab588efc99ea43687f8635dc7b883a8d45d624dc141970fddf503efa6f88c7"

# Known legacy V1 Client IDs that are not enabled for OAuth 2.0
LEGACY_SIMKL_CLIENT_IDS = {
    "8cdf2298c78dd4ff8cb8039faecd1b9f11cf108fac2b88092abd15c22cfe2cc2",
    "4c920ba05273be800e843c0a2a4c148e1a17adbbba14c441bc3861214088a296",
}

# Set to True to enable Simkl call logging, or set ORAC_DEBUG_SIMKL=TRUE in environment
DEBUG_SIMKL_CALLS = os.environ.get("ORAC_DEBUG_SIMKL", "FALSE").upper() == "TRUE"


def get_effective_simkl_client_id(config_db_path: str = None) -> str:
    """
    Returns the configured Simkl client_id from config.db, or environment variable,
    or falls back to DEFAULT_SIMKL_CLIENT_ID.
    Automatically filters out and migrates legacy V1 client IDs that do not support OAuth 2.0.
    """
    if config_db_path:
        from resources.lib.config_handler import get_config_value, update_config_values
        cid = get_config_value("simkl.client", config_db_path) or get_config_value("simkl_client", config_db_path)
        if cid and cid not in ("empty_setting", ""):
            if cid in LEGACY_SIMKL_CLIENT_IDS:
                log(f"[Simkl] Detected legacy V1 client ID '{cid[:8]}...', migrating config to V2 client ID", level=LOGINFO)
                update_config_values({
                    "simkl.client": DEFAULT_SIMKL_CLIENT_ID,
                    "simkl_client": DEFAULT_SIMKL_CLIENT_ID
                }, config_db_path)
                return DEFAULT_SIMKL_CLIENT_ID
            return cid
    return os.environ.get("SIMKL_CLIENT_ID") or DEFAULT_SIMKL_CLIENT_ID


def get_simkl_params(client_id: str = None, extra_params: dict = None) -> dict:
    """
    Build required URL query parameters for Simkl API requests:
    client_id, app-name, app-version.
    """
    params = {
        "app-name": SIMKL_APP_NAME,
        "app-version": __version__,
    }
    if client_id:
        params["client_id"] = client_id
    if extra_params:
        params.update(extra_params)
    return params


def get_simkl_headers(token: str = None, client_id: str = None, extra_headers: dict = None) -> dict:
    """
    Build required HTTP headers for Simkl API requests:
    User-Agent, Content-Type, Authorization, simkl-api-key.
    """
    headers = {
        "User-Agent": f"{SIMKL_APP_NAME}/{__version__}",
        "Content-Type": "application/json",
    }
    if client_id:
        headers["simkl-api-key"] = client_id
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    return headers


def simkl_request(method: str, url: str, **kwargs):
    """
    Execute an HTTP request to the Simkl API.
    When DEBUG_SIMKL_CALLS is True, logs request details (URL, params, headers, body)
    and response details (status, response snippet) to Orac Server log.
    """
    if DEBUG_SIMKL_CALLS:
        params = kwargs.get("params")
        headers = kwargs.get("headers")
        json_body = kwargs.get("json")
        body_info = f" | Body: {json_body}" if json_body is not None else ""
        log(f"[Simkl API] >>> {method.upper()} {url} | Params: {params} | Headers: {headers}{body_info}", level=LOGINFO)

    resp = requests.request(method, url, **kwargs)

    if DEBUG_SIMKL_CALLS:
        snippet = resp.text[:400] + ("..." if len(resp.text) > 400 else "")
        log(f"[Simkl API] <<< {resp.status_code} {url} | Response ({len(resp.content)} bytes): {snippet}", level=LOGINFO)

    return resp


def simkl_get(url: str, **kwargs):
    """Convenience wrapper for GET requests via simkl_request."""
    return simkl_request("GET", url, **kwargs)


def simkl_post(url: str, **kwargs):
    """Convenience wrapper for POST requests via simkl_request."""
    return simkl_request("POST", url, **kwargs)


def refresh_simkl_token(config_db_path: str) -> str | None:
    """
    Refresh a Simkl AUTH V2 token using grant_type=refresh_token.
    Updates simkl.token, simkl.refresh_token, and simkl.expires_at in config.db.
    Returns the new access_token on success, or None on failure.
    """
    if not config_db_path:
        return None
    from resources.lib.config_handler import get_config_value, update_config_values
    refresh_token = get_config_value("simkl.refresh_token", config_db_path) or get_config_value("simkl_refresh_token", config_db_path)
    if not refresh_token or refresh_token == "empty_setting":
        return None

    client_id = get_effective_simkl_client_id(config_db_path)
    url = "https://api.simkl.com/oauth2/token"
    headers = {
        "User-Agent": f"{SIMKL_APP_NAME}/{__version__}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token
    }

    try:
        resp = simkl_post(url, headers=headers, data=data, timeout=15)
        if resp.status_code == 200:
            token_data = resp.json()
            new_access_token = token_data.get("access_token")
            new_refresh_token = token_data.get("refresh_token") or refresh_token
            expires_in = token_data.get("expires_in", 604800)  # default 7 days
            new_expires_at = time.time() + expires_in

            update_config_values({
                "simkl.token": new_access_token,
                "simkl_token": new_access_token,
                "simkl.refresh_token": new_refresh_token,
                "simkl_refresh_token": new_refresh_token,
                "simkl.expires_at": str(new_expires_at),
                "simkl.client": client_id
            }, config_db_path)
            log(f"[Simkl API] Successfully refreshed V2 access token (expires in {expires_in}s)", level=LOGINFO)
            return new_access_token
        else:
            log(f"[Simkl API] Token refresh failed (HTTP {resp.status_code}): {resp.text}", level=LOGERROR)
            return None
    except Exception as e:
        log(f"[Simkl API] Error refreshing Simkl token: {e}", level=LOGERROR)
        return None


def ensure_valid_simkl_token(config_db_path: str) -> str | None:
    """
    Checks the stored Simkl token in config.db.
    If it's an AUTH V2 token (starts with 'simkl_at_'), checks expiration and
    proactively refreshes if it expires within 24 hours (or is expired).
    If it's a legacy V1 token (64 hex characters), returns it as-is.
    """
    if not config_db_path:
        return None
    from resources.lib.config_handler import get_config_value
    token = get_config_value("simkl.token", config_db_path) or get_config_value("simkl_token", config_db_path)
    if not token or token == "empty_setting":
        return None

    # Only V2 tokens start with simkl_at_
    if token.startswith("simkl_at_"):
        expires_at_str = get_config_value("simkl.expires_at", config_db_path, "")
        try:
            expires_at = float(expires_at_str) if expires_at_str else 0
        except ValueError:
            expires_at = 0

        # If token expires within 24 hours (86400s) or is expired, refresh now
        if expires_at and time.time() >= (expires_at - 86400):
            log("[Simkl API] V2 access token is near expiry or expired — refreshing...", level=LOGINFO)
            new_token = refresh_simkl_token(config_db_path)
            if new_token:
                return new_token

    return token


def get_simkl_user_info(config_db_path: str) -> dict:
    """
    Returns a dict with 'username' and 'user_id' for the authorized Simkl user.
    If 'simkl.user_id' is not yet stored (or was stored as non-numeric username),
    queries https://api.simkl.com/users/settings and caches it into config.db.
    """
    from resources.lib.config_handler import get_config_value, update_config_values
    username = get_config_value("simkl.user", config_db_path) or get_config_value("simkl_user", config_db_path) or "simkl_user"
    user_id = get_config_value("simkl.user_id", config_db_path) or get_config_value("simkl_user_id", config_db_path)

    # Note: Simkl returns user id in 'account.id' (numeric). If missing or non-numeric, fetch from API.
    if not user_id or user_id in ("empty_setting", "") or not str(user_id).isdigit():
        token = ensure_valid_simkl_token(config_db_path)
        client_id = get_effective_simkl_client_id(config_db_path)
        if token and client_id:
            try:
                settings_url = "https://api.simkl.com/users/settings"
                headers = get_simkl_headers(token=token, client_id=client_id)
                resp = simkl_get(settings_url, params=get_simkl_params(client_id), headers=headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    user_obj = data.get("user", {}) if isinstance(data, dict) else {}
                    account_obj = data.get("account", {}) if isinstance(data, dict) else {}
                    fetched_name = str(user_obj.get("name") or username)
                    fetched_id = str(account_obj.get("id") or user_obj.get("id") or "")
                    if fetched_id:
                        user_id = fetched_id
                        username = fetched_name
                        update_config_values({
                            "simkl.user_id": user_id,
                            "simkl_user_id": user_id,
                            "simkl.user": username,
                            "simkl_user": username
                        }, config_db_path)
                        log(f"[Simkl API] Cached user ID: {user_id} for user: {username}", level=LOGINFO)
            except Exception as e:
                log(f"[Simkl API] Error fetching user settings: {e}", level=LOGWARNING)

    return {"username": username, "user_id": user_id or username}


def fetch_simkl_user_custom_lists(config_db_path: str) -> dict:
    """
    Fetches the user's custom lists via GET https://api.simkl.com/lists/user/{userId}.
    Handles pagination. Returns {'lists': [...]} or {'error': 'premium_only', 'lists': []}.
    """
    token = ensure_valid_simkl_token(config_db_path)
    client_id = get_effective_simkl_client_id(config_db_path)
    if not token or not client_id:
        log("[Simkl API] Missing Simkl credentials for custom lists.", level=LOGINFO)
        return {"lists": []}

    user_info = get_simkl_user_info(config_db_path)
    user_id = user_info.get("user_id")
    if not user_id or not str(user_id).isdigit():
        log(f"[Simkl API] Non-numeric Simkl user ID ({user_id}); unable to fetch custom lists.", level=LOGWARNING)
        return {"lists": []}

    headers = get_simkl_headers(token=token, client_id=client_id)
    all_lists = []
    page = 1
    limit = 50

    while True:
        url = f"https://api.simkl.com/lists/user/{user_id}"
        params = get_simkl_params(client_id, {"page": page, "limit": limit})
        try:
            resp = simkl_get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json() if resp.text else {}
                if isinstance(data, dict) and data.get("error") == "premium_only":
                    log(f"[Simkl API] Custom lists require Simkl PRO/VIP: {data.get('message')}", level=LOGINFO)
                    return {"error": "premium_only", "lists": []}

                lists_page = data.get("lists", []) if isinstance(data, dict) else []
                all_lists.extend(lists_page)

                pagination = data.get("pagination", {})
                total_pages = pagination.get("total_pages", 1)
                if page >= total_pages or not lists_page:
                    break
                page += 1
            else:
                log(f"[Simkl API] Error fetching user custom lists (HTTP {resp.status_code}): {resp.text[:200]}", level=LOGWARNING)
                break
        except Exception as e:
            log(f"[Simkl API] Exception fetching user custom lists: {e}", level=LOGERROR)
            break

    log(f"[Simkl API] Fetched {len(all_lists)} custom lists for user {user_id}", level=LOGINFO)
    return {"lists": all_lists}


def fetch_simkl_custom_list_items(config_db_path: str, list_id: int | str, limit: int = 500, tmdb_handler=None) -> dict | None:
    """
    Fetches all items for a Simkl custom list via GET https://api.simkl.com/lists/{id}.
    Handles pagination and normalizes items for Orac DB sync (with 'tmdb', 'trakt', 'imdb' ids).
    """
    token = ensure_valid_simkl_token(config_db_path)
    client_id = get_effective_simkl_client_id(config_db_path)
    if not token or not client_id:
        log("[Simkl API] Missing Simkl credentials for fetching list items.", level=LOGINFO)
        return None

    headers = get_simkl_headers(token=token, client_id=client_id)
    page = 1
    all_raw_items = []
    list_meta = {}

    while True:
        url = f"https://api.simkl.com/lists/{list_id}"
        params = get_simkl_params(client_id, {"page": page, "limit": limit, "extended": "full"})
        try:
            resp = simkl_get(url, params=params, headers=headers, timeout=20)
            if resp.status_code == 200:
                data = resp.json() if resp.text else {}
                if isinstance(data, dict) and data.get("error") == "premium_only":
                    log(f"[Simkl API] Custom list {list_id} requires Simkl PRO/VIP: {data.get('message')}", level=LOGWARNING)
                    return {"error": "premium_only", "items": [], "counts": {"movies": 0, "shows": 0}, "meta": {}}

                if not list_meta and isinstance(data, dict):
                    raw_desc = data.get("description")
                    if isinstance(raw_desc, dict):
                        desc_str = raw_desc.get("full") or raw_desc.get("short") or ""
                    else:
                        desc_str = str(raw_desc or "")
                    list_meta = {
                        "id": data.get("id"),
                        "name": data.get("name"),
                        "slug": data.get("slug"),
                        "media_type": data.get("media_type"),
                        "description": desc_str,
                        "privacy": data.get("privacy", "public"),
                        "sort": data.get("sort", ["position", "asc"]),
                        "counts": data.get("counts", {})
                    }

                items_page = data.get("items", []) if isinstance(data, dict) else []
                all_raw_items.extend(items_page)

                pagination = data.get("pagination", {})
                total_pages = pagination.get("total_pages", 1)
                if page >= total_pages or not items_page:
                    break
                page += 1
            elif resp.status_code == 404:
                log(f"[Simkl API] Custom list {list_id} not found on Simkl (404).", level=LOGWARNING)
                return None
            else:
                log(f"[Simkl API] Error fetching custom list items for {list_id} (HTTP {resp.status_code})", level=LOGWARNING)
                break
        except Exception as e:
            log(f"[Simkl API] Exception fetching items for list {list_id}: {e}", level=LOGERROR)
            break

    formatted_items = []
    item_counts = {"movies": 0, "shows": 0}

    for item in all_raw_items:
        raw_type = str(item.get("type") or "").lower()
        media_type = "movie" if raw_type in ("movie", "movies") else "show"

        ids = item.get("ids", {})
        tmdb_id = ids.get("tmdb")
        trakt_id = ids.get("trakt")
        imdb_id = ids.get("imdb")
        simkl_id = ids.get("simkl_id") or ids.get("simkl")

        # Fallback to resolve tmdb_id via imdb if missing
        if not tmdb_id and imdb_id and tmdb_handler:
            try:
                resolved = tmdb_handler.find_by_external_id(imdb_id, source="imdb_id")
                if resolved and resolved.get("id"):
                    tmdb_id = resolved["id"]
            except Exception:
                pass

        if not tmdb_id:
            continue

        try:
            tmdb_id_int = int(tmdb_id)
        except (ValueError, TypeError):
            continue

        trakt_id_val = int(trakt_id) if trakt_id else -tmdb_id_int

        formatted_items.append({
            "type": media_type,
            media_type: {
                "title": item.get("title"),
                "year": item.get("year"),
                "ids": {
                    "tmdb": tmdb_id_int,
                    "trakt": trakt_id_val,
                    "imdb": imdb_id,
                    "simkl": simkl_id
                }
            }
        })
        if media_type == "movie":
            item_counts["movies"] += 1
        else:
            item_counts["shows"] += 1

    return {
        "items": formatted_items,
        "counts": item_counts,
        "meta": list_meta
    }


def add_item_to_simkl_watchlist(config_db_path: str, tmdb_id: int, media_type: str, status: str = "plantowatch") -> bool:
    """
    Adds a movie or TV show to the user's Simkl watchlist (e.g. status='plantowatch')
    via POST https://api.simkl.com/sync/add-to-list.
    Handles rate-limit locking retries.
    """
    token = ensure_valid_simkl_token(config_db_path)
    client_id = get_effective_simkl_client_id(config_db_path)
    if not token or not client_id:
        log("[Simkl API] Missing Simkl credentials to add to watchlist.", level=LOGWARNING)
        return False

    url = "https://api.simkl.com/sync/add-to-list"
    headers = get_simkl_headers(token=token, client_id=client_id)
    params = get_simkl_params(client_id)

    key = "movies" if media_type == "movie" else "shows"
    payload = {
        "to": status,
        key: [{"ids": {"tmdb": int(tmdb_id)}}]
    }

    for attempt in range(2):
        try:
            resp = simkl_post(url, params=params, headers=headers, json=payload, timeout=15)
            if resp.status_code == 200:
                log(f"[Simkl API] Successfully added {media_type} {tmdb_id} to Simkl watchlist ({status})", level=LOGINFO)
                return True
            elif resp.status_code == 400 and "rate_limit" in resp.text and attempt == 0:
                log("[Simkl API] Rate limit lock active on add-to-list. Waiting 3s before retry...", level=LOGINFO)
                time.sleep(3)
                continue
            else:
                log(f"[Simkl API] Failed to add item to Simkl watchlist (HTTP {resp.status_code}): {resp.text}", level=LOGWARNING)
                return False
        except Exception as e:
            log(f"[Simkl API] Exception adding item to Simkl watchlist: {e}", level=LOGERROR)
            return False

    return False


def remove_item_from_simkl_watchlist(config_db_path: str, tmdb_id: int, media_type: str) -> bool:
    """
    Removes a movie or TV show from the user's Simkl watchlist / history
    via POST https://api.simkl.com/sync/history/remove.
    Handles rate-limit locking retries.
    """
    token = ensure_valid_simkl_token(config_db_path)
    client_id = get_effective_simkl_client_id(config_db_path)
    if not token or not client_id:
        log("[Simkl API] Missing Simkl credentials to remove from watchlist.", level=LOGWARNING)
        return False

    url = "https://api.simkl.com/sync/history/remove"
    headers = get_simkl_headers(token=token, client_id=client_id)
    params = get_simkl_params(client_id)

    key = "movies" if media_type == "movie" else "shows"
    payload = {
        key: [{"ids": {"tmdb": int(tmdb_id)}}]
    }

    for attempt in range(2):
        try:
            resp = simkl_post(url, params=params, headers=headers, json=payload, timeout=15)
            if resp.status_code == 200:
                log(f"[Simkl API] Successfully removed {media_type} {tmdb_id} from Simkl history/watchlist", level=LOGINFO)
                return True
            elif resp.status_code == 400 and "rate_limit" in resp.text and attempt == 0:
                log("[Simkl API] Rate limit lock active on history/remove. Waiting 3s before retry...", level=LOGINFO)
                time.sleep(3)
                continue
            else:
                log(f"[Simkl API] Failed to remove item from Simkl watchlist (HTTP {resp.status_code}): {resp.text}", level=LOGWARNING)
                return False
        except Exception as e:
            log(f"[Simkl API] Exception removing item from Simkl watchlist: {e}", level=LOGERROR)
            return False

    return False

