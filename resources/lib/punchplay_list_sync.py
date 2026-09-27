# -*- coding: utf-8 -*-
"""
punchplay_list_sync.py
----------------------
Sync task for PunchPlay lists:
  1. User lists  — fetched from /api/platform/v1/me/lists (requires auth + lists:read)
                   Currently registers metadata only; item sync is a follow-on feature.
  2. Generic lists — trending movies/shows/anime from /api/public/v1/catalog/trending
                     (no authentication required)
"""

import requests
from datetime import datetime

from resources.lib.db_utils import db_connect
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
from resources.lib.trakt_list_sync import run_list_sync
from resources.lib.config_handler import get_config_value
from resources.lib.punchplay_api import (
    PUNCHPLAY_CLIENT_ID,
    PUNCHPLAY_TRENDING_URL,
    PUNCHPLAY_LISTS_URL,
    ensure_valid_punchplay_token,
    punchplay_get,
    get_punchplay_headers,
)
from resources.lib.punchplay_lists import PUNCHPLAY_GENERIC_LISTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")


def cleanup_list_items(db_path, list_id):
    """Removes all items for a given list_id from the list_items table."""
    try:
        with db_connect(db_path) as conn:
            conn.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
            conn.commit()
    except Exception as e:
        log(f"[PunchPlay] Error cleaning up list items for {list_id}: {e}", level=LOGERROR)


def update_punchplay_list_metadata(db_path, list_id, slug, name, description,
                                   source="punchplay", user="punchplay",
                                   owned_by_user=0, add_to_library_default=0,
                                   count_movies=0, count_shows=0):
    """Upserts a PunchPlay list entry in the lists table."""
    try:
        with db_connect(db_path) as conn:
            conn.execute("""
                INSERT INTO lists (list_id, source, user, slug, name, description,
                                   last_checked, item_count_movies, item_count_shows,
                                   owned_by_user, add_to_library)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(list_id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    last_checked=excluded.last_checked,
                    item_count_movies=excluded.item_count_movies,
                    item_count_shows=excluded.item_count_shows
            """, (
                list_id, source, user, slug, name, description,
                _now_iso(), count_movies, count_shows,
                owned_by_user, add_to_library_default
            ))
            conn.commit()
    except Exception as e:
        log(f"[PunchPlay] Error updating list metadata for {list_id}: {e}", level=LOGERROR)


# ---------------------------------------------------------------------------
# Generic list helpers (no auth required)
# ---------------------------------------------------------------------------

def fetch_punchplay_generic_items(api_type):
    """
    Fetches trending items from the PunchPlay public catalog.
    api_type: 'movie', 'show', or 'anime'
    Returns a list of raw item dicts (with tmdbId, type, name, etc.)
    """
    try:
        resp = requests.get(
            PUNCHPLAY_TRENDING_URL,
            params={"type": api_type},
            headers={"Accept": "application/json"},
            timeout=20
        )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items", [])
            if isinstance(items, list):
                return items
            log(f"[PunchPlay] Unexpected response shape for trending/{api_type}", level=LOGWARNING)
        else:
            log(f"[PunchPlay] Trending {api_type} fetch failed ({resp.status_code}): {resp.text.strip()}", level=LOGWARNING)
    except Exception as e:
        log(f"[PunchPlay] Error fetching trending {api_type}: {e}", level=LOGERROR)
    return []


def normalize_punchplay_items(raw_items, media_type):
    """
    Converts raw PunchPlay catalog items into Orac's run_list_sync format.
    PunchPlay items have tmdbId (int) and type ('movie'|'show').
    """
    normalized = []
    for raw in raw_items:
        tmdb_id = raw.get("tmdbId")
        if not tmdb_id:
            continue
        # Use a synthetic negative trakt_id based on tmdb_id (same pattern as Simkl)
        trakt_id = -int(tmdb_id)
        item_type = raw.get("type", media_type)  # 'movie' or 'show' (anime shows have type='show')
        if item_type == "movie":
            normalized.append({
                "type": "movie",
                "movie": {"ids": {"tmdb": int(tmdb_id), "trakt": trakt_id}}
            })
        else:
            normalized.append({
                "type": "show",
                "show": {"ids": {"tmdb": int(tmdb_id), "trakt": trakt_id}}
            })
    return normalized


# ---------------------------------------------------------------------------
# User list helpers (auth required)
# ---------------------------------------------------------------------------

def fetch_punchplay_user_lists(config_db_path):
    """
    Fetches all user-owned lists from PunchPlay via GET /api/platform/v1/me/lists.
    Returns a list of list summary dicts, or an empty list if not authenticated.
    """
    token = ensure_valid_punchplay_token(config_db_path)
    if not token:
        log("[PunchPlay] No valid token for user list fetch. Skipping.", level=LOGINFO)
        return []

    all_lists = []
    cursor = None
    page = 0

    while True:
        page += 1
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor

        try:
            resp = punchplay_get(PUNCHPLAY_LISTS_URL, token=token, params=params, timeout=15)
            if resp.status_code != 200:
                log(f"[PunchPlay] Failed to fetch user lists ({resp.status_code}): {resp.text.strip()}", level=LOGWARNING)
                break

            data = resp.json()
            items = data.get("items", [])
            all_lists.extend(items)
            log(f"[PunchPlay] User lists page {page}: {len(items)} lists fetched", level=LOGDEBUG)

            cursor = data.get("nextCursor")
            if not cursor:
                break

        except Exception as e:
            log(f"[PunchPlay] Error fetching user lists page {page}: {e}", level=LOGERROR)
            break

    return all_lists


