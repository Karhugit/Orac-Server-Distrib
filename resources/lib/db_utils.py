import sqlite3
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGDEBUG
import json
from datetime import datetime


# ---------------------------------------------------------------------------
# Central SQLite connection helper
# ---------------------------------------------------------------------------

def db_connect(path, timeout=30.0, **kwargs):
    """
    Drop-in replacement for sqlite3.connect() that applies best-practice
    settings to every connection:

    - timeout=30s      — wait up to 30 s before raising OperationalError
    - WAL journal mode — allows concurrent readers while a write is in
                         progress, which is the primary cause of the
                         'database is locked' errors seen during sync.
                         WAL mode is sticky at the file level, so once
                         enabled it persists across all future connections.
    - busy_timeout     — SQLite-level busy handler (30 000 ms) that is
                         respected even by connections that don't set a
                         Python-level timeout.
    - synchronous=NORMAL — safe default; faster than FULL without the
                           durability risk of OFF.

    In-memory databases (':memory:') are left untouched because WAL is
    not meaningful for them.
    """
    conn = sqlite3.connect(path, timeout=timeout, **kwargs)
    if path != ':memory:':
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
        except Exception:
            pass  # never let pragma failures prevent normal operation
    return conn



def add_tvshow(tvshows_static_cursor, tvshows_dynamic_cursor, trakt_queue_cursor, media_id, trakt_handler, tmdb_handler=None, show=None):

    show_trakt_id = show["ids"]["trakt"]
    show_tmdb_id = media_id.get("tmdb")
    show_title = show.get("title", "Unknown Show")
    
    # Check if this is a placeholder Trakt ID (negative value)
    # Placeholder IDs indicate the show was sourced from TMDB without Trakt resolution
    is_placeholder_trakt_id = show_trakt_id is None or show_trakt_id < 0
    
    if is_placeholder_trakt_id and show_tmdb_id and trakt_handler is not None:
        try:
            # Try to resolve TMDB ID to Trakt ID
            resp = trakt_handler._get(f"/search/tmdb/{show_tmdb_id}?type=show")
            if resp and resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list) and len(data) > 0:
                    item = data[0]
                    if 'show' in item:
                        resolved_trakt_id = item['show']['ids'].get('trakt')
                        if resolved_trakt_id:
                            show_trakt_id = resolved_trakt_id
                            show["ids"]["trakt"] = show_trakt_id
                            is_placeholder_trakt_id = False
                            log(f"[DB] Successfully resolved TMDb ID {show_tmdb_id} to Trakt ID {show_trakt_id}", level=LOGINFO)
        except Exception as e:
            log(f"[DB] Error resolving TMDB ID {show_tmdb_id} to Trakt ID: {e}", level=LOGDEBUG)

    if is_placeholder_trakt_id:
        log(f"[DB] Syncing '{show_title}' (TMDb ID: {show_tmdb_id}) with TMDB-only data (placeholder Trakt ID: {show_trakt_id})", level=LOGINFO)
    else:
        log(f"[DB] Starting optimized sync for '{show_title}' (TMDb ID: {show_tmdb_id})", level=LOGINFO)

    # --- Step 1: Optimized Data Fetching ---

    # Fetch Trakt data only if we have a real Trakt ID
    trakt_seasons_data = []
    if not is_placeholder_trakt_id:
        try:
            trakt_seasons_data = trakt_handler.get_show_seasons_with_episodes(show_trakt_id)
            log(f"[DB] Fetched {len(trakt_seasons_data)} seasons from Trakt for '{show_title}'", level=LOGDEBUG)
        except Exception as e:
            log(f"[DB] Error fetching Trakt seasons/episodes for '{show_title}': {e}", level=LOGERROR)
            # Continue with TMDB-only data
    else:
        log(f"[DB] Skipping Trakt data fetch for '{show_title}' (using TMDB-only)", level=LOGDEBUG)

    # Fetch all TMDb data (show, seasons, episodes, images) in one optimized call
    try:
        tmdb_full_data = tmdb_handler.get_full_show_details(show_tmdb_id)
        if not tmdb_full_data:
            log(f"[DB] Could not get full TMDb data for '{show_title}'", level=LOGERROR)
            return
    except Exception as e:
        log(f"[DB] Error fetching full TMDb details for '{show_title}': {e}", level=LOGERROR)
        return

    # --- Step 2: Data Processing and DB Insertion ---

    # Prepare a lookup for Trakt episodes by season and episode number for fast access
    trakt_episodes_lookup = {}
    for season_data in trakt_seasons_data:
        s_num = season_data.get("number")
        if s_num is not None:
            trakt_episodes_lookup[s_num] = {ep.get("number"): ep for ep in season_data.get("episodes", []) if ep.get("number") is not None}

    # Combine Trakt and TMDb show data
    genres = show.get("genres", [])

    title = show.get("title") or tmdb_full_data.get("name")
    title = show.get("title") or tmdb_full_data.get("name")
    original_title = show.get("original_title") or tmdb_full_data.get("original_name") or title
    language = show.get("language") or tmdb_full_data.get("original_language") or ""

    
    first_air_date = tmdb_full_data.get("first_air_date")
    # Year
    year = show.get("year")
    if not year and first_air_date:
         year = int(first_air_date[:4])
    
    # First Aired
    date_input_format = '%Y-%m-%dT%H:%M:%S.%fZ'
    date_output_format = "%Y-%m-%d"
    input_date = show.get("first_aired", "")
    if input_date:
        first_aired = datetime.strptime(input_date, date_input_format).strftime(date_output_format)
    elif first_air_date:
        first_aired = first_air_date
    else:
        first_aired = ""

    slug = show.get("ids", {}).get("slug", "")
    overview = show.get("overview") or tmdb_full_data.get("overview", "")
    imdb_id = show.get("ids", {}).get("imdb", "")
    tvdb_id = tmdb_full_data.get("external_ids", {}).get("tvdb_id") or show.get("ids", {}).get("tvdb", "")
    if tvdb_id:
        tvdb_id = str(tvdb_id)
    last_updated = show.get("updated_at") or 0
    trailer = show.get("trailer", "")
    tagline = show.get("tagline") or tmdb_full_data.get("tagline", "")
    status = show.get("status") or tmdb_full_data.get("status", "unknown")
    certification = show.get("certification", "")
    
    # Networks
    network = show.get("network")
    if not network and tmdb_full_data.get("networks"):
        network = tmdb_full_data["networks"][0].get("name", "")

    # Country
    country = show.get("country")
    if not country and tmdb_full_data.get("origin_country"):
        country = tmdb_full_data["origin_country"][0]

    rating = show.get("rating") or tmdb_full_data.get("vote_average", 0.0)
    votes = show.get("votes") or tmdb_full_data.get("vote_count", 0)

    # Insert/Update Show in DB
    # We use show_tmdb_id as Primary Key now
    # Insert with show_tmdb_id first to ensure row exists
    tvshows_static_cursor.execute("""
        INSERT OR IGNORE INTO shows (show_tmdb_id) VALUES (?)
    """, (show_tmdb_id,))

    # Always update the show details to ensure data is fresh
    tvshows_static_cursor.execute("""
        UPDATE shows SET
            title = ?, original_title = ?, year = ?, first_aired = ?, slug = ?, overview = ?, show_trakt_id = ?, imdb_id = ?, tvdb_id = ?, last_updated = ?,
            trailer = ?, tagline = ?, status = ?, certification = ?, network = ?, country = ?, rating = ?, votes = ?, language = ?
        WHERE show_tmdb_id = ?
    """, (title, original_title, year, first_aired, slug, overview, show_trakt_id, imdb_id, tvdb_id, last_updated, trailer, tagline, status,
          certification, network, country, rating, votes, language, show_tmdb_id))


    # Insert Genres
    for genre in genres:
        # Normalize to slug format
        genre = genre.lower().replace(" ", "-")
        # Ensure genre exists
        tvshows_static_cursor.execute(
            "INSERT OR IGNORE INTO genres(name) VALUES(?)",
            (genre,)
        )
        # Link show <→ genre
        tvshows_static_cursor.execute(
            "INSERT OR IGNORE INTO tvshows_genres(tmdb_id, genre) VALUES(?, ?)",
            (show_tmdb_id, genre)
        )

    show_images = tmdb_handler.get_show_images_from_data(tmdb_full_data)
    tvshows_static_cursor.execute("""
        UPDATE shows
        SET poster_path = ?, fanart_path = ?, thumbnail_path = ?, clearlogo_path = ?, landscape_path = ?
        WHERE show_tmdb_id = ?
    """, (
        show_images.get("poster"), show_images.get("fanart"), show_images.get("thumb"),
        show_images.get("clearlogo"), show_images.get("landscape"), show_tmdb_id
    ))

    # Process Seasons and Episodes from the pre-fetched TMDb data
    tmdb_all_season_details = tmdb_handler.get_seasons_and_episodes_from_full_data(tmdb_full_data)
    processed_seasons = set()
    processed_episodes = set()

    for tmdb_season_data in tmdb_all_season_details:
        season_number = tmdb_season_data.get("season_number")
        if season_number is None:
            continue
        processed_seasons.add(season_number)

        # Insert Season
        insert_tv_season(tvshows_static_cursor, show_tmdb_id, tmdb_season_data)

        # Update Season Images from pre-fetched data
        season_images = tmdb_handler.get_season_images_from_data(tmdb_season_data, tmdb_full_data)
        season_id = f"{show_tmdb_id}:S{season_number}"
        tvshows_static_cursor.execute("""
            UPDATE seasons
            SET poster_path = ?, fanart_path = ?, thumbnail_path = ?, landscape_path = ?
            WHERE season_id = ?
        """, (
            season_images.get("poster"), season_images.get("fanart"),
            season_images.get("thumb"), season_images.get("landscape"), season_id
        ))

        # Process Episodes
        tmdb_episodes_in_season = tmdb_season_data.get("episodes", [])
        if not tmdb_episodes_in_season:
            continue

        for tmdb_episode_data in tmdb_episodes_in_season:
            episode_number = tmdb_episode_data.get("episode_number")
            if episode_number is None: continue
            
            processed_episodes.add((season_number, episode_number))
            
            episode_tmdb_id = tmdb_episode_data.get("id")
            if not episode_tmdb_id: continue
            
            # Get corresponding Trakt episode data for ID merging
            trakt_episode_data = trakt_episodes_lookup.get(season_number, {}).get(episode_number, {})

            # Insert Episode
            insert_episode_combined(tvshows_static_cursor, trakt_episode_data, tmdb_episode_data, show_tmdb_id)

            # Update Episode Images from pre-fetched data
            episode_images = tmdb_handler.get_episode_images_from_data(tmdb_episode_data, tmdb_full_data)
            tvshows_static_cursor.execute("""
                UPDATE episodes
                SET episode_poster_path = ?, episode_fanart_path = ?, episode_thumbnail_path = ?, episode_landscape_path = ?
                WHERE tmdb_id = ?
            """, (
                episode_images.get("poster"), episode_images.get("fanart"),
                episode_images.get("thumb"), episode_images.get("landscape"), episode_tmdb_id
            ))

    # Process Trakt-only Seasons and Episodes
    for season_number, episodes_dict in trakt_episodes_lookup.items():
        if season_number not in processed_seasons:
            trakt_season_meta = next((s for s in trakt_seasons_data if s.get("number") == season_number), {})
            mock_season = {
                "season_number": season_number,
                "name": trakt_season_meta.get("title") or f"Season {season_number}",
                "overview": trakt_season_meta.get("overview", ""),
                "episodes": list(episodes_dict.values()),
                "air_date": trakt_season_meta.get("first_aired", "")
            }
            insert_tv_season(tvshows_static_cursor, show_tmdb_id, mock_season)

        for episode_number, trakt_episode_data in episodes_dict.items():
            if (season_number, episode_number) in processed_episodes:
                continue
            
            insert_episode_combined(tvshows_static_cursor, trakt_episode_data, {}, show_tmdb_id)

    # Sync Fanart.tv if enabled
    try:
        from resources.lib.config_handler import get_fanart_config
        config = get_fanart_config()
        if config["fanart_enabled"]:
            import threading
            from resources.lib.fanart_client import sync_fanart_for_item
            threading.Thread(
                target=sync_fanart_for_item,
                args=(show_tmdb_id, "show", tmdb_handler, None),
                daemon=True,
                name=f"FanartSyncShow_{show_tmdb_id}"
            ).start()
    except Exception as e:
        log(f"[Fanart] Error triggering show sync in add_tvshow: {e}", level=LOGERROR)

