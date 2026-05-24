import sqlite3
import json
from resources.lib.log_utils import log, LOGDEBUG, LOGINFO, LOGERROR, LOGWARNING
from resources.lib.db_utils import add_tvshow
from datetime import datetime
import resources.lib.recommendations_handler as recommendations
#from resources.lib.tmdb_utils import update_show_images

async def update_dynamic_tvshow_data(trakt_handler, tmdb_handler, username, tvshows_dynamic_db_path, tvshows_static_db_path, trakt_queue_path):
    static_conn = None
    dynamic_conn = None
    trakt_queue_conn = None

    if not username:
        log("[Orac] Skipping dynamic TV show data update: No username provided", level=LOGWARNING)
        return

    try:
        # Step 1: Get watched shows/episodes from Trakt
        watched_resp = await trakt_handler.get("/sync/watched/shows")
        if watched_resp is None:
            log(f"[Orac] No response received when fetching watched TV shows for {username}", level=LOGERROR)
            return
        if watched_resp.status_code != 200:
            log(f"[Orac] Failed to fetch watched TV shows for {username}: {watched_resp.status_code}", level=LOGERROR)
            return

        watched_data = watched_resp.json()

        # Step 2: Open DB connections
        static_conn = sqlite3.connect(tvshows_static_db_path)
        static_cursor = static_conn.cursor()
        dynamic_conn = sqlite3.connect(tvshows_dynamic_db_path)
        dynamic_cursor = dynamic_conn.cursor()
        trakt_queue_conn = sqlite3.connect(trakt_queue_path)
        trakt_queue_cursor = trakt_queue_conn.cursor()

        insert_count = 0
        skip_count = 0



        # Step 3: Filter and process shows
        for show in watched_data:
            trakt_last_updated = show.get("last_updated_at")
            show_trakt_id = show["show"]["ids"]["trakt"]
            show_tmdb_id = show["show"]["ids"].get("tmdb")
            show_imdb_id = show["show"]["ids"].get("imdb")
            show_title = show["show"]["title"]

            # Resolution chain for missing TMDb ID:
            # Step 1: Try IMDB ID -> TMDb lookup
            if not show_tmdb_id and show_imdb_id:
                result = tmdb_handler.find_by_external_id(show_imdb_id, source="imdb_id")
                if result:
                    show_tmdb_id = result.get("id")
                    log(f"[Orac] Resolved TMDb ID {show_tmdb_id} for watched show '{show_title}' (Trakt: {show_trakt_id}) using IMDB ID {show_imdb_id}", level=LOGINFO)

            # Step 2: Ask Trakt directly for the show's full IDs — the watched-sync
            # response sometimes omits tmdb even when Trakt has it
            if not show_tmdb_id:
                try:
                    trakt_show_resp = trakt_handler._get(f"/shows/{show_trakt_id}?extended=full")
                    if trakt_show_resp and trakt_show_resp.status_code == 200:
                        trakt_show_data = trakt_show_resp.json()
                        show_tmdb_id = trakt_show_data.get("ids", {}).get("tmdb")
                        if not show_imdb_id:
                            show_imdb_id = trakt_show_data.get("ids", {}).get("imdb")
                        if show_tmdb_id:
                            log(f"[Orac] Resolved TMDb ID {show_tmdb_id} for watched show '{show_title}' (Trakt: {show_trakt_id}) via Trakt full show lookup", level=LOGINFO)
                        elif show_imdb_id:
                            # Got IMDB ID from Trakt, now try TMDb find
                            result = tmdb_handler.find_by_external_id(show_imdb_id, source="imdb_id")
                            if result:
                                show_tmdb_id = result.get("id")
                                log(f"[Orac] Resolved TMDb ID {show_tmdb_id} for watched show '{show_title}' (Trakt: {show_trakt_id}) via Trakt IMDB ID {show_imdb_id}", level=LOGINFO)
                except Exception as e:
                    log(f"[Orac] Error during Trakt full-show lookup for '{show_title}' (Trakt: {show_trakt_id}): {e}", level=LOGWARNING)

            # Step 3: If all resolution attempts fail, skip gracefully.
            # We cannot write to user_show_sync, watched_episodes, or watched_history
            # without a TMDb ID (all require it as a non-null key), so there is no
            # safe partial write possible — watched/next-episode state won't update
            # for this show until Trakt or TMDb gains the missing mapping.
            if not show_tmdb_id:
                log(f"[Orac] Skipping watched show '{show_title}' (Trakt: {show_trakt_id}): could not resolve a TMDb ID after all fallback attempts.", level=LOGWARNING)
                skip_count += 1
                continue

            # Check the show's specific last_updated timestamp from the dynamic DB
            dynamic_cursor.execute("SELECT last_updated_at FROM user_show_sync WHERE user = ? AND show_tmdb_id = ?", (username, show_tmdb_id))
            local_sync_row = dynamic_cursor.fetchone()
            local_last_updated = local_sync_row[0] if local_sync_row else None

            if local_last_updated and trakt_last_updated and trakt_last_updated <= local_last_updated:
                skip_count += 1
                continue  # Skip unchanged shows


            try:
                # If the show is new, it will fetch from Trakt (for IDs) and TMDb (for metadata)
                #   and populate the 'shows', 'seasons', and 'episodes' static tables.
                # - If the show already exists, it will effectively no-op or re-insert (based on OR REPLACE),
                #   but importantly, it ensures all episodes have both Trakt and TMDb IDs.
                log(f"[Orac] Processing watched show: {show_title} (Trakt ID: {show_trakt_id}) for static DB sync.", level=LOGINFO)
    # Media id is a list of type and id, e.g. "trakt":"1234"
                media_id = {"trakt": show_trakt_id, "tmdb": show_tmdb_id}
                add_tvshow(
                    static_cursor,
                    dynamic_cursor,
                    trakt_queue_cursor,
                    media_id,    
                    trakt_handler,
                    tmdb_handler,
                    show["show"] # Pass the show dictionary
                )            
    
                for season_data in show.get("seasons", []):
                    season_num = season_data["number"]
                    for episode_data in season_data.get("episodes", []):
                        episode_num = episode_data["number"]
                        watched_at = episode_data.get("last_watched_at")
                        if not watched_at:
                            log(f"[Orac] Skipping episode {episode_num} of season {season_num} for show {show_title} (Trakt ID: {show_trakt_id}) - no watched_at data", level=LOGDEBUG)
                            skip_count += 1
                            continue
    # Get episode data from episodes table
                        static_cursor.execute("""
                            SELECT episode_trakt_id, tmdb_id FROM episodes WHERE show_id = ? AND season = ? AND episode_number = ?
                        """, (show_tmdb_id, season_num, episode_num))
                        episode_ids = static_cursor.fetchone()
                        if episode_ids:
                            episode_trakt_id, episode_tmdb_id = episode_ids
                            # Insert into dynamic 'watched_episodes' table.
                            dynamic_cursor.execute("""
                                INSERT OR REPLACE INTO watched_episodes (user, episode_trakt_id, tmdb_id, season, episode, watched_at, percent_watched, watched_status)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """, (username, episode_trakt_id, episode_tmdb_id, season_num, episode_num, watched_at, 100, 2))

                            # Insert into watched_history for dual sync (sets trakt_synced_at so we don't double sync Trakt, resets Simkl/MDBList so they get re-synced)
                            dynamic_cursor.execute("""
                                INSERT INTO watched_history (show_tmdb_id, season, episode, is_watched, last_watched_at, trakt_synced_at, simkl_synced_at, mdblist_synced_at)
                                VALUES (?, ?, ?, 1, ?, ?, NULL, NULL)
                                ON CONFLICT(show_tmdb_id, season, episode) DO UPDATE SET
                                    is_watched = 1, last_watched_at = ?, trakt_synced_at = ?,
                                    simkl_synced_at = NULL, mdblist_synced_at = NULL
                            """, (show_tmdb_id, season_num, episode_num, watched_at, watched_at, watched_at, watched_at))

                            log(f"[Orac] ✔ Synced watched S{season_num}E{episode_num} of {show_title} for {username} at {watched_at}", level=LOGINFO)
                            insert_count += 1
                        else:
                            log(f"[Orac] WARNING: Episode S{season_num}E{episode_num} of {show_title} not found in static 'episodes' table. Cannot sync watched status for this specific episode.", level=LOGWARNING)
    
                # After syncing watched episodes, update the sync timestamp for this show and user
                dynamic_cursor.execute("""
                    INSERT OR REPLACE INTO user_show_sync (user, show_tmdb_id, last_updated_at)
                    VALUES (?, ?, ?)
                """, (username, show_tmdb_id, trakt_last_updated))
    
                # Update show's watched status based on episodes
                _update_show_watched_status(dynamic_cursor, static_cursor, username, show_tmdb_id)

                # Commit changes for this show to ensure partial progress is saved
                static_conn.commit()
                dynamic_conn.commit()
                trakt_queue_conn.commit()

            except Exception as e:
                log(f"[Orac] Error processing watched show '{show_title}' (Trakt: {show_trakt_id}): {e}", level=LOGERROR)

        log(f"[Orac] Watched episodes updated for {username}: {insert_count} added, {skip_count} skipped", level=LOGINFO)

    except Exception as e:
        log(f"[Orac] Error updating dynamic TV show data: {e}", level=LOGERROR)

    finally:
        if static_conn:
            static_conn.close()
        if dynamic_conn:
            dynamic_conn.close()
        if trakt_queue_conn:
            trakt_queue_conn.close()