# ---------------------------------------------------------------------------
# Main sync task
# ---------------------------------------------------------------------------

async def punchplay_list_sync_task(config_db_path, lists_db_path, trakt_handler, tmdb_handler,
                                    movies_static_db_path, movies_dynamic_db_path,
                                    tvshows_static_db_path, tvshows_dynamic_db_path,
                                    trakt_queue_path):
    try:
        # Read current library settings for all PunchPlay lists
        lists_library_settings = {}
        with db_connect(lists_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT list_id, add_to_library FROM lists")
            for row in cursor.fetchall():
                lists_library_settings[row[0]] = row[1]

        username = get_config_value("punchplay.user", config_db_path) or "punchplay"

        # -----------------------------------------------------------------
        # 1. User Lists (requires auth)
        # -----------------------------------------------------------------
        token = ensure_valid_punchplay_token(config_db_path)
        if token:
            log("[PunchPlay] **SYNC** Fetching user lists...", level=LOGINFO)
            user_lists = fetch_punchplay_user_lists(config_db_path)
            log(f"[PunchPlay] Found {len(user_lists)} user list(s) on PunchPlay", level=LOGINFO)

            for lst in user_lists:
                list_api_id = lst.get("id")  # Integer ID from PunchPlay
                list_name = lst.get("name", "Untitled List")
                list_desc = lst.get("description") or ""
                item_count = lst.get("itemCount", 0)
                is_watchlist = lst.get("isWatchlist", False)

                if list_api_id is None:
                    continue

                slug = f"punchplay-list-{list_api_id}"
                list_id = f"punchplay:user:{list_api_id}"

                # Infer movie/show count from itemCount (we don't know the split without fetching items)
                # Register with rough estimate — will be refined when item sync is added
                is_in_library = lists_library_settings.get(list_id, None)

                if is_in_library is None:
                    # New list — register with add_to_library=0 (user must opt in)
                    # Watchlists default to add_to_library=1
                    default_lib = 1 if is_watchlist else 0
                    update_punchplay_list_metadata(
                        lists_db_path, list_id, slug, list_name, list_desc,
                        source="punchplay", user=username, owned_by_user=1,
                        add_to_library_default=default_lib,
                        count_movies=item_count, count_shows=0
                    )
                    log(f"[PunchPlay] Registered new user list: '{list_name}' (id={list_api_id})", level=LOGINFO)
                else:
                    # Update metadata (without touching add_to_library)
                    update_punchplay_list_metadata(
                        lists_db_path, list_id, slug, list_name, list_desc,
                        source="punchplay", user=username, owned_by_user=1,
                        add_to_library_default=is_in_library,
                        count_movies=item_count, count_shows=0
                    )
        else:
            log("[PunchPlay] Not authenticated — skipping user list sync.", level=LOGINFO)

        # -----------------------------------------------------------------
        # 2. Generic Lists (Trending Movies / Shows / Anime)
        # -----------------------------------------------------------------
        for list_def in PUNCHPLAY_GENERIC_LISTS:
            slug = list_def["slug"]
            list_id = f"punchplay:generic:{slug}"
            media_type = list_def["type"]      # 'movie' or 'show'
            api_type = list_def["api_type"]    # 'movie', 'show', or 'anime'
            is_in_library = lists_library_settings.get(list_id, 0) == 1

            if not is_in_library:
                # Register metadata only — user must opt in to sync items
                count_m = 50 if media_type == "movie" else 0
                count_s = 50 if media_type == "show" else 0
                update_punchplay_list_metadata(
                    lists_db_path, list_id, slug,
                    list_def["name"], list_def["description"],
                    count_movies=count_m, count_shows=count_s
                )
                cleanup_list_items(lists_db_path, list_id)
                log(f"[PunchPlay] Generic list '{slug}' not in library. Updated metadata only.", level=LOGDEBUG)
                continue

            # In library — fetch and sync items
            log(f"[PunchPlay] **SYNC** Fetching generic list '{slug}' ({api_type})...", level=LOGINFO)
            raw_items = fetch_punchplay_generic_items(api_type)
            if not raw_items:
                log(f"[PunchPlay] No items returned for generic list '{slug}'", level=LOGWARNING)
                continue

            normalized_items = normalize_punchplay_items(raw_items, media_type)
            if not normalized_items:
                log(f"[PunchPlay] No normalizable items for generic list '{slug}'", level=LOGWARNING)
                continue

            counts = {
                "movies": len(normalized_items) if media_type == "movie" else 0,
                "shows": len(normalized_items) if media_type == "show" else 0,
            }
            list_meta = {
                "ids": {"slug": slug},
                "name": list_def["name"],
                "description": list_def["description"],
                "updated_at": _now_iso(),
                "item_count": counts,
                "user": {"ids": {"slug": "punchplay"}},
                "source": "punchplay",
                "owned_by_user": False,
                "list_id": list_id
            }

            log(f"[PunchPlay] **SYNC** Updating generic list {slug} ({len(normalized_items)} items)", level=LOGINFO)
            log(f"[PunchPlay]   -  Movies: {counts['movies']}", level=LOGINFO)
            log(f"[PunchPlay]   -  Shows:  {counts['shows']}", level=LOGINFO)

            await run_list_sync(
                lists_db_path,
                "punchplay",
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
            log(f"[PunchPlay] **SYNC** Updated generic list {slug}", level=LOGINFO)

    except Exception as e:
        log(f"[PunchPlay] Error in PunchPlay list sync task: {e}", level=LOGERROR)
