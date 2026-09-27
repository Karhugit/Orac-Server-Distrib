import sqlite3
import json
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from resources.lib.db_utils import db_connect
from resources.lib.log_utils import log, LOGINFO, LOGDEBUG, LOGERROR, LOGWARNING

INTRODB_API_BASE = "https://api.introdb.app"
USER_AGENT = "OracServer/1.3.6 (https://github.com/Karhugit/orac_server)"


def _format_segment(segment_data):
    """
    Formats an IntroDB segment dictionary into a normalized JSON string.
    Returns empty string if segment_data is None or empty.
    """
    if not segment_data or not isinstance(segment_data, dict):
        return ""
    start_sec = segment_data.get("start_sec")
    end_sec = segment_data.get("end_sec")
    if start_sec is None or end_sec is None:
        return ""
    payload = {
        "start": start_sec,
        "end": end_sec,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "start_ms": segment_data.get("start_ms", int(start_sec * 1000)),
        "end_ms": segment_data.get("end_ms", int(end_sec * 1000)),
        "confidence": segment_data.get("confidence", 1.0),
        "submission_count": segment_data.get("submission_count", 1)
    }
    return json.dumps(payload)


def fetch_episode_segments(show_imdb_id, season, episode, timeout=15):
    """
    Queries the IntroDB /segments endpoint for an episode.
    Returns (intro_json_or_empty, outro_json_or_empty) on success,
    or None if the request failed (e.g. rate limited or connection error).
    """
    if not show_imdb_id or not show_imdb_id.startswith("tt"):
        return None
    url = f"{INTRODB_API_BASE}/segments"
    params = {
        "imdb_id": show_imdb_id,
        "season": int(season),
        "episode": int(episode)
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json"
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            intro_str = _format_segment(data.get("intro"))
            outro_str = _format_segment(data.get("outro"))
            return intro_str, outro_str
        elif resp.status_code == 404:
            # Episode or show not found in IntroDB
            return "", ""
        elif resp.status_code == 429:
            log(f"[IntroDB] Rate limited (429) for {show_imdb_id} S{season}E{episode}", level=LOGWARNING)
            return None
        else:
            log(f"[IntroDB] API returned status {resp.status_code} for {show_imdb_id} S{season}E{episode}: {resp.text[:100]}", level=LOGDEBUG)
            return None
    except requests.exceptions.Timeout:
        log(f"[IntroDB] Request timed out for {show_imdb_id} S{season}E{episode}", level=LOGDEBUG)
        return None
    except Exception as e:
        log(f"[IntroDB] Exception fetching segments for {show_imdb_id} S{season}E{episode}: {e}", level=LOGDEBUG)
        return None


def backfill_missing_show_imdb_ids(tvshows_static_db_path, tmdb_handler, max_shows=10):
    """
    Looks up IMDb IDs (and TVDB IDs) via TMDb for shows in the static DB that are missing them.
    """
    if not tmdb_handler:
        return
    try:
        with db_connect(tvshows_static_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT show_tmdb_id, title FROM shows
                WHERE (imdb_id IS NULL OR imdb_id = '')
                LIMIT ?
            """, (max_shows,))
            missing_shows = cursor.fetchall()
            if not missing_shows:
                return

            log(f"[IntroDB] Attempting to backfill IMDb IDs for {len(missing_shows)} shows via TMDb...", level=LOGINFO)
            updates = []
            for show_tmdb_id, title in missing_shows:
                try:
                    ext = None
                    if hasattr(tmdb_handler, 'get_show_external_ids'):
                        ext = tmdb_handler.get_show_external_ids(show_tmdb_id)
                    elif hasattr(tmdb_handler, '_get'):
                        ext = tmdb_handler._get(f"/tv/{show_tmdb_id}/external_ids")

                    if not isinstance(ext, dict):
                        continue

                    imdb_id = ext.get("imdb_id") or ""
                    tvdb_id = str(ext.get("tvdb_id")) if ext.get("tvdb_id") else None

                    updates.append((imdb_id, tvdb_id, show_tmdb_id))
                    if imdb_id:
                        log(f"[IntroDB] Found IMDb ID {imdb_id} for '{title}' (TMDb {show_tmdb_id})", level=LOGINFO)
                    else:
                        log(f"[IntroDB] No IMDb ID available on TMDb for '{title}' (TMDb {show_tmdb_id})", level=LOGDEBUG)
                except Exception as e:
                    log(f"[IntroDB] Error fetching external IDs for show {show_tmdb_id}: {e}", level=LOGDEBUG)

            if updates:
                cursor.executemany("""
                    UPDATE shows
                    SET imdb_id = ?,
                        tvdb_id = COALESCE(NULLIF(tvdb_id, ''), ?)
                    WHERE show_tmdb_id = ?
                """, updates)
                conn.commit()
    except Exception as e:
        log(f"[IntroDB] Error backfilling show IMDb IDs: {e}", level=LOGWARNING)


def sync_introdb_segments(tvshows_static_db_path, tmdb_handler=None, max_episodes=500, dynamic_db_path=None):
    """
    Syncs segment timestamps from IntroDB into the episodes table in tvshows_static_cache.db.
    Checks episodes where intro IS NULL, prioritizing active/watched shows.
    """
    if not tvshows_static_db_path:
        return

    # Check that intro and outro columns exist
    try:
        with db_connect(tvshows_static_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(episodes)")
            existing_cols = {row[1] for row in cursor.fetchall()}
            if "intro" not in existing_cols or "outro" not in existing_cols:
                from resources.lib.migrate_database import migration_3_add_introdb_columns
                migration_3_add_introdb_columns(tvshows_static_db_path, dynamic_db_path or tvshows_static_db_path)
    except Exception as e:
        log(f"[IntroDB] Error verifying columns: {e}", level=LOGWARNING)

    # Optionally backfill missing IMDb IDs
    if tmdb_handler:
        backfill_missing_show_imdb_ids(tvshows_static_db_path, tmdb_handler, max_shows=10)

    # Collect episodes needing IntroDB sync
    pending_episodes = []
    try:
        with db_connect(tvshows_static_db_path) as conn:
            cursor = conn.cursor()

            # Attach dynamic DB if available to prioritize watched / in-progress shows
            has_dynamic = False
            if dynamic_db_path:
                try:
                    cursor.execute("ATTACH DATABASE ? AS dynamic_db", (dynamic_db_path,))
                    has_dynamic = True
                except Exception:
                    pass

            if has_dynamic:
                query = """
                    SELECT e.tmdb_id, s.imdb_id, e.season, e.episode_number,
                           CASE WHEN we.tmdb_id IS NOT NULL THEN 0 ELSE 1 END as priority
                    FROM episodes e
                    JOIN shows s ON e.show_id = s.show_tmdb_id
                    LEFT JOIN dynamic_db.watched_episodes we ON e.tmdb_id = we.tmdb_id
                    WHERE s.imdb_id IS NOT NULL AND s.imdb_id != ''
                      AND e.season > 0 AND e.episode_number > 0
                      AND e.intro IS NULL
                    ORDER BY priority ASC, e.show_id ASC, e.season ASC, e.episode_number ASC
                    LIMIT ?
                """
            else:
                query = """
                    SELECT e.tmdb_id, s.imdb_id, e.season, e.episode_number, 0 as priority
                    FROM episodes e
                    JOIN shows s ON e.show_id = s.show_tmdb_id
                    WHERE s.imdb_id IS NOT NULL AND s.imdb_id != ''
                      AND e.season > 0 AND e.episode_number > 0
                      AND e.intro IS NULL
                    ORDER BY e.show_id ASC, e.season ASC, e.episode_number ASC
                    LIMIT ?
                """

            cursor.execute(query, (max_episodes,))
            pending_episodes = cursor.fetchall()
    except Exception as e:
        log(f"[IntroDB] Error collecting pending episodes: {e}", level=LOGERROR)
        return

    if not pending_episodes:
        log("[IntroDB] No episodes pending IntroDB sync.", level=LOGINFO)
        return

    log(f"[IntroDB] Starting segment sync for {len(pending_episodes)} episodes...", level=LOGINFO)
    start_time = time.time()

    updates = []
    intros_found = 0
    outros_found = 0
    empty_count = 0
    rate_limited = False

    def _worker(item):
        ep_tmdb_id, show_imdb, season, ep_num, _ = item
        res = fetch_episode_segments(show_imdb, season, ep_num)
        return ep_tmdb_id, res

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_worker, ep): ep for ep in pending_episodes}
        for future in as_completed(futures):
            if rate_limited:
                continue
            try:
                ep_tmdb_id, res = future.result()
                if res is None:
                    # Request failed or rate-limited; skip updating this episode so it can be retried
                    continue
                intro_str, outro_str = res
                updates.append((intro_str, outro_str, ep_tmdb_id))
                if intro_str:
                    intros_found += 1
                if outro_str:
                    outros_found += 1
                if not intro_str and not outro_str:
                    empty_count += 1
            except Exception as e:
                log(f"[IntroDB] Error processing worker result: {e}", level=LOGDEBUG)

    if updates:
        try:
            with db_connect(tvshows_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.executemany("""
                    UPDATE episodes
                    SET intro = ?, outro = ?
                    WHERE tmdb_id = ?
                """, updates)
                conn.commit()
        except Exception as e:
            log(f"[IntroDB] Error committing segment updates to database: {e}", level=LOGERROR)

    duration = time.time() - start_time
    log(
        f"[IntroDB] Segment sync finished in {duration:.2f}s: "
        f"{len(updates)}/{len(pending_episodes)} episodes updated "
        f"({intros_found} intros, {outros_found} outros found, {empty_count} without segments).",
        level=LOGINFO
    )