async def sync_dropped_shows(trakt_handler, username, tvshows_static_db_path):
    if not username:
        log("[Orac] Skipping dropped shows sync: No username provided", level=LOGWARNING)
        return
    try:
        # Step 1: Fetch dropped items from Trakt
        dropped_resp = await trakt_handler.get("/users/hidden/dropped?limit=1000")
        if dropped_resp is None:
            log(f"[Orac] No response received when fetching dropped shows for {username}", level=LOGERROR)
            return
        if dropped_resp.status_code != 200:
            log(f"[Orac] Failed to fetch dropped shows for {username}: {dropped_resp.status_code}", level=LOGERROR)
            return

        dropped_data = dropped_resp.json()
        trakt_dropped_ids = {
            item["show"]["ids"]["trakt"]
            for item in dropped_data
            if item.get("show") and item["show"].get("ids", {}).get("trakt")
        }

        # Step 2: Open DB and fetch local dropped IDs
        conn = sqlite3.connect(tvshows_static_db_path)
        cursor = conn.cursor()

        # If the 'dropped' column doesn't exist, attempt to add it (safe to ignore failure if already exists)
        try:
            cursor.execute("ALTER TABLE shows ADD COLUMN dropped INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists

        cursor.execute("SELECT show_trakt_id FROM shows WHERE dropped = 1")
        db_dropped_ids = {row[0] for row in cursor.fetchall()}

        # Step 3: Compare sets
        to_mark_dropped = trakt_dropped_ids - db_dropped_ids
        to_undrop = db_dropped_ids - trakt_dropped_ids

        # Step 4: Update DB
        if to_mark_dropped:
            cursor.executemany("UPDATE shows SET dropped = 1 WHERE show_trakt_id = ?", [(tid,) for tid in to_mark_dropped])
        if to_undrop:
            cursor.executemany("UPDATE shows SET dropped = 0 WHERE show_trakt_id = ?", [(tid,) for tid in to_undrop])

        conn.commit()

        log(f"[Orac] Dropped shows updated: {len(to_mark_dropped)} marked dropped, {len(to_undrop)} reset", level=LOGINFO)

    except Exception as e:
        log(f"[Orac] Error during dropped show sync: {e}", level=LOGERROR)

    finally:
        if 'conn' in locals():
            conn.close()

def _update_show_watched_status(dynamic_cursor, static_cursor, user, show_tmdb_id):
    """Calculates and updates the tri-state watched status for a TV show."""
    # 0: Unwatched, 1: In Progress, 2: Watched
    
    # Get total episodes from static DB
    # Note: episodes.show_id is now the show_tmdb_id
    static_cursor.execute("SELECT tmdb_id FROM episodes WHERE show_id = ?", (show_tmdb_id,))
    rows = static_cursor.fetchall()
    total_count = len(rows)
    
    if total_count == 0:
        status = 0
    else:
        tmdb_ids = [row[0] for row in rows]
        # Count episodes in watched_episodes for this show and user
        placeholders = ','.join(['?' for _ in tmdb_ids])
        dynamic_cursor.execute(f"""
            SELECT COUNT(*), MIN(percent_watched)
            FROM watched_episodes
            WHERE user = ? AND tmdb_id IN ({placeholders})
        """, (user, *tmdb_ids))
        
        watched_count, min_percent = dynamic_cursor.fetchone()
        
        if watched_count == 0:
            status = 0
        elif watched_count == total_count and min_percent == 100:
            status = 2
        else:
            status = 1
            
    dynamic_cursor.execute("""
        UPDATE user_show_sync SET watched_status = ? 
        WHERE user = ? AND show_tmdb_id = ?
    """, (status, user, show_tmdb_id))
    
    # If the row doesn't exist, insert it (though it should usually exist)
    if dynamic_cursor.rowcount == 0:
        from datetime import datetime
        dynamic_cursor.execute("""
            INSERT OR IGNORE INTO user_show_sync (user, show_tmdb_id, last_updated_at, watched_status)
            VALUES (?, ?, ?, ?)
        """, (user, show_tmdb_id, datetime.utcnow().isoformat(), status))
        
    log(f"[Orac] TV Show {show_tmdb_id} status calc: watched_count={watched_count}, total_count={total_count}, min_percent={min_percent} -> status={status}", level=LOGDEBUG)
    return status

def update_next_episode(
    static_db_path,
    dynamic_db_path,
    trakt_queue_path,
    trakt_handler,
    tmdb_handler,
    watched_type,
    show_tmdb_id,
    show_trakt_id,
    season,
    episode,
    percent_watched,
    username):

    #Given a watched episode's tmdb_id of the show, season and episode this updates the watched_episodes table for the user.

    #Opens and closes the database connections internally.
#    username = 'karhu69'
    if not username:
        log(f"[Orac] Skipping next episode update for show {show_tmdb_id}: No username provided", level=LOGWARNING)
        return

    log(f"[Orac] Updating next episode for user {username} after watching show {show_tmdb_id}", level=LOGDEBUG)
    log(f"[Orac] Show tmdb_id: {show_tmdb_id}, season: {season}, episode: {episode}", level=LOGDEBUG)
    try:
        with sqlite3.connect(static_db_path) as static_conn, sqlite3.connect(dynamic_db_path) as dynamic_conn, sqlite3.connect(trakt_queue_path) as trakt_queue_conn:
            static_cursor = static_conn.cursor()
            dynamic_cursor = dynamic_conn.cursor()
            trakt_queue_cursor = trakt_queue_conn.cursor()

            # Step 1: Find trakt_id to use as show_id of show. If this is being marked watched because it is the first watch of an episode, this will fail
            
            static_cursor.execute("""
                SELECT show_trakt_id FROM shows WHERE show_tmdb_id = ?
            """, (int(show_tmdb_id),))
            row = static_cursor.fetchone()

            if not row:
                log(f"[Orac] Watched episode tmdb_id {show_tmdb_id} not found in static DB")
                # Must be a new show, so we can add it, plus seasons and episodes
    # Media id is a list of type and id, e.g. "trakt":"1234"
                media_id = {"tmdb": show_tmdb_id,"trakt": show_trakt_id}
                url = f"/search/tmdb/{int(show_tmdb_id)}?type=show&extended=full"
                response = trakt_handler._get(url)
                if response.status_code != 200:
                    log(f"[Orac] Failed to get new show details")
                    return False
                search_results = response.json()
                show_full = search_results[0]
                show_details = show_full.get("show", {})
                show_trakt_id = show_details.get("ids", {}).get("trakt")

                # Build show details for add_tvshow to use
                show = {}
                show = {"genres": show_details.get("genres",[]),
                        "trakt": show_trakt_id,
                        "tmdb": show_tmdb_id,
                        "title": show_details.get("title"),
                        "year": show_details.get("year"),
                        "slug": show_details.get("ids", {}).get("slug"),
                        "overview": show_details.get("overview"),
                        "last_updated": show_details.get("updated_at"),
                        "ids": show_details.get("ids", {})
                }

                add_tvshow(
                    static_cursor,
                    dynamic_cursor,
                    trakt_queue_cursor,
                    media_id,
                    trakt_handler,
                    tmdb_handler,
                    show
                )
                log(f"[Orac] Added show {show_tmdb_id} to static DB and queue for full update.", level=LOGINFO)
            else:
                show_trakt_id = row[0]

            # If show is in DB but has no valid Trakt ID, try to resolve it now
            if show_trakt_id is None or (isinstance(show_trakt_id, int) and show_trakt_id < 0):
                log(f"[Orac] Show {show_tmdb_id} is in static DB but missing a valid Trakt ID. Attempting to resolve on-the-fly...", level=LOGINFO)
                try:
                    url = f"/search/tmdb/{int(show_tmdb_id)}?type=show"
                    response = trakt_handler._get(url)
                    if response.status_code == 200:
                        search_results = response.json()
                        if search_results and isinstance(search_results, list) and len(search_results) > 0:
                            show_details = search_results[0].get("show", {})
                            resolved_trakt_id = show_details.get("ids", {}).get("trakt")
                            if resolved_trakt_id:
                                show_trakt_id = resolved_trakt_id
                                static_cursor.execute("""
                                    UPDATE shows SET show_trakt_id = ? WHERE show_tmdb_id = ?
                                """, (show_trakt_id, int(show_tmdb_id)))
                                static_conn.commit()
                                log(f"[Orac] Successfully resolved and backfilled Trakt ID {show_trakt_id} for show {show_tmdb_id}", level=LOGINFO)
                except Exception as e:
                    log(f"[Orac] Error resolving Trakt ID for show {show_tmdb_id}: {e}", level=LOGERROR)



            # Step 1: Find the episode ids
            static_cursor.execute("""
                SELECT episode_trakt_id, tmdb_id FROM episodes WHERE show_id = ?
                AND season = ? AND episode_number = ?
                LIMIT 1
            """, (show_tmdb_id, season, episode))

            row = static_cursor.fetchone()
            log(f"[Orac] Found row for watched episode: {row}", level=LOGDEBUG)
            if not row:
                log(f"[Orac] Watched episode show_id {show_tmdb_id} season {season} number {episode} not found in static DB", level=LOGERROR)
                return
            episode_trakt_id, episode_tmdb_id = row

            # Step 2: Update watched_episodes
            watched_status = 2 if percent_watched >= 90 else (1 if percent_watched > 0 else 0)
            now_str_ms = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
            dynamic_cursor.execute("""
                INSERT OR REPLACE INTO watched_episodes (user, episode_trakt_id, tmdb_id, season, episode, watched_at, percent_watched, watched_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (username, episode_trakt_id, episode_tmdb_id, season, episode, now_str_ms, percent_watched, watched_status))

            # Insert into watched_history for dual sync (sets trakt_synced_at so we don't double sync Trakt, resets Simkl/MDBList so they get re-synced)
            if percent_watched >= 90:
                dynamic_cursor.execute("""
                    INSERT INTO watched_history (show_tmdb_id, season, episode, is_watched, last_watched_at, trakt_synced_at, simkl_synced_at, mdblist_synced_at)
                    VALUES (?, ?, ?, 1, ?, ?, NULL, NULL)
                    ON CONFLICT(show_tmdb_id, season, episode) DO UPDATE SET
                        is_watched = 1, last_watched_at = ?, trakt_synced_at = ?,
                        simkl_synced_at = NULL, mdblist_synced_at = NULL
                """, (show_tmdb_id, season, episode, now_str_ms, now_str_ms, now_str_ms, now_str_ms))

            # Update show's parent status
            _update_show_watched_status(dynamic_cursor, static_cursor, username, show_tmdb_id)

            # If the percent watched is less than 90, we can finish here
            if percent_watched is not None and percent_watched < 90:
                log(f"[Orac] Episode {episode_tmdb_id} of show {show_tmdb_id} marked as partially watched ({percent_watched}%) for user {username}", level=LOGINFO)
                
                dynamic_conn.commit()
                trakt_queue_conn.commit()
                return

            # Put an entry in the queue to update trakt
            if percent_watched is None:
                percent_watched = 100
            if show_trakt_id is not None and (not isinstance(show_trakt_id, int) or show_trakt_id >= 0):
                payload = {"update_type": "watched_episode", "season": season, "episode": episode, "tmdb_id": episode_tmdb_id, "episode_trakt_id": episode_trakt_id, "percent_watched": percent_watched}
                trakt_queue_cursor.execute("""
                    INSERT INTO update_queue (trakt_id, update_type, payload, status, media_type)
                    VALUES (?, ?, ?, 'pending', ?)
                """, (show_trakt_id, 'watched_episode', json.dumps(payload), 'episode'))
                trakt_queue_conn.commit()
            else:
                log(f"[Orac] Skipping Trakt sync queue for show {show_tmdb_id} (no valid Trakt ID available)", level=LOGINFO)


            dynamic_conn.commit()
            
            # Clear recommendations cache
            recommendations.clear_user_cache(username)

    except Exception as e:
        log(f"[Orac] Error updating next episode: {e}", level=LOGERROR)

async def update_dynamic_movie_data(trakt_handler, tmdb_handler, username, movies_dynamic_db_path, movies_static_db_path, trakt_queue_path):
    static_conn = None
    dynamic_conn = None
    
    if not username:
        log("[Orac] Skipping dynamic movie data update: No username provided", level=LOGWARNING)
        return

    try:
        # Step 1: Get watched movies from Trakt with extended info for metadata
        watched_resp = await trakt_handler.get("/sync/watched/movies?extended=full")
        if watched_resp is None:
            log(f"[Orac] No response received when fetching watched movies for {username}", level=LOGERROR)
            return
        if watched_resp.status_code != 200:
            log(f"[Orac] Failed to fetch watched movies for {username}: {watched_resp.status_code}", level=LOGERROR)
            return

        log(f"[Orac] Updating dynamic movie data for {username}", level=LOGINFO)


        watched_data = watched_resp.json()

        # Step 2: Open DB connections
        static_conn = sqlite3.connect(movies_static_db_path)
        static_cursor = static_conn.cursor()
        dynamic_conn = sqlite3.connect(movies_dynamic_db_path)
        dynamic_cursor = dynamic_conn.cursor()

        for movie_item in watched_data:
            movie = movie_item.get("movie", {})
            movie_trakt_id = movie["ids"]["trakt"]
            movie_tmdb_id = movie["ids"].get("tmdb")
            movie_imdb_id = movie["ids"].get("imdb")
            
            # If TMDB ID is missing, try to resolve it via IMDB ID
            if not movie_tmdb_id and movie_imdb_id:
                 result = tmdb_handler.find_by_external_id(movie_imdb_id, source="imdb_id")
                 if result:
                     movie_tmdb_id = result.get("id")
                     log(f"[Orac] Resolved TMDB ID {movie_tmdb_id} for watched movie (Trakt: {movie_trakt_id}) using IMDB ID {movie_imdb_id}", level=LOGINFO)
            
            if not movie_tmdb_id:
                log(f"[Orac] Skipping watched movie (Trakt: {movie_trakt_id}) due to missing TMDB ID.", level=LOGWARNING)
                continue

            movie_last_updated = movie_item.get("last_updated_at")

            # --- Check and Insert into Static DB if missing ---
            static_cursor.execute("SELECT 1 FROM movies WHERE tmdb_id = ?", (movie_tmdb_id,))
            if not static_cursor.fetchone():
                try:
                    # Insert minimal metadata from Trakt extended response
                    # Note: Trakt extended info doesn't have all TMDB fields (like budget, revenue, etc), 
                    # but has enough for basic listing and genre matching.
                    
                    static_cursor.execute("""
                        INSERT OR IGNORE INTO movies (
                            trakt_id, tmdb_id, title, year, overview, 
                            tagline, released, runtime, country, rating, language, certification, original_title
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        movie_trakt_id, 
                        movie_tmdb_id, 
                        movie.get("title"), 
                        movie.get("year"), 
                        movie.get("overview"),
                        movie.get("tagline"), 
                        movie.get("released"), 
                        movie.get("runtime"), 
                        movie.get("country"), 
                        movie.get("rating"), 
                        movie.get("language"), 
                        movie.get("certification"),
                        movie.get("original_title")
                    ))
                    

                    genres = movie.get("genres", [])
                    for genre in genres:
                        # Normalize to slug format
                        genre = genre.lower().replace(" ", "-")
                        static_cursor.execute("INSERT OR IGNORE INTO genres(name) VALUES(?)", (genre,))
                        static_cursor.execute("INSERT OR IGNORE INTO movie_genres(trakt_id, tmdb_id, genre) VALUES(?, ?, ?)", (movie_trakt_id, movie_tmdb_id, genre))
                    
                    # Fetch full metadata and images from TMDB
                    log(f"[Orac] Fetching full metadata and images for watched movie '{movie.get('title')}' (ID: {movie_tmdb_id})", level=LOGDEBUG)
                    tmdb_handler.update_movie_static_data_from_tmdb(movie_trakt_id, movie_tmdb_id, static_cursor)

                    log(f"[Orac] Synced watched movie '{movie.get('title')}' (ID: {movie_tmdb_id}) to static DB.", level=LOGDEBUG)
                    
                except Exception as e:
                    log(f"[Orac] Error syncing watched movie '{movie.get('title')}' to static DB: {e}", level=LOGWARNING)
            # --------------------------------------------------

            # Update Dynamic DB
            watched_status = 2 # Movies from Trakt watched list are fully watched
            dynamic_cursor.execute("""
                INSERT OR REPLACE INTO movie_status (tmdb_id, trakt_id, watched, user_rating, last_updated, watched_status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (movie_tmdb_id, movie_trakt_id, 100, movie_item.get("rating", None), movie_last_updated, watched_status))

        static_conn.commit()
        dynamic_conn.commit()

        log(f"[Orac] Watched movies updated for {username}", level=LOGINFO)

    except Exception as e:
        log(f"[Orac] Error updating dynamic movie data: {e}", level=LOGERROR)

    finally:
        if static_conn:
            static_conn.close()
        if dynamic_conn:
            dynamic_conn.close()

def mark_movie_watched(static_db_path, dynamic_db_path, trakt_queue_path, trakt_handler, tmdb_handler, movie_tmdb_id, percent_watched, username):
    #Given a watched movie's tmdb_id, this marks it as watched in the dynamic db for the user.
    #Opens and closes the database connections internally.
    if not username:
        log(f"[Orac] Skipping movie watched mark for {movie_tmdb_id}: No username provided", level=LOGWARNING)
        return False

    log(f"[Orac] Marking movie {movie_tmdb_id} as watched for user {username}", level=LOGDEBUG)
    try:
        with sqlite3.connect(static_db_path) as static_conn, sqlite3.connect(dynamic_db_path) as dynamic_conn, sqlite3.connect(trakt_queue_path) as trakt_queue_conn:
            static_cursor = static_conn.cursor()
            dynamic_cursor = dynamic_conn.cursor()
            trakt_queue_cursor = trakt_queue_conn.cursor()

            # Step 1: Find trakt_id to use as movie_id of movie. If this is being marked watched because it is the first watch of a movie, this will fail
            static_cursor.execute("""
                SELECT trakt_id FROM movies WHERE tmdb_id = ?
            """, (int(movie_tmdb_id),))
            row = static_cursor.fetchone()

            if not row:
                log(f"[Orac] Watched movie tmdb_id {movie_tmdb_id} not found in static DB", level=LOGDEBUG)
                # Must be a new movie, so we can add it
# Media id is a list of type and id, e.g. "trakt":"1234"
                url = f"/search/tmdb/{int(movie_tmdb_id)}?type=movie&extended=full"
                response = trakt_handler._get(url)
                if response.status_code != 200:
                    log(f"[Orac] Failed to get new movie details")
                    return False
                search_results = response.json()    
                if not search_results:
                    log(f"[Orac] No search results found for tmdb_id {movie_tmdb_id}", level=LOGDEBUG)
                    return False
                
                # Correctly extract movie details first
                movie_details = search_results[0].get("movie", {})
                if not movie_details:
                    log(f"[Orac] No movie details found for tmdb_id {movie_tmdb_id}", level=LOGINFO)
                    return False
                
                movie_trakt_id = movie_details.get("ids", {}).get("trakt")
                if not movie_trakt_id:
                     log(f"[Orac] No Trakt ID found for tmdb_id {movie_tmdb_id}", level=LOGINFO)
                     return False

                movie = {
                        "trakt": movie_trakt_id,
                        "tmdb": movie_tmdb_id,
                        "title": movie_details.get("title"),
                        "year": movie_details.get("year"),
                        "slug": movie_details.get("ids", {}).get("slug"),
                        "overview": movie_details.get("overview"),
                        "last_updated": movie_details.get("updated_at"),
                        "ids": movie_details.get("ids", {})
                }
                static_cursor.execute("""
                    INSERT OR REPLACE INTO movies (trakt_id, tmdb_id, title, year, overview)
                    VALUES (?, ?, ?, ?, ?)
                """, (movie_trakt_id, int(movie_tmdb_id), movie.get("title"), movie.get("year"), movie.get("overview")))
                
                # Insert Genres
                genres = movie_details.get("genres", [])
                for genre in genres:
                    # Normalize to slug format
                    genre = genre.lower().replace(" ", "-")
                    # Ensure genre exists
                    static_cursor.execute("INSERT OR IGNORE INTO genres(name) VALUES(?)", (genre,))
                    # Link movie <-> genre
                    static_cursor.execute("INSERT OR IGNORE INTO movie_genres(trakt_id, tmdb_id, genre) VALUES(?, ?, ?)", (movie_trakt_id, int(movie_tmdb_id), genre))
                
                static_conn.commit()
                log(f"[Orac] Added movie {movie_tmdb_id} to static DB.", level=LOGINFO)
            else:
                movie_trakt_id = row[0]
                log(f"[Orac] Found existing movie {movie_tmdb_id} in static DB.", level=LOGINFO)
                # Handle case where movie exists in static DB but has no trakt_id (e.g. added from TMDB-only source)
                if not movie_trakt_id:
                    log(f"[Orac] Static DB movie {movie_tmdb_id} has no trakt_id — fetching from Trakt.", level=LOGINFO)
                    url = f"/search/tmdb/{int(movie_tmdb_id)}?type=movie&extended=full"
                    response = trakt_handler._get(url)
                    if response and response.status_code == 200:
                        search_results = response.json()
                        if search_results:
                            movie_details = search_results[0].get("movie", {})
                            movie_trakt_id = movie_details.get("ids", {}).get("trakt")
                            if movie_trakt_id:
                                # Backfill trakt_id into static DB
                                static_cursor.execute("UPDATE movies SET trakt_id = ? WHERE tmdb_id = ?", (movie_trakt_id, int(movie_tmdb_id)))
                                static_conn.commit()
                                log(f"[Orac] Resolved and backfilled trakt_id {movie_trakt_id} for movie {movie_tmdb_id}", level=LOGINFO)
                    if not movie_trakt_id:
                        log(f"[Orac] Could not resolve trakt_id for movie {movie_tmdb_id} — Trakt queue entry will be skipped.", level=LOGWARNING)
            # Step 2: Update movie_status
            watched_status = 2 if percent_watched >= 90 else (1 if percent_watched > 0 else 0)
            now_str_ms = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
            dynamic_cursor.execute("""
                INSERT OR REPLACE INTO movie_status (tmdb_id, trakt_id, watched, user_rating, last_updated, watched_status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (int(movie_tmdb_id), movie_trakt_id, percent_watched, None, now_str_ms, watched_status))

            # Insert into watched_history for dual sync
            if percent_watched >= 90:
                dynamic_cursor.execute("""
                    INSERT INTO watched_history (tmdb_id, is_watched, last_watched_at, trakt_synced_at, simkl_synced_at)
                    VALUES (?, 1, ?, NULL, NULL)
                    ON CONFLICT(tmdb_id) DO UPDATE SET
                        is_watched = 1, last_watched_at = ?, trakt_synced_at = NULL, simkl_synced_at = NULL
                """, (int(movie_tmdb_id), now_str_ms, now_str_ms))
            
                # Queue Trakt update (only if we have a valid trakt_id)
                payload = {
                    "update_type": "watched_movie",
                    "tmdb_id": int(movie_tmdb_id),
                    "percent_watched": percent_watched
                }
                if movie_trakt_id:
                    trakt_queue_cursor.execute("""
                        INSERT INTO update_queue (trakt_id, update_type, payload, status, media_type)
                        VALUES (?, ?, ?, 'pending', 'movie')
                    """, (movie_trakt_id, 'watched_movie', json.dumps(payload)))
                else:
                    log(f"[Orac] Skipping Trakt queue entry for movie {movie_tmdb_id}: no trakt_id available.", level=LOGWARNING)
            
            dynamic_conn.commit()
            trakt_queue_conn.commit()
            log(f"[Orac] Movie {movie_tmdb_id} marked as watched for user {username}", level=LOGINFO)
            
            # Clear recommendations cache to remove this movie from suggestions
            recommendations.clear_user_cache(username)

    except Exception as e:
        log(f"[Orac] Error marking movie as watched: {e}", level=LOGERROR)

def mark_tvshow_watched(static_db_path, dynamic_db_path, trakt_queue_path, trakt_handler, tmdb_handler, show_tmdb_id, percent_watched, username):
    """Marks an entire TV show as watched in the dynamic DB and queues updates for Trakt."""
    if not username:
        log(f"[Orac] Skipping TV show watched mark for {show_tmdb_id}: No username provided", level=LOGWARNING)
        return False

    log(f"[Orac] Marking TV show {show_tmdb_id} as watched for user {username}", level=LOGDEBUG)
    try:
        with sqlite3.connect(static_db_path) as static_conn, sqlite3.connect(dynamic_db_path) as dynamic_conn, sqlite3.connect(trakt_queue_path) as trakt_queue_conn:
            static_cursor = static_conn.cursor()
            dynamic_cursor = dynamic_conn.cursor()
            trakt_queue_cursor = trakt_queue_conn.cursor()

            # Step 1: Check if show exists in static DB
            static_cursor.execute("SELECT show_trakt_id FROM shows WHERE show_tmdb_id = ?", (int(show_tmdb_id),))
            row = static_cursor.fetchone()

            if not row:
                log(f"[Orac] Show with TMDB ID {show_tmdb_id} not found in static DB. Adding it now.", level=LOGINFO)
                # Fetch show from Trakt
                url = f"/search/tmdb/{int(show_tmdb_id)}?type=show&extended=full"
                response = trakt_handler._get(url)
                if response.status_code != 200:
                    log(f"[Orac] Failed to get show details from Trakt for TMDB ID {show_tmdb_id}", level=LOGERROR)
                    return False
                
                search_results = response.json()
                if not search_results:
                    log(f"[Orac] No results found on Trakt for TMDB ID {show_tmdb_id}", level=LOGERROR)
                    return False
                
                show_details = search_results[0].get("show", {})
                show_trakt_id = show_details.get("ids", {}).get("trakt")
                
                # Build show object for add_tvshow
                show_obj = {
                    "genres": show_details.get("genres", []),
                    "trakt": show_trakt_id,
                    "tmdb": show_tmdb_id,
                    "title": show_details.get("title"),
                    "year": show_details.get("year"),
                    "slug": show_details.get("ids", {}).get("slug"),
                    "overview": show_details.get("overview"),
                    "last_updated": show_details.get("updated_at"),
                    "ids": show_details.get("ids", {})
                }
                
                # Use add_tvshow to populate shows, seasons, and episodes
                media_id = {"tmdb": show_tmdb_id, "trakt": show_trakt_id}
                add_tvshow(static_cursor, dynamic_cursor, trakt_queue_cursor, media_id, trakt_handler, tmdb_handler, show_obj)
                static_conn.commit()
                log(f"[Orac] Successfully added show {show_tmdb_id} and its seasons/episodes to static DB", level=LOGINFO)
            else:
                show_trakt_id = row[0]

            # If show is in DB but has no valid Trakt ID, try to resolve it now
            if show_trakt_id is None or (isinstance(show_trakt_id, int) and show_trakt_id < 0):
                log(f"[Orac] Show {show_tmdb_id} is in static DB but missing a valid Trakt ID. Attempting to resolve on-the-fly...", level=LOGINFO)
                try:
                    url = f"/search/tmdb/{int(show_tmdb_id)}?type=show"
                    response = trakt_handler._get(url)
                    if response.status_code == 200:
                        search_results = response.json()
                        if search_results and isinstance(search_results, list) and len(search_results) > 0:
                            show_details = search_results[0].get("show", {})
                            resolved_trakt_id = show_details.get("ids", {}).get("trakt")
                            if resolved_trakt_id:
                                show_trakt_id = resolved_trakt_id
                                static_cursor.execute("""
                                    UPDATE shows SET show_trakt_id = ? WHERE show_tmdb_id = ?
                                """, (show_trakt_id, int(show_tmdb_id)))
                                static_conn.commit()
                                log(f"[Orac] Successfully resolved and backfilled Trakt ID {show_trakt_id} for show {show_tmdb_id}", level=LOGINFO)
                except Exception as e:
                    log(f"[Orac] Error resolving Trakt ID for show {show_tmdb_id}: {e}", level=LOGERROR)

            # Step 2: Fetch all episodes for this show from static DB
            static_cursor.execute("""
                SELECT episode_trakt_id, tmdb_id, season, episode_number 
                FROM episodes 
                WHERE show_id = ?
            """, (show_tmdb_id,))
            episodes = static_cursor.fetchall()

            # Step 3: Mark each episode as watched in dynamic DB and queue Trakt update
            watched_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
            watched_status = 2 if percent_watched >= 90 else 1
            for ep_trakt_id, ep_tmdb_id, season_num, ep_num in episodes:
                # Mark watched in dynamic DB
                dynamic_cursor.execute("""
                    INSERT OR REPLACE INTO watched_episodes (user, episode_trakt_id, tmdb_id, season, episode, watched_at, percent_watched, watched_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (username, ep_trakt_id, ep_tmdb_id, season_num, ep_num, watched_at, percent_watched, watched_status))

                if percent_watched >= 90:
                    # Insert into watched_history for dual sync (sets trakt_synced_at so we don't double sync Trakt, resets Simkl/MDBList so they get re-synced)
                    dynamic_cursor.execute("""
                        INSERT INTO watched_history (show_tmdb_id, season, episode, is_watched, last_watched_at, trakt_synced_at, simkl_synced_at, mdblist_synced_at)
                        VALUES (?, ?, ?, 1, ?, ?, NULL, NULL)
                        ON CONFLICT(show_tmdb_id, season, episode) DO UPDATE SET
                            is_watched = 1, last_watched_at = ?, trakt_synced_at = ?,
                            simkl_synced_at = NULL, mdblist_synced_at = NULL
                    """, (show_tmdb_id, season_num, ep_num, watched_at, watched_at, watched_at, watched_at))

                    # If percent_watched is >= 90, queue Trakt update
                    payload = {
                        "update_type": "watched_episode",
                        "season": season_num,
                        "episode": ep_num,
                        "tmdb_id": ep_tmdb_id,
                        "episode_trakt_id": ep_trakt_id,
                        "percent_watched": percent_watched
                    }
                    if show_trakt_id is not None and (not isinstance(show_trakt_id, int) or show_trakt_id >= 0):
                        trakt_queue_cursor.execute("""
                            INSERT INTO update_queue (trakt_id, update_type, payload, status, media_type)
                            VALUES (?, ?, ?, 'pending', ?)
                        """, (show_trakt_id, 'watched_episode', json.dumps(payload), 'episode'))
                    else:
                        log(f"[Orac] Skipping Trakt sync queue for show {show_tmdb_id} (no valid Trakt ID available)", level=LOGINFO)

            # Step 4: Update show's parent status
            _update_show_watched_status(dynamic_cursor, static_cursor, username, show_tmdb_id)

            dynamic_conn.commit()
            trakt_queue_conn.commit()
            log(f"[Orac] Successfully marked all {len(episodes)} episodes of show {show_tmdb_id} as watched ({percent_watched}%)", level=LOGINFO)
            
            # Clear recommendations cache
            recommendations.clear_user_cache(username)
            return True

    except Exception as e:
        log(f"[Orac] Error in mark_tvshow_watched: {e}", level=LOGERROR)
        return False

def drop_tvshow(static_db_path, dynamic_db_path, trakt_queue_path, trakt_handler, show_tmdb_id, username):
    """
    Mark a TV show as dropped.
    1. Updates local database to set dropped=1
    2. Queues a request to Trakt to add it to hidden/dropped items
    3. Simkl bulk sync or list sync should handle dropped status, but we will add an immediate job for Simkl or Trakt if Orac supports it.
    """
    try:
        with sqlite3.connect(static_db_path) as static_conn, \
             sqlite3.connect(trakt_queue_path) as trakt_queue_conn:
            
            static_cursor = static_conn.cursor()
            trakt_queue_cursor = trakt_queue_conn.cursor()

            # Step 1: Find the show's Trakt ID
            static_cursor.execute("SELECT show_trakt_id FROM shows WHERE show_tmdb_id = ?", (show_tmdb_id,))
            row = static_cursor.fetchone()
            if not row:
                log(f"[Orac] Could not find TV show {show_tmdb_id} in static database to drop", level=LOGWARNING)
                return False
            show_trakt_id = row[0]

            # Step 2: Mark as dropped in the static db
            try:
                static_cursor.execute("ALTER TABLE shows ADD COLUMN dropped INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already exists
                
            static_cursor.execute("UPDATE shows SET dropped = 1 WHERE show_tmdb_id = ?", (show_tmdb_id,))
            static_conn.commit()

            # Step 3: Queue Trakt update
            if show_trakt_id is not None and (not isinstance(show_trakt_id, int) or show_trakt_id >= 0):
                payload = {
                    "update_type": "drop_show",
                    "shows": [{"ids": {"trakt": show_trakt_id}}]
                }
                trakt_queue_cursor.execute("""
                    INSERT INTO update_queue (trakt_id, update_type, payload, status, media_type)
                    VALUES (?, ?, ?, 'pending', ?)
                """, (show_trakt_id, 'drop_show', json.dumps(payload), 'show'))
                trakt_queue_conn.commit()
            else:
                log(f"[Orac] Skipping Trakt drop show sync for show {show_tmdb_id} (no valid Trakt ID available)", level=LOGINFO)
            log(f"[Orac] Successfully marked TV show {show_tmdb_id} as dropped", level=LOGINFO)

            # Note: The actual API push to Simkl and Trakt happens in the queue worker or sync engine.
            # Simkl bulk sync will need to be told this is dropped, or we queue a direct Simkl push in `queue_worker.py`.
            return True

    except Exception as e:
        log(f"[Orac] Error in drop_tvshow: {e}", level=LOGERROR)
        return False