def insert_tv_season(cursor, show_tmdb_id, season):
    season_number = season.get("season_number")
    if season_number is None:
        return

#    trakt_id = media_id["trakt"]
    season_id = f"{show_tmdb_id}:S{season_number}"
    title = season.get("name", "")
    overview = season.get("overview", "")
    # Calculate episode_count from the 'episodes' list within the season data
    episodes_list = season.get("episodes", [])
    episode_count = len(episodes_list) if episodes_list else 0
    air_date = season.get("air_date", "")

    cursor.execute("""
        INSERT OR REPLACE INTO seasons (season_id, show_id, season, title, overview, episode_count, air_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (season_id, show_tmdb_id, season_number, title, overview, episode_count, air_date))


def insert_episode_combined(cursor, trakt_episode_data, tmdb_episode_data, show_tmdb_id):
    """
    Insert an episode into the episodes table using combined Trakt and TMDb data.
    """
    # Prefer IDs from Trakt data as it's more comprehensive
    trakt_ids = trakt_episode_data.get("ids", {})
    trakt_id = trakt_ids.get("trakt")
    imdb_id = trakt_ids.get("imdb")
    tvdb_id = trakt_ids.get("tvdb")
    slug = trakt_ids.get("slug")

    # TMDb episode ID is usually just 'id' in TMDb episode data
    tmdb_id = tmdb_episode_data.get("id")
    if not tmdb_id and trakt_id:
        tmdb_id = -trakt_id
        
    if not tmdb_id:
        log(f"Skipping episode S{trakt_episode_data.get('season')}E{trakt_episode_data.get('number')} - Missing sufficient ID", LOGDEBUG)
        return

    # Metadata: Prefer TMDb for its richness, but fall back to Trakt if a value is missing/empty.
    title = tmdb_episode_data.get("name") or trakt_episode_data.get("title", "")
    overview = tmdb_episode_data.get("overview") or trakt_episode_data.get("overview", "")
    season_number = tmdb_episode_data.get("season_number") or trakt_episode_data.get("season")
    episode_number = tmdb_episode_data.get("episode_number") or trakt_episode_data.get("number")

    # If episode overview is missing from both sources, fall back to the show's overview
    if not overview:
        cursor.execute("SELECT overview FROM shows WHERE show_tmdb_id = ?", (show_tmdb_id,))
        show_overview_row = cursor.fetchone()
        overview = show_overview_row[0] if show_overview_row and show_overview_row[0] else ""
        if overview:
            log(f"Episode S{season_number}E{episode_number} of show {show_tmdb_id} is missing an overview. Using show's overview as fallback.", LOGDEBUG)

    # Resolve dynamic DB path and migrate watched status if migrating from placeholder (negative) to real tmdb_id
    if tmdb_id > 0 and season_number is not None and episode_number is not None:
        cursor.execute("""
            SELECT tmdb_id FROM episodes
            WHERE show_id = ? AND season = ? AND episode_number = ?
        """, (show_tmdb_id, season_number, episode_number))
        existing_rows = cursor.fetchall()
        for (old_tmdb_id,) in existing_rows:
            if old_tmdb_id < 0 and old_tmdb_id != tmdb_id:
                log(f"[DB] S{season_number}E{episode_number} tmdb_id is changing from placeholder {old_tmdb_id} to real {tmdb_id}. Migrating watched status.", level=LOGINFO)
                
                # Determine dynamic DB path
                dynamic_db_path = None
                try:
                    cursor.execute("PRAGMA database_list")
                    db_list = cursor.fetchall()
                    for db in db_list:
                        if db[1] == 'main' and db[2]:
                            dynamic_db_path = db[2].replace("static", "dynamic")
                            break
                except Exception as e:
                    log(f"[DB] Error finding dynamic DB path: {e}", level=LOGDEBUG)

                if dynamic_db_path:
                    try:
                        with sqlite3.connect(dynamic_db_path, timeout=10) as d_conn:
                            d_cursor = d_conn.cursor()
                            # Check if new tmdb_id already exists in watched_episodes
                            d_cursor.execute("SELECT 1 FROM watched_episodes WHERE tmdb_id = ?", (tmdb_id,))
                            if not d_cursor.fetchone():
                                d_cursor.execute("""
                                    UPDATE watched_episodes SET tmdb_id = ?
                                    WHERE tmdb_id = ?
                                """, (tmdb_id, old_tmdb_id))
                                d_conn.commit()
                                log(f"[DB] Successfully migrated watched_episodes from placeholder {old_tmdb_id} to {tmdb_id}", level=LOGINFO)
                            else:
                                d_cursor.execute("DELETE FROM watched_episodes WHERE tmdb_id = ?", (old_tmdb_id,))
                                d_conn.commit()
                                log(f"[DB] Deleted duplicate watched_episodes placeholder {old_tmdb_id} (real {tmdb_id} already exists)", level=LOGINFO)
                    except Exception as e:
                        log(f"[DB] Error updating watched_episodes: {e}", level=LOGERROR)
                
                # Delete obsolete negative placeholder row in static episodes
                try:
                    cursor.execute("DELETE FROM episodes WHERE tmdb_id = ?", (old_tmdb_id,))
                except Exception as e:
                    log(f"[DB] Error deleting placeholder episode: {e}", level=LOGERROR)

    raw_air_date = tmdb_episode_data.get("air_date") or trakt_episode_data.get("first_aired") or trakt_episode_data.get("effective_release_date") or ""
    air_date = raw_air_date[:10] if raw_air_date and len(raw_air_date) >= 10 else ""
    first_aired = trakt_episode_data.get("first_aired") or air_date
    runtime = tmdb_episode_data.get("runtime") or trakt_episode_data.get("runtime", 0)
    rating = tmdb_episode_data.get("vote_average") or trakt_episode_data.get("rating", 0.0)
    votes = tmdb_episode_data.get("vote_count") or trakt_episode_data.get("votes", 0)
    original_title = title  # Use the combined title

    # Trakt specific fields
    updated_at = trakt_episode_data.get("updated_at", "")
    episode_type = trakt_episode_data.get("episode_type", "standard")

    cursor.execute("""
        INSERT OR REPLACE INTO episodes (
            episode_trakt_id, show_id, season, episode_number, episode_title, episode_overview, air_date, slug, tmdb_id, imdb_id, tvdb_id, rating, first_aired,
            updated_at, votes, runtime, episode_type, original_title
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trakt_id,
        show_tmdb_id,
        season_number,
        episode_number,
        title,
        overview,
        air_date,
        slug,
        tmdb_id,
        imdb_id,
        tvdb_id,
        rating,
        first_aired,
        updated_at,
        votes,
        runtime,
        episode_type,
        original_title
    ))

