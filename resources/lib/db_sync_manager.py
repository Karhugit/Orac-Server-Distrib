from resources.lib.sync_trakt_with_db import trakt_list_sync_task, sync_recent_tvshow_updates, sync_recent_tmdb_tv_changes
from time import time
from resources.lib.log_utils import log, LOGDEBUG, LOGINFO, LOGERROR, LOGWARNING
from datetime import datetime
import sqlite3
from resources.lib.watched import update_dynamic_tvshow_data, sync_dropped_shows, update_dynamic_movie_data
from resources.lib.flixpatrol_sync import FlixPatrolSync
from resources.lib.mdblist_list_sync import mdblist_list_sync_task

async def cleanup_static_databases(movies_static_db_path, movies_dynamic_db_path, tvshows_static_db_path, tvshows_dynamic_db_path, lists_db_path, tags_db_path=None):
    """
    Identifies and removes orphaned items from static databases.
    An item is orphaned if it's not in any list, collection, or watch history.
    """
    log("[Orac] **GC** Starting garbage collection for static databases...", level=LOGINFO)
    gc_start_time = time()

    active_movie_ids = set()
    active_show_ids = set()

    try:
        # --- Step 1: Aggregate all active media IDs ---

        # 1. Collect all referenced Trakt IDs from Lists DB
        with sqlite3.connect(lists_db_path) as conn:
            cursor = conn.cursor()
            
            # Movies - Handle None/Null/Empty strings
            cursor.execute("""
                SELECT li.trakt_id
                FROM list_items li
                JOIN lists l ON li.list_id = l.list_id
                WHERE li.media_type = 'movie' AND l.add_to_library = 1
            """)
            
            for row in cursor.fetchall():
                 try:
                     if row[0]:
                         active_movie_ids.add(str(row[0])) # Store as string for comparing with set
                 except Exception:
                     pass

            cursor.execute("""
                SELECT li.trakt_id
                FROM list_items li
                JOIN lists l ON li.list_id = l.list_id
                WHERE li.media_type = 'show' AND l.add_to_library = 1
            """)
            for row in cursor.fetchall():
                 try:
                     if row[0]:
                         active_show_ids.add(str(row[0]))
                 except Exception:
                     pass

        # From movie watch history
        with sqlite3.connect(movies_dynamic_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trakt_id FROM movie_status WHERE watched > 0")
            for row in cursor.fetchall():
                if row[0]:
                    active_movie_ids.add(str(row[0]))

        # From TV show watch history
        with sqlite3.connect(tvshows_dynamic_db_path) as conn:
            cursor = conn.cursor()
            # We need to join with the static DB to get the show_trakt_id
            conn.execute(f"ATTACH DATABASE '{tvshows_static_db_path}' AS static_db")
            # Update: episodes.show_id references shows.show_tmdb_id now
            cursor.execute("SELECT DISTINCT s.show_trakt_id FROM static_db.episodes e JOIN watched_episodes we ON e.tmdb_id = we.tmdb_id JOIN static_db.shows s ON e.show_id = s.show_tmdb_id")
            for row in cursor.fetchall():
                if row[0]:
                    active_show_ids.add(str(row[0]))
            conn.execute("DETACH DATABASE static_db")

        # From tags database - tagged items should not be garbage collected
        try:
            with sqlite3.connect(tags_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT trakt_id FROM tag_items WHERE media_type = 'movie' AND trakt_id IS NOT NULL")
                for row in cursor.fetchall():
                    if row[0]:
                        active_movie_ids.add(str(row[0]))
                
                cursor.execute("SELECT trakt_id FROM tag_items WHERE media_type = 'show' AND trakt_id IS NOT NULL")
                for row in cursor.fetchall():
                    if row[0]:
                        active_show_ids.add(str(row[0]))
        except Exception as e:
            log(f"[Orac] **GC** Warning: Could not load tagged items from tags DB: {e}", level=LOGWARNING)

        # --- Step 2: Get all IDs from static databases ---
        with sqlite3.connect(movies_static_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trakt_id FROM movies")
            static_movie_ids = {str(row[0]) for row in cursor.fetchall() if row[0]}

        with sqlite3.connect(tvshows_static_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT show_trakt_id FROM shows")
            static_show_ids = {str(row[0]) for row in cursor.fetchall() if row[0]}

        # --- Step 3: Identify orphans ---
        orphaned_movie_ids = static_movie_ids - active_movie_ids
        orphaned_show_ids = static_show_ids - active_show_ids

        # --- Step 4: Execute deletions and VACUUM ---
        if orphaned_movie_ids:
            log(f"[Orac] **GC** Removing {len(orphaned_movie_ids)} orphaned movies.", level=LOGINFO)
            with sqlite3.connect(movies_static_db_path) as conn:
                # Filter out any non-integer strings if present, though IDs should be ints
                safe_ids = []
                for mid in orphaned_movie_ids:
                    try:
                        safe_ids.append((int(mid),))
                    except (ValueError, TypeError):
                        pass
                
                if safe_ids:
                    conn.executemany("DELETE FROM movies WHERE trakt_id = ?", safe_ids)
            
            # Clean up dynamic DB (movie_status)
            log(f"[Orac] **GC** Removing {len(safe_ids)} orphaned entries from movie_status (dynamic DB).", level=LOGINFO)
            with sqlite3.connect(movies_dynamic_db_path) as conn:
                conn.executemany("DELETE FROM movie_status WHERE trakt_id = ?", safe_ids)
                conn.commit()
                conn.execute("VACUUM")
            
            # VACUUM must be run outside of a transaction
            with sqlite3.connect(movies_static_db_path) as conn:
                conn.execute("VACUUM")

        if orphaned_show_ids:
            log(f"[Orac] **GC** Removing {len(orphaned_show_ids)} orphaned shows.", level=LOGINFO)
            with sqlite3.connect(tvshows_static_db_path) as conn:
                safe_ids = []
                for sid in orphaned_show_ids:
                     try:
                         safe_ids.append((int(sid),))
                     except (ValueError, TypeError):
                         pass
                if safe_ids:
                    conn.executemany("DELETE FROM shows WHERE show_trakt_id = ?", safe_ids)
            # VACUUM must be run outside of a transaction
            with sqlite3.connect(tvshows_static_db_path) as conn:
                conn.execute("VACUUM")

        log(f"[Orac] **GC** Garbage collection finished in {time() - gc_start_time:.2f} seconds.", level=LOGINFO)

    except Exception as e:
        log(f"[Orac] **GC** Error during garbage collection: {e}", level=LOGERROR)

async def sync_lists_and_items(trakt_handler, tmdb_handler, movies_static_db_path, movies_dynamic_db_path, tvshows_static_db_path, tvshows_dynamic_db_path, lists_db_path, 
                                trakt_update_queue_path, trakt_queue_worker, username=None, external_indexes_db_path=None, config_db_path=None, **kwargs):
    # Sync lists and items to local DB
    if not username and config_db_path:
        from .config_handler import get_trakt_user
        username = get_trakt_user(config_db_path)
    
    if not username and trakt_handler:
        username = await trakt_handler.fetch_username()
    
    if not username:
        log("[Orac] **SYNC** Starting sync process without Trakt username. Some user-specific data might be skipped.", level=LOGWARNING)
    else:
        log(f"[Orac] **SYNC** Starting sync process for user: {username}", level=LOGINFO)
    
    start_time = time()
    await trakt_list_sync_task(
        trakt_handler,
        tmdb_handler,
        lists_db_path,
        movies_static_db_path,
        movies_dynamic_db_path,
        tvshows_static_db_path,
        tvshows_dynamic_db_path,
        trakt_update_queue_path,
        username=username,
        external_indexes_db_path=external_indexes_db_path
    )
    log(f"[Orac] Lists sync completed in {time() - start_time:.2f} seconds", level=LOGINFO)

    # Sync TMDB Lists if configured
    if config_db_path:
        from .config_handler import get_tmdb_user, get_tmdb_session_id
        tmdb_user = get_tmdb_user(config_db_path)
        tmdb_session_id = get_tmdb_session_id(config_db_path)

        if tmdb_user and tmdb_session_id:
            from resources.lib.tmdb_list_sync import tmdb_list_sync_task
            log(f"[Orac] **SYNC** Starting TMDB sync process for user: {tmdb_user}", level=LOGINFO)
            tmdb_start_time = time()
            await tmdb_list_sync_task(
                trakt_handler,
                tmdb_handler,
                tmdb_user,
                tmdb_session_id,
                lists_db_path,
                movies_static_db_path,
                movies_dynamic_db_path,
                tvshows_static_db_path,
                tvshows_dynamic_db_path,
                trakt_update_queue_path
            )
            log(f"[Orac] TMDB lists sync completed in {time() - tmdb_start_time:.2f} seconds", level=LOGINFO)

    # Sync Simkl Lists
    if config_db_path:
        from resources.lib.simkl_list_sync import simkl_list_sync_task
        log(f"[Orac] **SYNC** Starting Simkl lists sync process", level=LOGINFO)
        simkl_start_time = time()
        await simkl_list_sync_task(
            config_db_path,
            lists_db_path,
            trakt_handler,
            tmdb_handler,
            movies_static_db_path,
            movies_dynamic_db_path,
            tvshows_static_db_path,
            tvshows_dynamic_db_path,
            trakt_update_queue_path
        )
        log(f"[Orac] Simkl lists sync completed in {time() - simkl_start_time:.2f} seconds", level=LOGINFO)

    # Sync MDBList Lists
    if config_db_path:
        log(f"[Orac] **SYNC** Starting MDBList sync process", level=LOGINFO)
        mdblist_start_time = time()
        await mdblist_list_sync_task(
            config_db_path,
            lists_db_path,
            movies_static_db_path=movies_static_db_path,
            movies_dynamic_db_path=movies_dynamic_db_path,
            tvshows_static_db_path=tvshows_static_db_path,
            tvshows_dynamic_db_path=tvshows_dynamic_db_path,
            trakt_update_queue_path=trakt_update_queue_path,
            trakt_handler=trakt_handler,
            tmdb_handler=tmdb_handler,
        )
        log(f"[Orac] MDBList sync completed in {time() - mdblist_start_time:.2f} seconds", level=LOGINFO)

    # Sync recent TV show updates to static DB (Trakt Source)
    trakt_updates_start = time()
    await sync_recent_tvshow_updates(
        trakt_handler,
        tmdb_handler, 
        tvshows_static_db_path,
        trakt_update_queue_path,
        config_db_path=config_db_path
    )
    log(f"[Orac] Recent TV show updates (Trakt) sync completed in {time() - trakt_updates_start:.2f} seconds", level=LOGINFO)

    # Sync recent TV show changes to static DB (TMDB Source)
    tmdb_updates_start = time()
    await sync_recent_tmdb_tv_changes(
        trakt_handler,
        tmdb_handler,
        tvshows_static_db_path,
        trakt_update_queue_path,
        config_db_path=config_db_path
    )
    log(f"[Orac] Recent TV show updates (TMDB) sync completed in {time() - tmdb_updates_start:.2f} seconds", level=LOGINFO)


    # Stop queue worker just before updating dynamic TV show data
    if trakt_queue_worker:
        log("[Orac] Pausing TraktQueueWorker before updating dynamic TV show data", level=LOGINFO)
        trakt_queue_worker.pause()

    try:
        await update_dynamic_tvshow_data(
            trakt_handler,
            tmdb_handler,
            username,
            tvshows_dynamic_db_path,
            tvshows_static_db_path,
            trakt_update_queue_path
        )
        await sync_dropped_shows(trakt_handler, username, tvshows_static_db_path)
        await update_dynamic_movie_data(
            trakt_handler,
            tmdb_handler,
            username,
            movies_dynamic_db_path,
            movies_static_db_path,
            trakt_update_queue_path
        )
        
        # Dual-Provider Watch History reconciliation
        try:
            from resources.lib.sync_engine import sync_providers
            await sync_providers(movies_dynamic_db_path, tvshows_dynamic_db_path, trakt_handler, config_db_path)
        except Exception as e:
            log(f"[Orac] Error running dual-provider history sync: {e}", level=LOGERROR)
            
    finally:
        if trakt_queue_worker:
            log("[Orac] Restarting TraktQueueWorker after updating dynamic TV show data", level=LOGINFO)
            trakt_queue_worker.resume()

    # Run FlixPatrol Sync
    # Run FlixPatrol Sync
    try:
        # Check if we need to run FlixPatrol sync (Daily check)
        should_run_flixpatrol = True
        try:
            with sqlite3.connect(lists_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT last_checked FROM lists WHERE slug = ?", ('flixpatrol-netflix-movie',))
                row = cursor.fetchone()
                if row and row[0]:
                    last_checked_str = row[0]
                    # Dates are stored as "2026-02-01T18:42:54.000Z"
                    try:
                        last_checked_dt = datetime.strptime(last_checked_str, "%Y-%m-%dT%H:%M:%S.000Z")
                        time_diff = (datetime.utcnow() - last_checked_dt).total_seconds()
                        if time_diff < 86400: # 24 hours
                            should_run_flixpatrol = False
                            log(f"[Orac] Skipping FlixPatrol sync (Last checked: {last_checked_str}, {time_diff/3600:.1f} hours ago)", level=LOGINFO)
                    except ValueError:
                        pass # proceed if date format is weird
        except Exception as e:
            log(f"[Orac] Error checking FlixPatrol last_checked status: {e}", level=LOGWARNING)

        if should_run_flixpatrol:
            log("[Orac] Starting FlixPatrol Sync...", level=LOGINFO)
            flix_sync = FlixPatrolSync(
                tmdb_handler, 
                lists_db_path,
                movies_static_db_path,
                movies_dynamic_db_path,
                tvshows_static_db_path,
                tvshows_dynamic_db_path,
                trakt_update_queue_path
            )
            await flix_sync.run_sync()

    except Exception as e:
        log(f"[Orac] Error running FlixPatrol sync: {e}", level=LOGERROR)

    # Finally, run garbage collection to clean up orphaned static entries
    # Note: tags_db_path should be passed from caller, but for backward compatibility, default to None
    tags_db_path = kwargs.get('tags_db_path')
    await cleanup_static_databases(
        movies_static_db_path,
        movies_dynamic_db_path,
        tvshows_static_db_path,
        tvshows_dynamic_db_path,
        lists_db_path,
        tags_db_path=tags_db_path
    )
