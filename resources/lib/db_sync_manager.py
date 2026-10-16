from resources.lib.sync_trakt_with_db import trakt_list_sync_task, sync_recent_tvshow_updates, sync_recent_tmdb_tv_changes
from resources.lib.db_utils import db_connect
from time import time
from resources.lib.log_utils import log, LOGDEBUG, LOGINFO, LOGERROR, LOGWARNING
from datetime import datetime
import sqlite3
from resources.lib.watched import update_dynamic_tvshow_data, sync_dropped_shows, update_dynamic_movie_data
from resources.lib.mdblist_list_sync import mdblist_list_sync_task
from resources.lib.trakt_to_mdblist_sync import sync_trakt_lists_to_mdblist_task


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
        with db_connect(lists_db_path) as conn:
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
        with db_connect(movies_dynamic_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trakt_id FROM movie_status WHERE watched > 0")
            for row in cursor.fetchall():
                if row[0]:
                    active_movie_ids.add(str(row[0]))

        # From TV show watch history
        with db_connect(tvshows_dynamic_db_path) as conn:
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
            with db_connect(tags_db_path) as conn:
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
        with db_connect(movies_static_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trakt_id FROM movies")
            static_movie_ids = {str(row[0]) for row in cursor.fetchall() if row[0]}

        with db_connect(tvshows_static_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT show_trakt_id FROM shows")
            static_show_ids = {str(row[0]) for row in cursor.fetchall() if row[0]}

        # --- Step 3: Identify orphans ---
        orphaned_movie_ids = static_movie_ids - active_movie_ids
        orphaned_show_ids = static_show_ids - active_show_ids

        # --- Step 4: Execute deletions and VACUUM ---
        if orphaned_movie_ids:
            log(f"[Orac] **GC** Removing {len(orphaned_movie_ids)} orphaned movies.", level=LOGINFO)
            with db_connect(movies_static_db_path) as conn:
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
            with db_connect(movies_dynamic_db_path) as conn:
                conn.executemany("DELETE FROM movie_status WHERE trakt_id = ?", safe_ids)
                conn.commit()
                conn.execute("VACUUM")
            
            # VACUUM must be run outside of a transaction
            with db_connect(movies_static_db_path) as conn:
                conn.execute("VACUUM")

        if orphaned_show_ids:
            log(f"[Orac] **GC** Removing {len(orphaned_show_ids)} orphaned shows.", level=LOGINFO)
            with db_connect(tvshows_static_db_path) as conn:
                safe_ids = []
                for sid in orphaned_show_ids:
                     try:
                         safe_ids.append((int(sid),))
                     except (ValueError, TypeError):
                         pass
                if safe_ids:
                    conn.executemany("DELETE FROM shows WHERE show_trakt_id = ?", safe_ids)
            # VACUUM must be run outside of a transaction
            with db_connect(tvshows_static_db_path) as conn:
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

        # Sync Trakt Lists & Collections to MDBList
        from resources.lib.config_handler import get_config_value
        trakt_to_mdb_sync = get_config_value("trakt_to_mdblist_sync", config_db_path, "false") == "true"
        if trakt_to_mdb_sync:
            log(f"[Orac] **SYNC** Starting Trakt -> MDBList list and collection sync", level=LOGINFO)
            t_to_m_start_time = time()
            await sync_trakt_lists_to_mdblist_task(
                config_db_path,
                trakt_handler,
                lists_db_path=lists_db_path,
                tmdb_handler=tmdb_handler
            )
            log(f"[Orac] Trakt -> MDBList sync completed in {time() - t_to_m_start_time:.2f} seconds", level=LOGINFO)
        else:
            log(f"[Orac] **SYNC** Trakt -> MDBList sync is disabled in settings. Skipping.", level=LOGINFO)

    # Sync PunchPlay Lists
    if config_db_path:
        from resources.lib.punchplay_list_sync import punchplay_list_sync_task
        log(f"[Orac] **SYNC** Starting PunchPlay lists sync process", level=LOGINFO)
        punchplay_start_time = time()
        await punchplay_list_sync_task(
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
        log(f"[Orac] PunchPlay lists sync completed in {time() - punchplay_start_time:.2f} seconds", level=LOGINFO)


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

    # Sync IntroDB segment timestamps to static DB
    introdb_start = time()
    try:
        from resources.lib.introdb_client import sync_introdb_segments
        import asyncio
        await asyncio.to_thread(
            sync_introdb_segments,
            tvshows_static_db_path,
            tmdb_handler=tmdb_handler,
            max_episodes=500,
            dynamic_db_path=tvshows_dynamic_db_path
        )
    except Exception as e:
        log(f"[Orac] Error in IntroDB segment sync: {e}", level=LOGERROR)
    log(f"[Orac] IntroDB segment sync completed in {time() - introdb_start:.2f} seconds", level=LOGINFO)


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
        
        # Multi-Provider Watch History reconciliation
        try:
            from resources.lib.sync_engine import sync_providers
            await sync_providers(
                movies_dynamic_db_path,
                tvshows_dynamic_db_path,
                trakt_handler,
                config_db_path,
                tvshows_static_db=tvshows_static_db_path,
                tmdb_handler=tmdb_handler
            )
        except Exception as e:
            log(f"[Orac] Error running multi-provider history sync: {e}", level=LOGERROR)
            
    finally:
        if trakt_queue_worker:
            log("[Orac] Restarting TraktQueueWorker after updating dynamic TV show data", level=LOGINFO)
            trakt_queue_worker.resume()

    # Clean up any legacy FlixPatrol entries from lists cache
    try:
        with db_connect(lists_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM list_items WHERE list_id IN (SELECT list_id FROM lists WHERE user = 'flixpatrol' OR source = 'flixpatrol')")
            cursor.execute("DELETE FROM lists WHERE user = 'flixpatrol' OR source = 'flixpatrol'")
            conn.commit()
    except Exception:
        pass

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


def remove_single_list_items(lists_db_path, list_id):
    """
    Removes all items for a single list from list_items without touching static/dynamic DBs
    (garbage collection cleans up any unreferenced media items later).
    """
    try:
        with db_connect(lists_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
            removed = cursor.rowcount
            conn.commit()
        log(f"[Orac] Removed {removed} items from list_items for '{list_id}'", level=LOGINFO)
        return removed
    except Exception as e:
        log(f"[Orac] Error removing list_items for '{list_id}': {e}", level=LOGERROR)
        return 0


async def sync_single_list_items(
    list_id,
    trakt_handler,
    tmdb_handler,
    movies_static_db_path,
    movies_dynamic_db_path,
    tvshows_static_db_path,
    tvshows_dynamic_db_path,
    lists_db_path,
    trakt_update_queue_path,
    username=None,
    external_indexes_db_path=None,
    config_db_path=None
):
    """
    Fetches and syncs items for a single list (into list_items and static/dynamic DBs)
    when the list is switched to 'In Library', avoiding a full 5-minute server resync.
    """
    import asyncio
    start_t = time()
    log(f"[Orac] **SINGLE LIST SYNC** Starting targeted sync for list '{list_id}'...", level=LOGINFO)

    if not username and config_db_path:
        from .config_handler import get_trakt_user
        username = get_trakt_user(config_db_path)
    if not username and trakt_handler:
        username = await trakt_handler.fetch_username()

    # Read list metadata from lists DB
    row_dict = None
    try:
        with db_connect(lists_db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT list_id, source, user, owned_by_user, slug, name, description FROM lists WHERE list_id = ?", (list_id,))
            row = cur.fetchone()
            if row:
                row_dict = dict(row)
    except Exception as e:
        log(f"[Orac] Error reading list metadata for '{list_id}': {e}", level=LOGERROR)

    source = (row_dict.get("source") if row_dict else None) or (str(list_id).split(":")[0] if ":" in str(list_id) else "trakt")
    slug = (row_dict.get("slug") if row_dict else None) or (str(list_id).split(":")[-1] if ":" in str(list_id) else str(list_id))
    list_user = (row_dict.get("user") if row_dict else None) or username or source
    list_name = (row_dict.get("name") if row_dict else None) or slug
    list_desc = (row_dict.get("description") if row_dict else None) or ""
    owned_by_user = bool(row_dict.get("owned_by_user", 1)) if row_dict else True

    from resources.lib.trakt_list_sync import run_list_sync

    try:
        # 1. External Index (tmdb:index:...)
        if str(list_id).startswith("tmdb:index:"):
            from resources.lib.sync_trakt_with_db import (
                get_active_external_indexes,
                fetch_external_movies_index,
                fetch_external_shows_index,
                normalize_custom_lists,
            )
            idx_slug = str(list_id).replace("tmdb:index:", "")
            active_indexes = get_active_external_indexes(external_indexes_db_path) if external_indexes_db_path else []
            target_idx = None
            for idx in active_indexes:
                if idx["id"].lower().replace(" ", "-").replace("_", "-") == idx_slug.lower():
                    target_idx = idx
                    break
            if target_idx:
                if target_idx["media_type"] == "movie":
                    res = await fetch_external_movies_index(trakt_handler, tmdb_handler, target_idx, movies_static_db_path)
                else:
                    res = await fetch_external_shows_index(trakt_handler, tmdb_handler, target_idx, tvshows_static_db_path)
                if isinstance(res, dict) and res.get("items"):
                    for norm_slug, list_meta, list_data in normalize_custom_lists([res], username or "tmdb", owned_by_user=False):
                        await run_list_sync(
                            lists_db_path, username or "tmdb", list_data, norm_slug,
                            movies_static_db_path, movies_dynamic_db_path,
                            tvshows_static_db_path, tvshows_dynamic_db_path,
                            trakt_update_queue_path, trakt_handler, tmdb_handler, list_meta
                        )

        # 2. Trakt Lists
        elif source == "trakt" or str(list_id).startswith("trakt:"):
            from resources.lib.sync_trakt_with_db import (
                get_trakt_watchlist,
                get_trakt_favorites,
                get_trakt_collection_movies,
                get_trakt_collection_tvshows,
                _fetch_all_list_items,
                normalize_watchlist,
                normalize_favorites,
                normalize_collection_movies,
                normalize_collection_tvshows,
                normalize_custom_lists,
            )
            if list_id == "trakt:personal:watchlist":
                wl = await get_trakt_watchlist(trakt_handler)
                if wl is not None:
                    s, ldata = normalize_watchlist(wl, username)
                    await run_list_sync(
                        lists_db_path, username, ldata, s,
                        movies_static_db_path, movies_dynamic_db_path,
                        tvshows_static_db_path, tvshows_dynamic_db_path,
                        trakt_update_queue_path, trakt_handler, tmdb_handler
                    )
            elif list_id == "trakt:personal:favorites":
                fav = await get_trakt_favorites(trakt_handler)
                if fav is not None:
                    s, ldata = normalize_favorites(fav, username)
                    await run_list_sync(
                        lists_db_path, username, ldata, s,
                        movies_static_db_path, movies_dynamic_db_path,
                        tvshows_static_db_path, tvshows_dynamic_db_path,
                        trakt_update_queue_path, trakt_handler, tmdb_handler
                    )
            elif list_id == "trakt:personal:collection-movies":
                cm = await get_trakt_collection_movies(trakt_handler)
                if cm is not None:
                    s, ldata = normalize_collection_movies(cm, username)
                    await run_list_sync(
                        lists_db_path, username, ldata, s,
                        movies_static_db_path, movies_dynamic_db_path,
                        tvshows_static_db_path, tvshows_dynamic_db_path,
                        trakt_update_queue_path, trakt_handler, tmdb_handler
                    )
            elif list_id == "trakt:personal:collection-tvshows":
                ct = await get_trakt_collection_tvshows(trakt_handler)
                if ct is not None:
                    s, ldata = normalize_collection_tvshows(ct, username)
                    await run_list_sync(
                        lists_db_path, username, ldata, s,
                        movies_static_db_path, movies_dynamic_db_path,
                        tvshows_static_db_path, tvshows_dynamic_db_path,
                        trakt_update_queue_path, trakt_handler, tmdb_handler
                    )
            elif str(list_id).startswith("trakt:generic:"):
                from resources.lib.sync_trakt_with_db import get_trakt_generic_lists
                g_lists = await get_trakt_generic_lists(trakt_handler, lists_db_path, {list_id: 1})
                for g_slug, g_meta, g_data in normalize_custom_lists(g_lists, username or "trakt", owned_by_user=False):
                    if g_meta.get("list_id") == list_id or g_slug == slug:
                        await run_list_sync(
                            lists_db_path, username or "trakt", g_data, g_slug,
                            movies_static_db_path, movies_dynamic_db_path,
                            tvshows_static_db_path, tvshows_dynamic_db_path,
                            trakt_update_queue_path, trakt_handler, tmdb_handler, g_meta
                        )
            else:
                # Personal or liked custom Trakt list
                items = await _fetch_all_list_items(trakt_handler, list_user, slug)
                if items is not None:
                    lst = {
                        "name": list_name,
                        "description": list_desc,
                        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "user": {"ids": {"slug": list_user}},
                        "ids": {"slug": slug},
                        "items": items
                    }
                    for c_slug, c_meta, c_data in normalize_custom_lists([lst], username or list_user, owned_by_user=owned_by_user):
                        c_meta["list_id"] = list_id
                        await run_list_sync(
                            lists_db_path, username or list_user, c_data, c_slug,
                            movies_static_db_path, movies_dynamic_db_path,
                            tvshows_static_db_path, tvshows_dynamic_db_path,
                            trakt_update_queue_path, trakt_handler, tmdb_handler, c_meta
                        )

        # 3. TMDb Lists
        elif source == "tmdb" or str(list_id).startswith("tmdb:"):
            from .config_handler import get_tmdb_user, get_tmdb_session_id
            from resources.lib.tmdb_list_sync import (
                normalize_tmdb_watchlist,
                normalize_tmdb_user_list,
                resolve_tmdb_to_trakt,
            )
            tmdb_user = get_tmdb_user(config_db_path) if config_db_path else list_user
            tmdb_session_id = get_tmdb_session_id(config_db_path) if config_db_path else None
            lists_to_sync_data = []

            if list_id == "tmdb:official:watchlist" and tmdb_session_id:
                account_details = tmdb_handler.get_account_details(tmdb_session_id)
                if account_details and "id" in account_details:
                    account_id = account_details["id"]
                    wl_movies, wl_shows = [], []
                    p = 1
                    while True:
                        r = tmdb_handler.get_watchlist_movies(account_id, tmdb_session_id, page=p)
                        if r and "results" in r:
                            wl_movies.extend(r["results"])
                            if p >= r.get("total_pages", 0):
                                break
                            p += 1
                        else:
                            break
                    p = 1
                    while True:
                        r = tmdb_handler.get_watchlist_shows(account_id, tmdb_session_id, page=p)
                        if r and "results" in r:
                            wl_shows.extend(r["results"])
                            if p >= r.get("total_pages", 0):
                                break
                            p += 1
                        else:
                            break
                    merged_meta = None
                    merged_items = []
                    if wl_movies:
                        _, merged_meta, m_items = normalize_tmdb_watchlist(wl_movies, tmdb_user, "movie")
                        merged_items.extend(m_items)
                    if wl_shows:
                        _, s_meta, s_items = normalize_tmdb_watchlist(wl_shows, tmdb_user, "show")
                        merged_items.extend(s_items)
                        if merged_meta:
                            merged_meta["item_count"]["shows"] = len(s_items)
                        else:
                            merged_meta = s_meta
                    if merged_meta:
                        lists_to_sync_data.append(("watchlist", merged_meta, merged_items))

            elif str(list_id).startswith("tmdb:personal:") and tmdb_session_id:
                tmdb_lid = str(list_id).replace("tmdb:personal:", "")
                list_details = None
                all_items = []
                dp = 1
                while True:
                    resp_details = tmdb_handler.get_list_details(tmdb_lid, tmdb_session_id, page=dp)
                    if resp_details and "items" in resp_details:
                        if not list_details:
                            list_details = resp_details
                        all_items.extend(resp_details["items"])
                        if dp >= resp_details.get("total_pages", 1):
                            break
                        dp += 1
                    else:
                        break
                if list_details:
                    list_details["items"] = all_items
                    lists_to_sync_data.append(normalize_tmdb_user_list(list_details, tmdb_user))

            for t_slug, list_meta, list_items in lists_to_sync_data:
                resolved_items = []
                for item in list_items:
                    m_type = item["type"]
                    t_id = item[m_type]["ids"]["tmdb"]
                    with db_connect(movies_static_db_path if m_type == "movie" else tvshows_static_db_path) as conn:
                        trakt_id = await resolve_tmdb_to_trakt(t_id, m_type, trakt_handler, conn.cursor())
                    if not trakt_id:
                        trakt_id = -int(t_id)
                    item[m_type]["ids"]["trakt"] = trakt_id
                    resolved_items.append(item)
                await run_list_sync(
                    lists_db_path, tmdb_user, resolved_items, t_slug,
                    movies_static_db_path, movies_dynamic_db_path,
                    tvshows_static_db_path, tvshows_dynamic_db_path,
                    trakt_update_queue_path, trakt_handler, tmdb_handler, list_meta
                )

        # 4. Simkl Lists
        elif source == "simkl" or str(list_id).startswith("simkl:"):
            from resources.lib.simkl_list_sync import (
                fetch_simkl_plantowatch,
                fetch_simkl_custom_list_items,
            )
            from .config_handler import get_config_value
            simkl_user = (get_config_value("simkl.user", config_db_path) if config_db_path else None) or list_user or "simkl_user"

            if list_id == "simkl:watchlist":
                simkl_data = fetch_simkl_plantowatch(config_db_path)
                if simkl_data:
                    list_meta = {
                        "ids": {"slug": "simkl-watchlist"},
                        "name": "Simkl Watchlist",
                        "description": "Plan to Watch on Simkl",
                        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "item_count": simkl_data["counts"],
                        "user": {"ids": {"slug": simkl_user}},
                        "source": "simkl",
                        "list_id": list_id
                    }
                    await run_list_sync(
                        lists_db_path, simkl_user, simkl_data["items"], "simkl-watchlist",
                        movies_static_db_path, movies_dynamic_db_path,
                        tvshows_static_db_path, tvshows_dynamic_db_path,
                        trakt_update_queue_path, trakt_handler, tmdb_handler, list_meta
                    )
            elif str(list_id).startswith("simkl:custom:"):
                simkl_lid = str(list_id).replace("simkl:custom:", "")
                list_data = fetch_simkl_custom_list_items(config_db_path, simkl_lid, limit=500, tmdb_handler=tmdb_handler)
                if list_data and "items" in list_data:
                    list_meta = {
                        "ids": {"slug": slug, "simkl": simkl_lid},
                        "name": list_name,
                        "description": list_desc,
                        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "item_count": list_data["counts"],
                        "user": {"ids": {"slug": simkl_user}},
                        "source": "simkl",
                        "owned_by_user": True,
                        "list_id": list_id
                    }
                    await run_list_sync(
                        lists_db_path, simkl_user, list_data["items"], slug,
                        movies_static_db_path, movies_dynamic_db_path,
                        tvshows_static_db_path, tvshows_dynamic_db_path,
                        trakt_update_queue_path, trakt_handler, tmdb_handler, list_meta
                    )

        # 5. MDBList Lists
        elif source == "mdblist" or str(list_id).startswith("mdblist:"):
            from resources.lib.mdblist_list_sync import (
                get_mdblist_api_key,
                fetch_mdblist_watchlist_items,
                fetch_mdblist_list_items,
                _normalise_mdblist_items,
                _sync_items_to_list_items,
            )
            api_key = get_mdblist_api_key(config_db_path)
            if api_key:
                if list_id == "mdblist:watchlist":
                    raw_items = await asyncio.to_thread(fetch_mdblist_watchlist_items, api_key)
                else:
                    numeric_id = str(list_id).replace("mdblist:", "")
                    raw_items = await asyncio.to_thread(fetch_mdblist_list_items, numeric_id, api_key)
                normalised = _normalise_mdblist_items(raw_items)
                await asyncio.to_thread(
                    _sync_items_to_list_items,
                    list_id, normalised, lists_db_path,
                    movies_static_db_path, movies_dynamic_db_path,
                    tvshows_static_db_path, tvshows_dynamic_db_path,
                    trakt_update_queue_path, trakt_handler, tmdb_handler
                )

        # 6. PunchPlay Lists
        elif source == "punchplay" or str(list_id).startswith("punchplay:"):
            from resources.lib.punchplay_list_sync import punchplay_list_sync_task
            await punchplay_list_sync_task(
                config_db_path, lists_db_path, trakt_handler, tmdb_handler,
                movies_static_db_path, movies_dynamic_db_path,
                tvshows_static_db_path, tvshows_dynamic_db_path,
                trakt_update_queue_path
            )

        log(f"[Orac] **SINGLE LIST SYNC** Completed targeted sync for '{list_id}' in {time() - start_t:.2f}s", level=LOGINFO)
        return True
    except Exception as e:
        log(f"[Orac] **SINGLE LIST SYNC** Error syncing list '{list_id}': {e}", level=LOGERROR)
        return False