def get_discover_params_from_db(cursor, query_name, media_type=None):
    """
    Retrieves discover parameters from the external_indexes table.
    """
    params, _ = get_discover_params_and_type_from_db(cursor, query_name, media_type)
    return params

def get_discover_params_and_type_from_db(cursor, query_name, media_type=None):
    """
    Retrieves discover parameters and media_type from the external_indexes table.
    """
    log(f"DB: Fetching discover params for query: '{query_name}' with media_type: {media_type}", LOGINFO)
    try:
        unslug = query_name.replace('-', ' ')
        query_base = "SELECT parameters, media_type FROM external_indexes WHERE (id = ? OR LOWER(id) = LOWER(?) OR LOWER(REPLACE(REPLACE(id, ' ', '-'), '_', '-')) = LOWER(?))"
        
        if media_type and media_type != 'all':
            normalized_media_types = [media_type]
            if media_type in ['tv', 'tvshow', 'show']:
                normalized_media_types = ['tv', 'tvshow', 'show']
            placeholders = ','.join('?' for _ in normalized_media_types)
            sql = f"{query_base} AND media_type IN ({placeholders})"
            cursor.execute(sql, (query_name, query_name, unslug, *normalized_media_types))
        else:
            cursor.execute(query_base, (query_name, query_name, unslug))
            
        result = cursor.fetchone()
        if result:
            return json.loads(result[0]), result[1]
        
        # Fallback: try fuzzy match by normalizing alphanumeric characters
        def normalize(s):
            return "".join(c.lower() for c in s if c.isalnum())
        target_norm = normalize(query_name)
        
        cursor.execute("SELECT id, media_type, parameters FROM external_indexes")
        for idx_id, idx_mtype, idx_params in cursor.fetchall():
            if normalize(idx_id) == target_norm:
                if not media_type or media_type == 'all' or (media_type in ['tv', 'tvshow', 'show'] and idx_mtype in ['tv', 'tvshow', 'show']) or media_type == idx_mtype:
                    return json.loads(idx_params), idx_mtype
                    
        log(f"No discover parameters found for query: {query_name}", LOGINFO)
        return None, None
    except sqlite3.Error as e:
        log(f"Database error while fetching discover parameters for '{query_name}': {e}", LOGERROR)
        return None, None
    except json.JSONDecodeError as e:
        log(f"Failed to parse JSON parameters for discover query '{query_name}': {e}", LOGERROR)
        return None, None
