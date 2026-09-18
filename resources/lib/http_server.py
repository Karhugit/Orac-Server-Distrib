import asyncio
from typing import Optional
import time
import requests
from resources.lib.db_utils import db_connect
import json
import sqlite3
import threading
import os
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlparse, unquote
from concurrent.futures import ThreadPoolExecutor

import xbmc
from fastapi import FastAPI, Request, Response, HTTPException, Path, Query
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from resources.lib.log_utils import log, LOGERROR, LOGDEBUG, LOGINFO, LOGWARNING
from resources.lib.sync_trakt_with_db import sync_trakt_list_metadata
from resources.lib.lists_handler import get_my_lists, get_generic_lists, get_add_options, get_remove_options, get_all_lists, update_list_library_status, delete_list_locally
from resources.lib.list_handler import handle_list_request, add_to_list, remove_from_list
from resources.lib.reload_handler import clear_databases
from resources.lib.queue_worker import UpdateQueueWorker
from resources.lib.trakt_maintenance_worker import TraktMaintenanceWorker
from resources.lib.stale_episode_refresh import StaleEpisodeRefreshWorker
from resources.lib.db_sync_manager import sync_lists_and_items
from resources.lib.episodes_handler import get_next_episodes
from resources.lib.watched import update_next_episode, mark_movie_watched, mark_tvshow_watched, drop_tvshow
from resources.lib.migrate_database import migrate_database
from resources.lib.indexing import get_genres
from resources.scrapers.scraper_manager import ScraperManager
from resources.lib.scraper_db import ScraperDB

from .movies_handler import handle_movie_request
from .discover_handler import handle_discover_request
from .shows_handler import handle_show_request
from .search_handler import search_tmdb
from .config_handler import update_config_values, get_trakt_user, get_config_value, clear_trakt_config, get_all_config
from .indexing import add_external_index, del_external_index
from .internal_indexing import add_internal_index, del_internal_index, get_internal_indexes, get_internal_index_contents, get_available_languages
from .scrape_handler import handle_scrape_request
from resources.lib.debrid_resolver import resolve_stream
from .tags_handler import get_all_tags, get_tags_for_item, add_tag_to_item, remove_tag_from_item, get_items_with_tag
from .collections_handler import handle_collections_request
from .providers_handler import init_watch_providers_db, sync_watch_providers, get_watch_providers
from .version import __version__
from .update_checker import check_for_update, get_update_state

def _vacuum_database(db_path, db_manager=None):
    if not db_path:
        return
    try:
        log(f"[Orac] Attempting to VACUUM database: {db_path}", level=LOGINFO)
        if db_manager:
            with db_manager.connection(db_path) as conn:
                conn.execute("VACUUM")
        else:
            with db_connect(db_path) as conn:
                conn.execute("VACUUM")
        log(f"[Orac] Successfully VACUUMed database: {db_path}", level=LOGINFO)
    except sqlite3.Error as e:
        log(f"[Orac] Error vacuuming database {db_path}: {e}", level=LOGERROR)

def _schedule_vacuum(db_path, db_manager=None, interval=86400):
    def vacuum_timer():
        _vacuum_database(db_path, db_manager)
        _schedule_vacuum(db_path, db_manager, interval)
    if db_path:
        timer = threading.Timer(interval, vacuum_timer)
        timer.daemon = True
        timer.start()

async def get_t_user(app: FastAPI):
    # 1. Trakt Username
    if app.state.trakt_handler and app.state.trakt_handler.username:
         return app.state.trakt_handler.username
    user = get_trakt_user(config_db_path=app.state.config_db_path)
    if user and user not in ("empty_setting", ""):
        if app.state.trakt_handler:
            app.state.trakt_handler.username = user
        return user
    if app.state.trakt_handler and hasattr(app.state.trakt_handler, 'fetch_username'):
        from .config_handler import get_trakt_access_token
        token = get_trakt_access_token(app.state.config_db_path)
        if token and token not in ("empty_setting", ""):
            try:
                user = await app.state.trakt_handler.fetch_username()
                if user and user not in ("empty_setting", ""):
                    return user
            except Exception:
                pass

    # 2. Simkl Username
    simkl_user = get_config_value("simkl.user", app.state.config_db_path) or get_config_value("simkl_user", app.state.config_db_path)
    if simkl_user and simkl_user not in ("empty_setting", ""):
        return simkl_user

    # 3. MDBList Username
    mdblist_user = get_config_value("mdblist.user", app.state.config_db_path) or get_config_value("mdblist_user", app.state.config_db_path)
    if mdblist_user and mdblist_user not in ("empty_setting", ""):
        return mdblist_user

    # 4. TMDB Username
    tmdb_user = get_config_value("tmdb_user", app.state.config_db_path)
    if tmdb_user and tmdb_user not in ("empty_setting", ""):
        return tmdb_user

    # 5. Stored user_id or existing user from watched_episodes database
    stored_user = get_config_value("user_id", app.state.config_db_path) or get_config_value("last_user", app.state.config_db_path)
    if stored_user and stored_user not in ("empty_setting", ""):
        return stored_user

    try:
        if app.state.tvshows_dynamic_db_path:
            with db_connect(app.state.tvshows_dynamic_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT user FROM watched_episodes WHERE user IS NOT NULL AND user != '' LIMIT 1")
                row = cursor.fetchone()
                if row and row[0]:
                    return row[0]
    except Exception:
        pass

    # 6. Default local user profile fallback
    return "local_user"


def parse_qs_fastapi(request: Request):
    """Converts FastAPI query params to a dictionary of lists like parse_qs"""
    query = {}
    for k, v in request.query_params.multi_items():
        if k not in query:
            query[k] = []
        query[k].append(v)
    return query

def flat_qs(request: Request):
    """Converts query to simple dict"""
    return dict(request.query_params)

def app_factory(
    trakt_handler=None, tmdb_handler=None, port=5555, movies_static_db_path=None, movies_dynamic_db_path=None, lists_db_path=None, tvshows_static_db_path=None,
    tvshows_dynamic_db_path=None, trakt_update_queue_path=None, config_db_path=None, ext_indexes_db_path=None, tags_db_path=None, scrapers_dir=None,
    config_db_conn=None, db_manager=None, trakt_history_sync_db_path=None
):

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        _vacuum_database(application.state.movies_static_db_path, application.state.db_manager)
        _vacuum_database(application.state.tvshows_static_db_path, application.state.db_manager)
        _vacuum_database(application.state.lists_db_path, application.state.db_manager)
        _schedule_vacuum(application.state.tvshows_static_db_path, application.state.db_manager)

        # Run database migrations
        migrate_database(application.state.tvshows_static_db_path, application.state.tvshows_dynamic_db_path)
        migrate_database(application.state.movies_static_db_path, application.state.movies_dynamic_db_path)

        trakt_queue_worker = UpdateQueueWorker(
            application.state.trakt_update_queue_path, application.state.tvshows_static_db_path,
            application.state.trakt_handler, application.state.tmdb_handler, application.state.db_manager,
            application.state.config_db_path, application.state.movies_dynamic_db_path, application.state.tvshows_dynamic_db_path
        )
        application.state.trakt_queue_worker = trakt_queue_worker
        trakt_queue_worker.start()
        log(f"[Orac] Update queue worker started...", level=LOGINFO)

        # Trakt history maintenance worker (90k ceiling + history sync)
        if application.state.trakt_history_sync_db_path:
            trakt_maintenance_worker = TraktMaintenanceWorker(
                trakt_auth=application.state.trakt_handler,
                movies_dynamic_db_path=application.state.movies_dynamic_db_path,
                tvshows_dynamic_db_path=application.state.tvshows_dynamic_db_path,
                update_queue_path=application.state.trakt_update_queue_path,
                history_sync_db_path=application.state.trakt_history_sync_db_path,
                db_manager=application.state.db_manager,
                sync_interval=300,
                maintenance_interval=3600,
                history_check_every=10,
            )
            application.state.trakt_maintenance_worker = trakt_maintenance_worker
            trakt_maintenance_worker.start()
            log("[Orac] Trakt maintenance worker started.", level=LOGINFO)
        else:
            application.state.trakt_maintenance_worker = None
            log("[Orac] Trakt maintenance worker skipped (no history_sync_db_path).", level=LOGWARNING)

        # Stale episode metadata refresh worker
        stale_refresh_worker = StaleEpisodeRefreshWorker(
            tmdb_handler=application.state.tmdb_handler,
            tvshows_static_db_path=application.state.tvshows_static_db_path,
            trakt_handler=application.state.trakt_handler,
            refresh_interval=86400,   # 24-hour cycle
            batch_size=100,
            startup_delay=120,        # wait 2 min after startup before first pass
        )
        application.state.stale_refresh_worker = stale_refresh_worker
        stale_refresh_worker.start()
        log("[Orac] Stale episode refresh worker started.", level=LOGINFO)

        # Watch-provider catalogue — init schema then do first sync (global, no region filter)
        init_watch_providers_db(application.state.config_db_path)
        import threading as _threading
        _prov_thread = _threading.Thread(
            target=sync_watch_providers,
            args=(application.state.tmdb_handler, application.state.config_db_path),
            daemon=True,
            name="ProviderSync",
        )
        _prov_thread.start()
        log("[Orac] Watch-provider sync started (global).", level=LOGINFO)

        # Startup update check — runs in background so it never delays startup
        import threading as _threading
        log(f"[Orac] Orac Server v{__version__} starting up.", level=LOGINFO)
        _threading.Thread(target=check_for_update, daemon=True, name="UpdateCheck").start()
        
        # Automatic poster cleanup for any legacy fanart.tv URLs
        from resources.lib.fanart_client import cleanup_broken_fanart_posters
        _threading.Thread(
            target=cleanup_broken_fanart_posters, 
            args=(application.state.config_db_path, application.state.tmdb_handler), 
            daemon=True, 
            name="PosterCleanup"
        ).start()

        # Background sync loop — runs every hour; also re-syncs providers and
        # checks for updates daily
        _provider_sync_counter = 0
        _update_check_counter = 0

        async def hourly_sync_loop():
            nonlocal _provider_sync_counter, _update_check_counter
            while True:
                try:
                    current_username = get_trakt_user(config_db_path=application.state.config_db_path)
                    await sync_lists_and_items(
                        application.state.trakt_handler,
                        application.state.tmdb_handler,
                        application.state.movies_static_db_path,
                        application.state.movies_dynamic_db_path,
                        application.state.tvshows_static_db_path,
                        application.state.tvshows_dynamic_db_path,
                        application.state.lists_db_path,
                        application.state.trakt_update_queue_path,
                        application.state.trakt_queue_worker,
                        current_username,
                        external_indexes_db_path=application.state.ext_indexes_db_path,
                        config_db_path=application.state.config_db_path,
                        tags_db_path=application.state.tags_db_path
                    )
                except Exception as e:
                    log(f"[Orac] Error in hourly sync loop: {e}", level=LOGERROR)

                # Re-sync providers every 24 loops (~24 hours)
                _provider_sync_counter += 1
                if _provider_sync_counter >= 24:
                    _provider_sync_counter = 0
                    try:
                        sync_watch_providers(
                            application.state.tmdb_handler,
                            application.state.config_db_path,
                        )
                    except Exception as e:
                        log(f"[Orac] Provider re-sync error: {e}", level=LOGERROR)

                # Re-check for updates every 24 loops (~24 hours)
                _update_check_counter += 1
                if _update_check_counter >= 24:
                    _update_check_counter = 0
                    _threading.Thread(target=check_for_update, daemon=True, name="UpdateCheck").start()

                await asyncio.sleep(3600)

        
        async def fanart_sync_loop():
            # Wait 10 seconds before the first sync run
            await asyncio.sleep(10)
            while True:
                try:
                    from resources.lib.fanart_client import run_fanart_latest_sync
                    # Run in an executor/thread pool because requests is synchronous and blocking
                    await asyncio.get_event_loop().run_in_executor(
                        None, run_fanart_latest_sync, application.state.config_db_path, application.state.tmdb_handler
                    )
                except Exception as e:
                    log(f"[Orac] Error in fanart sync loop: {e}", level=LOGERROR)
                # Sleep for 30 minutes (1800 seconds)
                await asyncio.sleep(1800)

        sync_task = asyncio.create_task(hourly_sync_loop())
        fanart_sync_task = asyncio.create_task(fanart_sync_loop())
        yield
        sync_task.cancel()
        fanart_sync_task.cancel()
        if application.state.trakt_maintenance_worker:
            application.state.trakt_maintenance_worker.stop()
        if application.state.stale_refresh_worker:
            application.state.stale_refresh_worker.stop()
        log("[Orac] Setup teardown complete", level=LOGINFO)

    app = FastAPI(lifespan=lifespan)
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.trakt_handler = trakt_handler
    app.state.tmdb_handler = tmdb_handler
    app.state.movies_static_db_path = movies_static_db_path
    app.state.movies_dynamic_db_path = movies_dynamic_db_path
    app.state.lists_db_path = lists_db_path
    app.state.tvshows_static_db_path = tvshows_static_db_path
    app.state.tvshows_dynamic_db_path = tvshows_dynamic_db_path
    app.state.trakt_update_queue_path = trakt_update_queue_path
    app.state.config_db_path = config_db_path
    app.state.config_db_conn = config_db_conn
    app.state.ext_indexes_db_path = ext_indexes_db_path
    app.state.tags_db_path = tags_db_path
    app.state.db_manager = db_manager
    app.state.trakt_history_sync_db_path = trakt_history_sync_db_path
    if scrapers_dir:
        app.state.scraper_manager = ScraperManager(scrapers_dir)
    else:
        app.state.scraper_manager = ScraperManager()

    def apply_fanart_overrides_to_payload(data):
        try:
            from resources.lib.config_handler import get_fanart_config
            config = get_fanart_config(app.state.config_db_path)
            if not config["fanart_enabled"]:
                return data
                
            from resources.lib.formatting_utils import format_image_url
            
            is_list = isinstance(data, list)
            items = data if is_list else [data]
            
            movie_ids = []
            show_ids = []
            resolved_items = []
            
            for item in items:
                if not isinstance(item, dict):
                    resolved_items.append(None)
                    continue
                
                media_type = item.get("media_type")
                show_tmdb_id = item.get("show_tmdb_id") or item.get("show_id")
                tmdb_id = item.get("tmdb_id")
                
                is_movie = False
                if media_type == "movie":
                    is_movie = True
                elif media_type in ("tvshow", "tv", "show", "episode"):
                    is_movie = False
                else:
                    if "show_tmdb_id" in item or "show_id" in item or "seasons" in item:
                        is_movie = False
                    elif "title" in item and "seasons" not in item:
                        is_movie = True
                
                if is_movie:
                    lookup_id = tmdb_id
                    if lookup_id:
                        movie_ids.append(lookup_id)
                        resolved_items.append(("movie", lookup_id))
                    else:
                        resolved_items.append(None)
                else:
                    lookup_id = show_tmdb_id or tmdb_id
                    if lookup_id:
                        show_ids.append(lookup_id)
                        resolved_items.append(("show", lookup_id))
                    else:
                        resolved_items.append(None)
            
            movie_fanart_map = {}
            if movie_ids:
                with db_connect(app.state.movies_static_db_path) as conn:
                    cursor = conn.cursor()
                    placeholders = ",".join("?" for _ in movie_ids)
                    cursor.execute(f"SELECT tmdb_id, poster_path, fanart_path, clearlogo_path FROM movies WHERE tmdb_id IN ({placeholders})", movie_ids)
                    for row in cursor.fetchall():
                        movie_fanart_map[row[0]] = {
                            "poster": format_image_url(row[1], "w780", app.state.tmdb_handler),
                            "fanart": format_image_url(row[2], "w1280", app.state.tmdb_handler),
                            "clearlogo": format_image_url(row[3], "w500", app.state.tmdb_handler)
                        }
                        
            show_fanart_map = {}
            if show_ids:
                with db_connect(app.state.tvshows_static_db_path) as conn:
                    cursor = conn.cursor()
                    placeholders = ",".join("?" for _ in show_ids)
                    cursor.execute(f"SELECT show_tmdb_id, poster_path, fanart_path, clearlogo_path FROM shows WHERE show_tmdb_id IN ({placeholders})", show_ids)
                    for row in cursor.fetchall():
                        show_fanart_map[row[0]] = {
                            "poster": format_image_url(row[1], "w780", app.state.tmdb_handler),
                            "fanart": format_image_url(row[2], "w1280", app.state.tmdb_handler),
                            "clearlogo": format_image_url(row[3], "w500", app.state.tmdb_handler)
                        }
                        
            for idx, item in enumerate(items):
                if not isinstance(item, dict) or not resolved_items[idx]:
                    continue
                    
                media_type_resolved, lookup_id = resolved_items[idx]
                if media_type_resolved == "movie":
                    f_data = movie_fanart_map.get(lookup_id)
                    if f_data:
                        if f_data["poster"]:
                            item["poster_path"] = f_data["poster"]
                            item["thumbnail_path"] = f_data["poster"]
                        if f_data["fanart"]:
                            item["fanart_path"] = f_data["fanart"]
                            item["landscape_path"] = f_data["fanart"]
                        if f_data["clearlogo"]:
                            item["clearlogo_path"] = f_data["clearlogo"]
                else:
                    f_data = show_fanart_map.get(lookup_id)
                    if f_data:
                        # Override standard keys
                        if f_data["poster"]:
                            item["poster_path"] = f_data["poster"]
                            item["thumbnail_path"] = f_data["poster"]
                        if f_data["fanart"]:
                            item["fanart_path"] = f_data["fanart"]
                            item["landscape_path"] = f_data["fanart"]
                        if f_data["clearlogo"]:
                            item["clearlogo_path"] = f_data["clearlogo"]
                            
                        # Override show-specific/episode-specific keys
                        if f_data["poster"]:
                            if "show_poster_path" in item:
                                item["show_poster_path"] = f_data["poster"]
                            if "show_thumbnail_path" in item:
                                item["show_thumbnail_path"] = f_data["poster"]
                            if "episode_poster_path" in item:
                                item["episode_poster_path"] = f_data["poster"]
                        if f_data["fanart"]:
                            if "show_fanart_path" in item:
                                item["show_fanart_path"] = f_data["fanart"]
                            if "show_landscape_path" in item:
                                item["show_landscape_path"] = f_data["fanart"]
                            if "episode_fanart_path" in item:
                                item["episode_fanart_path"] = f_data["fanart"]
                            if "episode_landscape_path" in item:
                                item["episode_landscape_path"] = f_data["fanart"]
                        if f_data["clearlogo"]:
                            if "show_clearlogo_path" in item:
                                item["show_clearlogo_path"] = f_data["clearlogo"]
                            if "episode_clearlogo_path" in item:
                                item["episode_clearlogo_path"] = f_data["clearlogo"]
        except Exception as e:
            log(f"[Orac] Error applying fanart overrides in send_safe: {e}", level=LOGERROR)
        return data

    # Create responses safely
    def send_safe(status, body, content_type="application/json"):
        if content_type == "application/json":
            if isinstance(body, (dict, list)):
                body = apply_fanart_overrides_to_payload(body)
            elif isinstance(body, str):
                try:
                    data = json.loads(body)
                    data = apply_fanart_overrides_to_payload(data)
                    body = json.dumps(data)
                except Exception:
                    pass

        if isinstance(body, dict) or isinstance(body, list):
            return JSONResponse(status_code=status, content=body)
        if isinstance(body, bytes):
            return Response(content=body, status_code=status, media_type=content_type)
        if isinstance(body, str):
            return Response(content=body.encode("utf-8"), status_code=status, media_type=content_type)
        return Response(content=str(body), status_code=status, media_type=content_type)

    @app.get("/")
    async def root_redirect():
        return RedirectResponse(url="/web/")

    @app.get("/ping")
    async def ping():
        return PlainTextResponse("Yes, what do you want?")

    @app.get("/api/status")
    async def api_status():
        try:
            with db_connect(app.state.config_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT key, value FROM config 
                    WHERE key IN (
                        'trakt_user', 'trakt_token', 'trakt_refresh', 'trakt_expires',
                        'simkl.user', 'simkl_user', 'simkl.token',
                        'tmdb_user', 'tmdb.user', 'tmdb_session_id',
                        'mdblist_api',
                        'rd.token', 'rd.enabled', 'rd.refresh', 'rd.account_id',
                        'pm.token', 'pm.enabled', 'pm.account_id',
                        'oc.token', 'oc.enabled',
                        'ed.token', 'ed.enabled',
                        'tb.token', 'tb.enabled',
                        'easynews_user', 'easynews_password', 'provider.easynews'
                    )
                """)
                rows = cursor.fetchall()
                data = {row[0]: row[1] for row in rows}
                
                # Default missing keys to 'empty_setting'
                trakt_user = data.get('trakt_user') or 'empty_setting'
                trakt_token = data.get('trakt_token') or 'empty_setting'
                trakt_refresh = data.get('trakt_refresh') or 'empty_setting'
                trakt_expires = data.get('trakt_expires') or 'empty_setting'
                
                simkl_user = data.get('simkl.user') or data.get('simkl_user') or 'empty_setting'
                simkl_token = data.get('simkl.token') or 'empty_setting'
                
                tmdb_user = data.get('tmdb_user') or data.get('tmdb.user') or 'empty_setting'
                tmdb_session_id = data.get('tmdb_session_id') or 'empty_setting'
                
                mdblist_api = data.get('mdblist_api') or 'empty_setting'

                # Build debrid data with correct default values
                debrid_keys = {
                    'rd.token': 'empty_setting',
                    'rd.enabled': 'false',
                    'rd.refresh': 'empty_setting',
                    'rd.account_id': 'empty_setting',
                    'pm.token': 'empty_setting',
                    'pm.enabled': 'false',
                    'pm.account_id': 'empty_setting',
                    'oc.token': 'empty_setting',
                    'oc.enabled': 'false',
                    'ed.token': 'empty_setting',
                    'ed.enabled': 'false',
                    'tb.token': 'empty_setting',
                    'tb.enabled': 'false',
                    'easynews_user': 'empty_setting',
                    'easynews_password': 'empty_setting',
                    'provider.easynews': 'false'
                }
                debrid_data = {k: data.get(k) if data.get(k) is not None else default for k, default in debrid_keys.items()}
                
                return JSONResponse(status_code=200, content={
                    "status": "online",
                    "trakt": {
                        "user": trakt_user,
                        "token": trakt_token,
                        "refresh": trakt_refresh,
                        "expires": trakt_expires
                    },
                    "simkl": {
                        "user": simkl_user,
                        "token": simkl_token
                    },
                    "tmdb": {
                        "user": tmdb_user,
                        "session_id": tmdb_session_id
                    },
                    "mdblist": {
                        "api": mdblist_api
                    },
                    "debrid": debrid_data
                })
        except Exception as e:
            log(f"Error in api_status: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})

    @app.get("/movie")
    async def movie(request: Request):
        query = parse_qs_fastapi(request)
        movie_tmdb_id = query.get("tmdb_id", [None])[0]
        if not movie_tmdb_id:
            return PlainTextResponse("Missing movie id", status_code=400)
        status, body, content_type = handle_movie_request(movie_tmdb_id, app.state.movies_dynamic_db_path, app.state.movies_static_db_path, app.state.tmdb_handler)
        return send_safe(status, body, content_type)

    @app.get("/show")
    async def show(request: Request):
        query = parse_qs_fastapi(request)
        show_tmdb_id = query.get("tmdb_id", [None])[0]
        user = query.get("user", [None])[0] or await get_t_user(app) or ""
        if not show_tmdb_id:
            return PlainTextResponse("Missing show tmdb_id", status_code=400)
        status, body, content_type = handle_show_request(show_tmdb_id, user, app.state.tvshows_static_db_path, app.state.tvshows_dynamic_db_path, app.state.tmdb_handler)
        return send_safe(status, body, content_type)

    @app.get("/list")
    async def get_list(request: Request):
        query = parse_qs_fastapi(request)
        list_name = query.get("name", [None])[0]
        item_type = query.get("item_type", [None])[0]
        user = query.get("user", [None])[0] or await get_t_user(app)
        status, body, content_type = await handle_list_request(
            list_name, item_type, user, 
            app.state.movies_dynamic_db_path, app.state.movies_static_db_path, 
            app.state.tvshows_dynamic_db_path, app.state.tvshows_static_db_path, 
            app.state.lists_db_path,
            trakt_handler=app.state.trakt_handler,
            tmdb_handler=app.state.tmdb_handler,
            ext_indexes_db_path=app.state.ext_indexes_db_path
        )
        return send_safe(status, body, content_type)

    @app.get("/lists")
    async def get_lists(request: Request):
        query = parse_qs_fastapi(request)
        list_name = query.get("name", ['my_lists'])[0]
        item_type = query.get("item_type", ['All'])[0]
        tmdb_id = query.get("tmdb_id", [None])[0]
        exclude_empty = query.get("exclude_empty", ['false'])[0] == 'true'
        
        result = []
        if list_name == 'my_lists':
            if item_type.lower() == 'all':
                result = get_all_lists(app.state.lists_db_path, app.state.ext_indexes_db_path, exclude_empty=exclude_empty)
            else:
                result = get_my_lists(app.state.lists_db_path, list_name, item_type, app.state.ext_indexes_db_path, exclude_empty=exclude_empty)
        elif list_name == 'add_list_options':
            trakt_user = await get_t_user(app)
            result = get_add_options(app.state.lists_db_path, item_type, tmdb_id, app.state.movies_static_db_path, app.state.tvshows_static_db_path, trakt_user, app.state.tmdb_handler)
        elif list_name == 'remove_list_options':
            trakt_user = await get_t_user(app)
            result = get_remove_options(app.state.lists_db_path, item_type, tmdb_id, app.state.movies_static_db_path, app.state.tvshows_static_db_path, trakt_user, app.state.tmdb_handler)
        elif list_name == 'generic_lists':
            result = get_generic_lists(app.state.lists_db_path, list_name, item_type, app.state.ext_indexes_db_path)
        
        if result is not None:
            return JSONResponse(status_code=200, content=result)
        else:
            return PlainTextResponse("Error getting lists", status_code=500)

    @app.get("/next_episodes")
    async def next_episodes(request: Request):
        query = parse_qs_fastapi(request)
        user = query.get("user", [None])[0]
        result = get_next_episodes(app.state.tvshows_dynamic_db_path, app.state.tvshows_static_db_path, user=user)
        if result is not None:
            return send_safe(200, result)
        return PlainTextResponse("Error getting next episodes", status_code=500)

    @app.get("/search_tmdb")
    async def search_handler(request: Request):
        query = parse_qs_fastapi(request)
        query_str = query.get("name", [None])[0]
        item_type = query.get("item_type", ['multi'])[0]
        if not query_str:
            return PlainTextResponse("Missing search query", status_code=400)
        try:
            results = search_tmdb(query_str, app.state.tmdb_handler, item_type=item_type)
            return JSONResponse(status_code=200, content=results)
        except Exception as e:
            return PlainTextResponse("Error searching TMDb", status_code=500)

    @app.get("/scrape")
    async def scrape(request: Request):
        query = parse_qs_fastapi(request)
        results_limit = 0
        if 'results_limit' in query:
             try:
                 results_limit = int(query['results_limit'][0])
             except ValueError:
                 results_limit = 0
        status, body, content_type = await handle_scrape_request(
            query, 
            app.state.scraper_manager, 
            app.state.movies_static_db_path, 
            app.state.tvshows_static_db_path,
            tmdb_handler=app.state.tmdb_handler,
            results_limit=results_limit,
            global_loop=asyncio.get_running_loop(),
            config_db_path=app.state.config_db_path
        )
        return send_safe(status, body, content_type)

    @app.get("/fast_start_episode")
    async def fast_start_episode(request: Request):
        query = parse_qs_fastapi(request)
        query['results_limit'] = ['4']
        status, body, content_type = await handle_scrape_request(
            query, app.state.scraper_manager, app.state.movies_static_db_path, app.state.tvshows_static_db_path,
            tmdb_handler=app.state.tmdb_handler, results_limit=4, global_loop=asyncio.get_running_loop(),
            config_db_path=app.state.config_db_path
        )
        return send_safe(status, body, content_type)

    @app.get("/resolve")
    async def resolve_endpoint(request: Request):
        query = parse_qs_fastapi(request)
        provider = query.get("provider", [None])[0] or query.get("debrid", [None])[0]
        magnet = query.get("magnet", [None])[0] or query.get("url", [None])[0]
        info_hash = query.get("hash", [None])[0]
        title = query.get("title", [""])[0]
        season = query.get("season", [None])[0]
        episode = query.get("episode", [None])[0]
        
        if not provider or not magnet:
            return JSONResponse(status_code=400, content={"success": False, "error": "provider and magnet are required"})
            
        cfg = get_all_config(app.state.config_db_path)
        stream_url = await asyncio.to_thread(resolve_stream, provider, magnet, info_hash, title, season, episode, cfg)
        if stream_url:
            return JSONResponse(status_code=200, content={"success": True, "stream_url": stream_url})
        else:
            return JSONResponse(status_code=404, content={"success": False, "error": "Could not resolve stream"})


    @app.get("/get_genres")
    async def get_genres_handler(request: Request):
        query = parse_qs_fastapi(request)
        item_type = query.get("item_type", [None])[0]
        if not item_type:
             return PlainTextResponse("Missing item_type", status_code=400)
        result = get_genres(app.state.movies_static_db_path, app.state.movies_dynamic_db_path, app.state.tvshows_static_db_path, app.state.tvshows_static_db_path, item_type)
        if result is not None:
            return JSONResponse(status_code=200, content=result)
        return PlainTextResponse("Error getting genres", status_code=500)

    @app.get("/get_external_indexes")
    async def get_external_indexes(request: Request):
        query = parse_qs_fastapi(request)
        media_type = query.get("item_type", [None])[0]
        try:
            with app.state.db_manager.connection(app.state.ext_indexes_db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                if media_type:
                    cursor.execute("SELECT * FROM external_indexes WHERE media_type = ? ORDER BY id", (media_type,))
                else:
                    cursor.execute("SELECT * FROM external_indexes ORDER BY id")
                rows = cursor.fetchall()
                indexes = []
                for row in rows:
                    index_item = dict(row)
                    if 'parameters' in index_item and isinstance(index_item['parameters'], str):
                        try:
                            index_item['parameters'] = json.loads(index_item['parameters'])
                        except json.JSONDecodeError:
                            index_item['parameters'] = {}
                    indexes.append(index_item)
                return JSONResponse(status_code=200, content={"success": True, "indexes": indexes})
        except Exception as e:
            return JSONResponse(status_code=500, content={"success": False, "error": "Error fetching external indexes"})

    @app.get("/get_internal_indexes")
    async def get_int_indexes(request: Request):
        query = parse_qs_fastapi(request)
        media_type = query.get("item_type", [None])[0]
        try:
            indexes = get_internal_indexes(app.state.ext_indexes_db_path, media_type)
            return JSONResponse(status_code=200, content={"success": True, "indexes": indexes})
        except Exception as e:
            return JSONResponse(status_code=500, content={"success": False, "error": "Error fetching internal indexes"})

    @app.get("/internal_index_contents")
    async def int_idx_contents(request: Request):
        query = parse_qs_fastapi(request)
        index_id = query.get("index_id", [None])[0]
        media_type = query.get("item_type", [None])[0]
        if not index_id or not media_type:
            return JSONResponse(status_code=400, content={"success": False, "error": "Missing index_id or item_type"})
        try:
            if media_type == 'movie':
                static_db = app.state.movies_static_db_path
                dynamic_db = app.state.movies_dynamic_db_path
            elif media_type in ('tvshow', 'episode'):
                static_db = app.state.tvshows_static_db_path
                dynamic_db = app.state.tvshows_dynamic_db_path
            user = query.get("user", [None])[0] or await get_t_user(app) or ""
            results = get_internal_index_contents(
                app.state.ext_indexes_db_path, index_id, media_type, static_db, dynamic_db, user=user, tags_db_path=app.state.tags_db_path
            )
            return JSONResponse(status_code=200, content={"success": True, "results": results})
        except Exception as e:
            return JSONResponse(status_code=500, content={"success": False, "error": "Error fetching internal index contents"})

    @app.get("/get_available_languages")
    async def available_languages(request: Request):
        try:
            languages = get_available_languages(app.state.movies_static_db_path)
            return JSONResponse(status_code=200, content={"success": True, "languages": languages})
        except Exception as e:
            return JSONResponse(status_code=500, content={"success": False, "error": "Error fetching languages"})

    @app.get("/tmdb_keywords")
    async def tmdb_keywords(request: Request):
        query = parse_qs_fastapi(request)
        keyword = query.get("keyword", [None])[0]
        item_type = query.get("item_type", [None])[0]
        if not keyword or not item_type:
             return PlainTextResponse("Missing keyword or item_type", status_code=400)
        keywords = app.state.tmdb_handler.get_keywords(keyword, item_type)
        return JSONResponse(status_code=200, content={"success": True, "keywords": keywords})

    @app.get("/providers")
    async def providers_route(request: Request):
        """Returns the TMDB watch-provider catalogue stored in the config DB.

        Query params:
            media_type  — 'movie', 'tv', or omit for all providers
        """
        query = parse_qs_fastapi(request)
        media_type = query.get("media_type", [None])[0]
        providers = get_watch_providers(app.state.config_db_path, media_type)
        return JSONResponse(status_code=200, content={"success": True, "providers": providers})

    @app.get("/force_sync")
    async def force_sync(request: Request):
        asyncio.create_task(sync_lists_and_items(
            app.state.trakt_handler, app.state.tmdb_handler, app.state.movies_static_db_path, app.state.movies_dynamic_db_path,
            app.state.tvshows_static_db_path, app.state.tvshows_dynamic_db_path, app.state.lists_db_path, app.state.trakt_update_queue_path,
            app.state.trakt_queue_worker, username=await get_t_user(app), external_indexes_db_path=app.state.ext_indexes_db_path,
            config_db_path=app.state.config_db_path, tags_db_path=app.state.tags_db_path
        ))
        return JSONResponse(status_code=200, content={"status": "started", "message": "Force sync started"})

    @app.get("/sync_watched")
    async def sync_watched_route(request: Request):
        from resources.lib.sync_engine import sync_providers, bulk_sync_history
        async def _run_watched_sync():
            try:
                await sync_providers(
                    app.state.movies_dynamic_db_path,
                    app.state.tvshows_dynamic_db_path,
                    app.state.trakt_handler,
                    app.state.config_db_path,
                    tvshows_static_db=app.state.tvshows_static_db_path,
                    force=True
                )
                bulk_sync_history(
                    app.state.movies_dynamic_db_path,
                    app.state.tvshows_dynamic_db_path,
                    app.state.trakt_handler,
                    app.state.config_db_path,
                    tvshows_static_db=app.state.tvshows_static_db_path
                )
            except Exception as e:
                log(f"[Orac] Error running manual watched sync: {e}", level=LOGERROR)
        asyncio.create_task(_run_watched_sync())
        return JSONResponse(status_code=200, content={"status": "started", "message": "Watched synchronization started"})

    @app.get("/tags")
    async def get_tags_h(request: Request):
        query = parse_qs_fastapi(request)
        details = query.get("details", ['false'])[0] == 'true'
        from .tags_handler import get_all_tags, get_all_tags_with_counts
        if details:
            tags = get_all_tags_with_counts(app.state.tags_db_path)
        else:
            tags = get_all_tags(app.state.tags_db_path)
        return JSONResponse(status_code=200, content={"success": True, "tags": tags})

    @app.get("/tags/{media_type}/{tmdb_id}")
    async def get_tags_for_item_route(media_type: str, tmdb_id: int):
        from .tags_handler import get_tags_for_item
        tags = get_tags_for_item(app.state.tags_db_path, media_type, tmdb_id)
        return JSONResponse(status_code=200, content={"success": True, "tags": tags})

    @app.get("/tags/{tag_name}/items")
    async def get_tag_items_route(tag_name: str):
        from .tags_handler import get_items_with_tag
        items = get_items_with_tag(app.state.tags_db_path, tag_name)
        enriched_items = []
        for item in items:
            m_type = item['media_type']
            m_id = item['tmdb_id']
            try:
                if m_type == 'movie':
                    status, body, _ = handle_movie_request(m_id, app.state.movies_dynamic_db_path, app.state.movies_static_db_path, app.state.tmdb_handler)
                    if status == 200:
                        data = json.loads(body)
                        data['media_type'] = m_type
                        enriched_items.append(data)
                elif m_type in ('show', 'tvshow'):
                    status, body, _ = handle_show_request(m_id, "", app.state.tvshows_static_db_path, app.state.tvshows_dynamic_db_path, app.state.tmdb_handler)
                    if status == 200:
                        data = json.loads(body)
                        data['media_type'] = m_type
                        enriched_items.append(data)
            except Exception:
                continue
        return JSONResponse(status_code=200, content={"success": True, "items": enriched_items})

    @app.get("/recommendations/movies")
    async def rec_movies(request: Request):
        query = parse_qs_fastapi(request)
        user = query.get("user", [None])[0] or await get_t_user(app) or ""
        from resources.lib.recommendations_handler import get_recommendations_async
        result = await get_recommendations_async(user, app.state.movies_dynamic_db_path, app.state.movies_static_db_path, app.state.tmdb_handler)
        return JSONResponse(status_code=200, content=result)

    @app.get("/collections/movies")
    async def collections_movies(request: Request):
        query = parse_qs_fastapi(request)
        user = query.get("user", [None])[0] or await get_t_user(app) or ""
        status, body, content_type = handle_collections_request(
            app.state.movies_static_db_path, 
            app.state.movies_dynamic_db_path, 
            tmdb_handler=app.state.tmdb_handler,
            user=user
        )
        return send_safe(status, body, content_type)

    @app.get("/reviews")
    async def reviews_route(request: Request):
        query = parse_qs_fastapi(request)
        tmdb_id = query.get('tmdb_id', [None])[0]
        if not tmdb_id:
            return JSONResponse(status_code=400, content={'success': False, 'error': 'Missing tmdb_id'})
        media_type = query.get('media_type', ['movie'])[0]
        try:
            max_reviews = int(query.get('max_reviews', ['20'])[0])
        except Exception:
            max_reviews = 20

        reviews = []
        if app.state.tmdb_handler:
            reviews = app.state.tmdb_handler.get_reviews(tmdb_id, media_type=media_type, max_reviews=max_reviews)
        else:
            try:
                from resources.lib.tmdb_handler import TMDbAPI
                from resources.lib.config_handler import get_config_value
                api_key = get_config_value('tmdb_api_key', app.state.config_db_path)
                if api_key:
                    handler = TMDbAPI(api_key=api_key, static_db_path=app.state.movies_static_db_path)
                    reviews = handler.get_reviews(tmdb_id, media_type=media_type, max_reviews=max_reviews)
            except Exception as e:
                log(f"[Orac] Error initializing TMDbAPI in reviews_route: {e}", level=LOGWARNING)
        if media_type == 'movie':
            try:
                with db_connect(app.state.movies_static_db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT title, year FROM movies WHERE tmdb_id = ?", (tmdb_id,))
                    row = cursor.fetchone()
                    if row:
                        title, year = row
                        from .metacritic_scraper import MetacriticScraper
                        mc = MetacriticScraper()
                        mc_reviews = mc.get_reviews(title, year)
                        if mc_reviews:
                            reviews = mc_reviews + reviews
            except Exception:
                pass
        return JSONResponse(status_code=200, content={'success': True, 'reviews': reviews})

    @app.get("/discover/{item_type}")
    async def discover_route(item_type: str, request: Request):
        query = parse_qs_fastapi(request)
        conn = None
        try:
            conn = app.state.db_manager.get_connection(app.state.ext_indexes_db_path)
            cursor = conn.cursor()
            status, body, content_type = handle_discover_request(item_type, query, app.state.tmdb_handler, cursor)
            return send_safe(status, body, content_type)
        except Exception:
            return PlainTextResponse("Database error", status_code=500)
        finally:
            if conn:
                conn.close()

    # --- WEB DASHBOARD ROUTES ---
    @app.get("/api/web/scrapers")
    async def web_scrapers_api():
        try:
            # Assuming scrapers.db is in the CWD (where run_server.py is running from)
            scraper_db = ScraperDB('scrapers.db')
            metrics = scraper_db.get_all_scrapers()
            return JSONResponse(status_code=200, content={"success": True, "scrapers": metrics})
        except Exception as e:
            log(f"Error fetching scraper metrics for dashboard: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.get("/api/web/platforms")
    async def web_platforms_api():
        try:
            with db_connect(app.state.config_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM config")
                rows = cursor.fetchall()
                
                # Normalize keys slightly in case of duplicates or variants
                data = {row[0]: row[1] for row in rows}
                
                platforms = []
                
                trakt_user = data.get('trakt_user') or data.get('trakt.user')
                trakt_token = data.get('trakt_token') or data.get('trakt.token')
                is_trakt_auth = bool(trakt_user and trakt_token and trakt_user not in ('empty_setting', '') and trakt_token not in ('empty_setting', ''))
                # Trakt is static - always present in the platforms list
                platforms.append({
                    "name": "Trakt",
                    "id": "trakt",
                    "username": (trakt_user if is_trakt_auth else ""),
                    "authenticated": is_trakt_auth,
                    "can_auth": True
                })
                
                simkl_user = data.get('simkl.user') or data.get('simkl_user')
                simkl_token = data.get('simkl.token') or data.get('simkl_token')
                is_simkl_auth = bool(simkl_user and simkl_token and simkl_user not in ('empty_setting', '') and simkl_token not in ('empty_setting', ''))
                # Simkl is static - always present in the platforms list
                platforms.append({
                    "name": "Simkl",
                    "id": "simkl",
                    "username": (simkl_user if is_simkl_auth else ""),
                    "authenticated": is_simkl_auth,
                    "can_auth": True
                })
                
                tmdb_user = data.get('tmdb_user') or data.get('tmdb.user')
                is_tmdb_auth = bool(tmdb_user and tmdb_user not in ('empty_setting', ''))
                # TMDb is static - always present in the platforms list
                platforms.append({
                    "name": "TMDb",
                    "id": "tmdb",
                    "username": (tmdb_user if is_tmdb_auth else ""),
                    "authenticated": is_tmdb_auth,
                    "can_auth": True
                })
                
                mdblist_api = data.get('mdblist_api')
                mdblist_user = data.get('mdblist.user') or data.get('mdblist_user')
                is_mdblist_auth = bool(mdblist_api and mdblist_api not in ('empty_setting', ''))
                
                # MDBList is static - always present in the platforms list
                platforms.append({
                    "name": "MDBList",
                    "id": "mdblist",
                    "username": (mdblist_user if (mdblist_user and mdblist_user not in ('empty_setting', '')) else ("Authorised" if is_mdblist_auth else "")),
                    "authenticated": is_mdblist_auth,
                    "can_auth": True
                })
                
                fanart_api = data.get('fanart_api_key')
                fanart_storage_mode = data.get('fanart_storage_mode') or 'URL'
                is_fanart_auth = bool(fanart_api and fanart_api not in ('empty_setting', ''))
                
                # Fanart is static - always present in the platforms list
                platforms.append({
                    "name": "Fanart",
                    "id": "fanart",
                    "username": ("Authorised" if is_fanart_auth else ""),
                    "authenticated": is_fanart_auth,
                    "storage_mode": fanart_storage_mode,
                    "can_auth": True
                })

                aio_user = data.get('aio.username')
                aio_pass = data.get('aio.password')
                aio_instance = data.get('aiostreams_instance') or "0"
                aio_custom_url = data.get('aio.custom_url')
                is_aio_auth = bool(
                    aio_user and aio_user not in ('empty_setting', '') and
                    aio_pass and aio_pass not in ('empty_setting', '') and
                    (aio_instance != "1" or (aio_custom_url and aio_custom_url not in ('empty_setting', '')))
                )
                
                # AIOStreams is static - always present in the platforms list
                platforms.append({
                    "name": "AIOStreams",
                    "id": "aiostreams",
                    "username": (aio_user if is_aio_auth else ""),
                    "password": (aio_pass if (is_aio_auth and aio_pass != 'empty_setting') else ""),
                    "authenticated": is_aio_auth,
                    "instance": aio_instance,
                    "custom_url": aio_custom_url if (aio_custom_url and aio_custom_url != 'empty_setting') else '',
                    "can_auth": True
                })
                
                trakt_to_mdblist_sync_enabled = (data.get('trakt_to_mdblist_sync') == 'true')
                    
                return JSONResponse(status_code=200, content={
                    "success": True, 
                    "platforms": platforms,
                    "fanart_storage_mode": fanart_storage_mode,
                    "trakt_to_mdblist_sync": trakt_to_mdblist_sync_enabled
                })
        except Exception as e:
            log(f"Error fetching platform tokens from config DB: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/trakt_to_mdblist_sync")
    async def update_trakt_to_mdblist_sync_api(request: Request):
        try:
            body = await request.json()
            enabled = bool(body.get("enabled", False))
            val_str = "true" if enabled else "false"
            success = update_config_values({"trakt_to_mdblist_sync": val_str}, app.state.config_db_path)
            log(f"[Orac] Updated Trakt -> MDBList sync toggle to: {val_str}", level=LOGINFO)
            return JSONResponse(status_code=200 if success else 500, content={"success": success, "enabled": enabled})
        except Exception as e:
            log(f"Error updating Trakt -> MDBList sync setting: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/trakt/start_auth")
    async def web_trakt_start_auth_api():
        try:
            client_id = get_config_value("client_id", app.state.config_db_path) or get_config_value("trakt_client", app.state.config_db_path) or get_config_value("trakt.client", app.state.config_db_path)
            if not client_id or client_id in ('empty_setting', ''):
                client_id = "f986871799a140dc20a166adfa637c98c8fa474dc80757aabad8668b99e184de"

            url = "https://api.trakt.tv/oauth/device/code"
            headers = {
                "Content-Type": "application/json",
                "trakt-api-version": "2",
                "trakt-api-key": client_id
            }
            resp = requests.post(url, json={"client_id": client_id}, headers=headers, timeout=10)
            if resp.status_code != 200:
                return JSONResponse(status_code=400, content={"success": False, "error": f"Trakt error ({resp.status_code}): Failed to get device code"})

            data = resp.json()
            device_code = data.get("device_code")
            user_code = data.get("user_code")
            verification_url = data.get("verification_url") or "https://trakt.tv/activate"
            expires_in = data.get("expires_in", 600)
            interval = data.get("interval", 5)

            return JSONResponse(status_code=200, content={
                "success": True,
                "device_code": device_code,
                "user_code": user_code,
                "verification_url": verification_url,
                "expires_in": expires_in,
                "interval": interval
            })
        except Exception as e:
            log(f"Error starting Trakt authentication: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/trakt/check_auth")
    async def web_trakt_check_auth_api(request: Request):
        try:
            body = await request.json()
            device_code = (body.get("device_code") or "").strip()
            if not device_code:
                return JSONResponse(status_code=400, content={"success": False, "error": "Missing device code"})

            client_id = get_config_value("client_id", app.state.config_db_path) or get_config_value("trakt_client", app.state.config_db_path) or get_config_value("trakt.client", app.state.config_db_path)
            if not client_id or client_id in ('empty_setting', ''):
                client_id = "f986871799a140dc20a166adfa637c98c8fa474dc80757aabad8668b99e184de"

            client_secret = get_config_value("client_secret", app.state.config_db_path) or get_config_value("trakt_secret", app.state.config_db_path) or get_config_value("trakt.secret", app.state.config_db_path)
            if not client_secret or client_secret in ('empty_setting', ''):
                client_secret = "579a93101ccf4cb251884dc1d632d74c3b3f34d275cee59d56e82cac31161f71"

            url = "https://api.trakt.tv/oauth/device/token"
            headers = {
                "Content-Type": "application/json",
                "trakt-api-version": "2",
                "trakt-api-key": client_id
            }
            payload = {
                "code": device_code,
                "client_id": client_id,
                "client_secret": client_secret
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code == 400:
                return JSONResponse(status_code=200, content={"success": False, "status": "pending", "error": "Approval pending"})
            elif resp.status_code != 200:
                return JSONResponse(status_code=200, content={"success": False, "status": "pending", "error": f"Trakt response {resp.status_code}"})

            data = resp.json()
            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 7776000)

            if not access_token:
                return JSONResponse(status_code=200, content={"success": False, "status": "pending", "error": "No access token returned"})

            # Fetch username from /users/me
            user_headers = {
                "Content-Type": "application/json",
                "trakt-api-version": "2",
                "trakt-api-key": client_id,
                "Authorization": f"Bearer {access_token}"
            }
            user_resp = requests.get("https://api.trakt.tv/users/me", headers=user_headers, timeout=10)
            username = "trakt_user"
            if user_resp.status_code == 200:
                user_data = user_resp.json()
                username = user_data.get("username") or user_data.get("ids", {}).get("slug") or "trakt_user"

            expires_at = str(time.time() + expires_in)
            success = update_config_values({
                "trakt_user": username,
                "trakt.user": username,
                "trakt_token": access_token,
                "trakt.token": access_token,
                "trakt_refresh": refresh_token,
                "trakt.refresh": refresh_token,
                "trakt_expires": expires_at,
                "client_id": client_id,
                "client_secret": client_secret,
                "trakt_token_refreshed": str(int(time.time()))
            }, app.state.config_db_path)

            # Reload Trakt auth in running server state if present
            if hasattr(app.state, 'trakt_auth') and app.state.trakt_auth:
                app.state.trakt_auth.reload_credentials()

            if success:
                log(f"[Trakt] Successfully authorised as user: {username}", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True, "username": username})
            else:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to save Trakt tokens in config database"})
        except Exception as e:
            log(f"Error checking Trakt authentication: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/trakt/revoke")
    async def web_trakt_revoke_api():
        try:
            token = get_config_value("trakt_token", app.state.config_db_path) or get_config_value("trakt.token", app.state.config_db_path)
            client_id = get_config_value("client_id", app.state.config_db_path) or "f986871799a140dc20a166adfa637c98c8fa474dc80757aabad8668b99e184de"
            client_secret = get_config_value("client_secret", app.state.config_db_path) or "579a93101ccf4cb251884dc1d632d74c3b3f34d275cee59d56e82cac31161f71"
            if token and token not in ('empty_setting', ''):
                try:
                    headers = {"Content-Type": "application/json", "trakt-api-version": "2", "trakt-api-key": client_id}
                    requests.post("https://api.trakt.tv/oauth/revoke", json={"token": token, "client_id": client_id, "client_secret": client_secret}, headers=headers, timeout=5)
                except Exception:
                    pass

            clear_trakt_config(app.state.config_db_path)
            update_config_values({
                "trakt_user": "empty_setting",
                "trakt.user": "empty_setting",
                "trakt_token": "empty_setting",
                "trakt.token": "empty_setting",
                "trakt_refresh": "empty_setting",
                "trakt.refresh": "empty_setting",
                "trakt_expires": "empty_setting"
            }, app.state.config_db_path)

            if hasattr(app.state, 'trakt_auth') and app.state.trakt_auth:
                app.state.trakt_auth.reload_credentials()

            log(f"[Trakt] Successfully revoked Trakt authorisation", level=LOGINFO)
            return JSONResponse(status_code=200, content={"success": True})
        except Exception as e:
            log(f"Error revoking Trakt authorisation: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/simkl/start_auth")
    async def web_simkl_start_auth_api():
        try:
            client_id = get_config_value("simkl.client", app.state.config_db_path) or get_config_value("simkl_client", app.state.config_db_path)
            if not client_id or client_id in ('empty_setting', ''):
                client_id = "8cdf2298c78dd4ff8cb8039faecd1b9f11cf108fac2b88092abd15c22cfe2cc2"

            url = "https://api.simkl.com/oauth/pin"
            resp = requests.get(url, params={"client_id": client_id}, timeout=10)
            if resp.status_code != 200:
                return JSONResponse(status_code=400, content={"success": False, "error": f"Simkl error ({resp.status_code}): Failed to get device PIN"})

            data = resp.json()
            user_code = data.get("user_code")
            verification_url = data.get("verification_url") or f"https://simkl.com/pin/{user_code}"
            expires_in = data.get("expires_in", 900)
            interval = data.get("interval", 5)

            return JSONResponse(status_code=200, content={
                "success": True,
                "user_code": user_code,
                "verification_url": verification_url,
                "expires_in": expires_in,
                "interval": interval
            })
        except Exception as e:
            log(f"Error starting Simkl authentication: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/simkl/check_auth")
    async def web_simkl_check_auth_api(request: Request):
        try:
            body = await request.json()
            user_code = (body.get("user_code") or "").strip()
            if not user_code:
                return JSONResponse(status_code=400, content={"success": False, "error": "Missing user code"})

            client_id = get_config_value("simkl.client", app.state.config_db_path) or get_config_value("simkl_client", app.state.config_db_path)
            if not client_id or client_id in ('empty_setting', ''):
                client_id = "8cdf2298c78dd4ff8cb8039faecd1b9f11cf108fac2b88092abd15c22cfe2cc2"

            url = f"https://api.simkl.com/oauth/pin/{user_code}"
            resp = requests.get(url, params={"client_id": client_id}, timeout=10)
            if resp.status_code != 200:
                return JSONResponse(status_code=200, content={"success": False, "status": "pending", "error": "Not yet approved on Simkl"})

            data = resp.json()
            if data.get("result") != "OK" or "access_token" not in data:
                return JSONResponse(status_code=200, content={"success": False, "status": "pending", "error": "Approval pending"})

            access_token = data["access_token"]

            # Fetch account settings to get username
            settings_url = "https://api.simkl.com/users/settings"
            headers = {
                "Content-Type": "application/json",
                "simkl-api-key": client_id,
                "Authorization": f"Bearer {access_token}"
            }
            user_resp = requests.get(settings_url, headers=headers, timeout=10)
            username = "simkl_user"
            if user_resp.status_code == 200:
                user_data = user_resp.json()
                if "user" in user_data and "name" in user_data["user"]:
                    username = str(user_data["user"]["name"])

            # Save in config.db
            success = update_config_values({
                "simkl.user": username,
                "simkl_user": username,
                "simkl.token": access_token,
                "simkl_token": access_token,
                "simkl.client": client_id
            }, app.state.config_db_path)

            if success:
                log(f"[Simkl] Successfully authorised as user: {username}", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True, "username": username})
            else:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to save Simkl tokens in config database"})
        except Exception as e:
            log(f"Error checking Simkl authentication: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/simkl/revoke")
    async def web_simkl_revoke_api():
        try:
            success = update_config_values({
                "simkl.user": "empty_setting",
                "simkl_user": "empty_setting",
                "simkl.token": "empty_setting",
                "simkl_token": "empty_setting"
            }, app.state.config_db_path)

            if success:
                log(f"[Simkl] Successfully revoked Simkl authorisation", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True})
            else:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to clear Simkl configuration values"})
        except Exception as e:
            log(f"Error revoking Simkl authorisation: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/mdblist/auth")
    async def web_mdblist_auth_api(request: Request):
        try:
            body = await request.json()
            api_key = (body.get("api_key") or "").strip()
            if not api_key or api_key == "empty_setting":
                return JSONResponse(status_code=400, content={"success": False, "error": "API key cannot be empty"})

            # Validate key against MDBList API
            url = f"https://api.mdblist.com/user?apikey={api_key}"
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                return JSONResponse(status_code=400, content={"success": False, "error": f"MDBList API error ({resp.status_code}): Invalid API key"})
            
            data = resp.json()
            username = data.get("username") or data.get("name") or "mdblist_user"

            # Save in config DB
            success = update_config_values({
                "mdblist_api": api_key,
                "mdblist.user": username,
                "mdblist_user": username
            }, app.state.config_db_path)

            if success:
                log(f"[MDBList] Successfully authorised as user: {username}", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True, "username": username})
            else:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to update configuration database"})
        except Exception as e:
            log(f"Error authenticating MDBList API: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/mdblist/revoke")
    async def web_mdblist_revoke_api():
        try:
            success = update_config_values({
                "mdblist_api": "empty_setting",
                "mdblist.user": "empty_setting",
                "mdblist_user": "empty_setting"
            }, app.state.config_db_path)

            if success:
                log(f"[MDBList] Successfully revoked MDBList authorisation", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True})
            else:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to clear configuration values"})
        except Exception as e:
            log(f"Error revoking MDBList authorisation: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/fanart/auth")
    async def web_fanart_auth_api(request: Request):
        try:
            body = await request.json()
            api_key = (body.get("api_key") or "").strip()
            storage_mode = body.get("storage_mode") or "URL"
            if storage_mode not in ("URL", "Local"):
                storage_mode = "URL"
            if not api_key or api_key == "empty_setting":
                return JSONResponse(status_code=400, content={"success": False, "error": "API key cannot be empty"})

            # Validate key against Fanart.tv API
            url = f"https://webservice.fanart.tv/v3/movies/11?api_key={api_key}"
            try:
                resp = requests.get(url, timeout=8)
                if resp.status_code == 401:
                    return JSONResponse(status_code=400, content={"success": False, "error": "Fanart.tv API error (401): Invalid API key"})
            except requests.RequestException as e:
                log(f"[Fanart] Warning: Fanart.tv validation network issue: {e}", level=LOGWARNING)

            # Save in config DB
            success = update_config_values({
                "fanart_api_key": api_key,
                "fanart_enabled": "true",
                "fanart_storage_mode": storage_mode
            }, app.state.config_db_path)

            if success:
                log(f"[Fanart] Successfully authorised Fanart.tv (Storage mode: {storage_mode})", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True, "storage_mode": storage_mode})
            else:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to update configuration database"})
        except Exception as e:
            log(f"Error authenticating Fanart.tv API: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/fanart/revoke")
    async def web_fanart_revoke_api():
        try:
            success = update_config_values({
                "fanart_api_key": "empty_setting",
                "fanart_enabled": "false"
            }, app.state.config_db_path)

            if success:
                log(f"[Fanart] Successfully revoked Fanart.tv authorisation", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True})
            else:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to clear configuration values"})
        except Exception as e:
            log(f"Error revoking Fanart.tv authorisation: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/fanart/storage_mode")
    async def web_fanart_storage_mode_api(request: Request):
        try:
            body = await request.json()
            storage_mode = body.get("storage_mode")
            if storage_mode not in ("URL", "Local"):
                return JSONResponse(status_code=400, content={"success": False, "error": "Invalid storage mode (must be 'URL' or 'Local')"})
            success = update_config_values({
                "fanart_storage_mode": storage_mode
            }, app.state.config_db_path)
            if success:
                log(f"[Fanart] Updated Fanart storage mode to: {storage_mode}", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True, "storage_mode": storage_mode})
            else:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to update configuration database"})
        except Exception as e:
            log(f"Error updating Fanart storage mode: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/aiostreams/auth")
    async def web_aiostreams_auth_api(request: Request):
        try:
            body = await request.json()
            instance = str(body.get("instance", "0")).strip()
            custom_url = (body.get("custom_url") or "").strip()
            username = (body.get("username") or "").strip()
            password = (body.get("password") or "").strip()

            if not username or username == "empty_setting":
                return JSONResponse(status_code=400, content={"success": False, "error": "Username cannot be empty"})
            if not password or password == "empty_setting":
                return JSONResponse(status_code=400, content={"success": False, "error": "Password cannot be empty"})
            if instance == "1" and (not custom_url or custom_url == "empty_setting"):
                return JSONResponse(status_code=400, content={"success": False, "error": "Custom URL cannot be empty when Custom URL is selected"})

            public_instances = {
                "0": "https://aiostreams.stremio.ru",
                "2": "https://aiostreams.viren070.me",
                "3": "https://aiostreams.fortheweak.cloud",
                "4": "https://aiostreamsfortheweebsstable.midnightignite.me"
            }
            base_link = custom_url.rstrip('/') if instance == "1" else public_instances.get(instance, "https://aiostreams.stremio.ru")

            # Validate credentials against instance
            try:
                resp = requests.get(f"{base_link}/api/v1/search", auth=(username, password), timeout=6)
                if resp.status_code == 401:
                    return JSONResponse(status_code=400, content={"success": False, "error": "Invalid credentials (401 Unauthorized)"})
            except requests.RequestException as net_err:
                log(f"[AIOStreams Auth] Warning: could not reach {base_link}: {net_err}", level=LOGWARNING)

            # Update config DB
            success = update_config_values({
                "aio.username": username,
                "aio.password": password,
                "aiostreams_instance": instance,
                "aio.custom_url": custom_url if instance == "1" else "empty_setting"
            }, app.state.config_db_path)

            if success:
                try:
                    scraper_db = ScraperDB('scrapers.db')
                    scraper_db.set_active_status('aiostreams', True)
                except Exception as s_err:
                    log(f"Error activating aiostreams in scraper db: {s_err}", level=LOGERROR)

                log(f"[AIOStreams] Successfully authorised as user: {username}", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True, "username": username})
            else:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to update configuration database"})
        except Exception as e:
            log(f"Error authenticating AIOStreams: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/aiostreams/revoke")
    async def web_aiostreams_revoke_api():
        try:
            success = update_config_values({
                "aio.username": "empty_setting",
                "aio.password": "empty_setting",
                "aiostreams_instance": "0",
                "aio.custom_url": "empty_setting"
            }, app.state.config_db_path)

            if success:
                try:
                    scraper_db = ScraperDB('scrapers.db')
                    scraper_db.set_active_status('aiostreams', False)
                except Exception as s_err:
                    log(f"Error setting aiostreams inactive in scraper db: {s_err}", level=LOGERROR)

                log(f"[AIOStreams] Successfully revoked AIOStreams authorisation", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True})
            else:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to clear AIOStreams configuration values"})
        except Exception as e:
            log(f"Error revoking AIOStreams authorisation: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    # =========================================================================
    # PREMIUMIZE DEBRID WEB & CLOUD ENDPOINTS
    # =========================================================================
    @app.post("/api/web/debrid/premiumize/start_auth")
    async def web_premiumize_start_auth_api():
        try:
            client_id = "230973825"
            url = "https://www.premiumize.me/token"
            data = {"response_type": "device_code", "client_id": client_id}
            resp = requests.post(url, data=data, timeout=10)
            if resp.status_code == 200:
                res_data = resp.json()
                return JSONResponse(status_code=200, content={
                    "success": True,
                    "device_code": res_data.get("device_code"),
                    "user_code": res_data.get("user_code"),
                    "verification_url": res_data.get("verification_uri", "https://www.premiumize.me/device"),
                    "expires_in": res_data.get("expires_in", 900),
                    "interval": res_data.get("interval", 5)
                })
            else:
                return JSONResponse(status_code=400, content={"success": False, "error": f"Premiumize error: {resp.text}"})
        except Exception as e:
            log(f"Error starting Premiumize auth: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/debrid/premiumize/poll_auth")
    async def web_premiumize_poll_auth_api(request: Request):
        try:
            body = await request.json()
            device_code = body.get("device_code")
            if not device_code:
                return JSONResponse(status_code=400, content={"success": False, "error": "Missing device_code"})

            client_id = "230973825"
            client_secret = "qeac5k2pj3jbxmmuds"
            poll_url = "https://www.premiumize.me/token"
            data = {
                "grant_type": "device_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": device_code
            }
            resp = requests.post(poll_url, data=data, timeout=10)
            res_data = resp.json()

            if "error" in res_data:
                err_val = res_data.get("error")
                return JSONResponse(status_code=200, content={
                    "success": False,
                    "error": err_val,
                    "pending": err_val in ("authorization_pending", "slow_down")
                })

            access_token = res_data.get("access_token")
            if access_token:
                customer_id = "empty_setting"
                try:
                    info_res = requests.get(
                        "https://www.premiumize.me/api/account/info",
                        headers={"Authorization": f"Bearer {access_token}"},
                        timeout=8
                    )
                    if info_res.status_code == 200:
                        info_data = info_res.json()
                        if info_data.get("status") == "success":
                            customer_id = str(info_data.get("customer_id") or "empty_setting")
                except Exception as acc_err:
                    log(f"Error fetching Premiumize account info after auth: {acc_err}", level=LOGWARNING)

                update_config_values({
                    "pm.token": str(access_token),
                    "pm.enabled": "true",
                    "pm.account_id": str(customer_id)
                }, app.state.config_db_path)

                log(f"[Premiumize] Successfully authorised account: {customer_id}", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True, "customer_id": customer_id})

            return JSONResponse(status_code=400, content={"success": False, "error": "No access token returned"})
        except Exception as e:
            log(f"Error polling Premiumize auth: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/debrid/premiumize/auth_token")
    async def web_premiumize_auth_token_api(request: Request):
        try:
            body = await request.json()
            token = (body.get("token") or "").strip()
            if not token or token == "empty_setting":
                return JSONResponse(status_code=400, content={"success": False, "error": "API Key / PIN cannot be empty"})

            info_res = requests.get(
                "https://www.premiumize.me/api/account/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=8
            )
            if info_res.status_code != 200:
                return JSONResponse(status_code=400, content={"success": False, "error": f"Failed to validate token (HTTP {info_res.status_code})"})

            info_data = info_res.json()
            if info_data.get("status") != "success":
                return JSONResponse(status_code=400, content={"success": False, "error": info_data.get("message", "Invalid Premiumize token")})

            customer_id = str(info_data.get("customer_id") or "empty_setting")
            update_config_values({
                "pm.token": token,
                "pm.enabled": "true",
                "pm.account_id": customer_id
            }, app.state.config_db_path)

            log(f"[Premiumize] Successfully authorised via token for account: {customer_id}", level=LOGINFO)
            return JSONResponse(status_code=200, content={"success": True, "customer_id": customer_id})
        except Exception as e:
            log(f"Error authorising Premiumize via token: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/debrid/premiumize/revoke")
    async def web_premiumize_revoke_api():
        try:
            update_config_values({
                "pm.token": "empty_setting",
                "pm.enabled": "false",
                "pm.account_id": "empty_setting"
            }, app.state.config_db_path)
            log("[Premiumize] Successfully revoked Premiumize authorisation", level=LOGINFO)
            return JSONResponse(status_code=200, content={"success": True})
        except Exception as e:
            log(f"Error revoking Premiumize: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    # Premiumize Cloud Storage Operations (Live Fetch, No Caching)
    def _get_pm_token():
        tok = get_config_value("pm.token", app.state.config_db_path)
        if not tok or tok == "empty_setting":
            return None
        return tok

    @app.get("/api/debrid/premiumize/cloud")
    async def pm_cloud_api(folder_id: Optional[str] = None):
        tok = _get_pm_token()
        if not tok:
            return JSONResponse(status_code=401, content={"status": "error", "message": "Premiumize not authorised"})
        url = "https://www.premiumize.me/api/folder/list"
        params = {"id": folder_id} if folder_id else {}
        headers = {"Authorization": f"Bearer {tok}", "User-Agent": "Liberator-Orac"}
        try:
            resp = await asyncio.to_thread(requests.get, url, params=params, headers=headers, timeout=20)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception as e:
            log(f"[Premiumize Cloud] Error fetching folder/list: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    @app.get("/api/debrid/premiumize/transfers")
    async def pm_transfers_api():
        tok = _get_pm_token()
        if not tok:
            return JSONResponse(status_code=401, content={"status": "error", "message": "Premiumize not authorised"})
        url = "https://www.premiumize.me/api/transfer/list"
        headers = {"Authorization": f"Bearer {tok}", "User-Agent": "Liberator-Orac"}
        try:
            resp = await asyncio.to_thread(requests.get, url, headers=headers, timeout=20)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception as e:
            log(f"[Premiumize Cloud] Error fetching transfer/list: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    @app.get("/api/debrid/premiumize/account_info")
    async def pm_account_info_api():
        tok = _get_pm_token()
        if not tok:
            return JSONResponse(status_code=401, content={"status": "error", "message": "Premiumize not authorised"})
        url = "https://www.premiumize.me/api/account/info"
        headers = {"Authorization": f"Bearer {tok}", "User-Agent": "Liberator-Orac"}
        try:
            resp = await asyncio.to_thread(requests.get, url, headers=headers, timeout=20)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception as e:
            log(f"[Premiumize Cloud] Error fetching account/info: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    @app.post("/api/debrid/premiumize/rename")
    async def pm_rename_api(request: Request):
        tok = _get_pm_token()
        if not tok:
            return JSONResponse(status_code=401, content={"status": "error", "message": "Premiumize not authorised"})
        try:
            body = await request.json()
            file_type = body.get("file_type", "item")
            endpoint = "folder/rename" if file_type == "folder" else "item/rename"
            url = f"https://www.premiumize.me/api/{endpoint}"
            data = {"id": body.get("id"), "name": body.get("name")}
            headers = {"Authorization": f"Bearer {tok}", "User-Agent": "Liberator-Orac"}
            resp = await asyncio.to_thread(requests.post, url, data=data, headers=headers, timeout=20)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception as e:
            log(f"[Premiumize Cloud] Error renaming {file_type}: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    @app.post("/api/debrid/premiumize/delete")
    async def pm_delete_api(request: Request):
        tok = _get_pm_token()
        if not tok:
            return JSONResponse(status_code=401, content={"status": "error", "message": "Premiumize not authorised"})
        try:
            body = await request.json()
            file_type = body.get("file_type", "item")
            endpoint = "folder/delete" if file_type == "folder" else "item/delete"
            url = f"https://www.premiumize.me/api/{endpoint}"
            data = {"id": body.get("id")}
            headers = {"Authorization": f"Bearer {tok}", "User-Agent": "Liberator-Orac"}
            resp = await asyncio.to_thread(requests.post, url, data=data, headers=headers, timeout=20)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception as e:
            log(f"[Premiumize Cloud] Error deleting {file_type}: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    @app.get("/api/debrid/premiumize/item_details")
    async def pm_item_details_api(id: str):
        tok = _get_pm_token()
        if not tok:
            return JSONResponse(status_code=401, content={"status": "error", "message": "Premiumize not authorised"})
        url = "https://www.premiumize.me/api/item/details"
        headers = {"Authorization": f"Bearer {tok}", "User-Agent": "Liberator-Orac"}
        try:
            resp = await asyncio.to_thread(requests.post, url, data={"id": id}, headers=headers, timeout=20)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception as e:
            log(f"[Premiumize Cloud] Error fetching item details: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    @app.get("/api/debrid/premiumize/cloud_all")
    async def pm_cloud_all_api():
        tok = _get_pm_token()
        if not tok:
            return JSONResponse(status_code=401, content={"status": "error", "message": "Premiumize not authorised"})
        url = "https://www.premiumize.me/api/item/listall"
        headers = {"Authorization": f"Bearer {tok}", "User-Agent": "Liberator-Orac"}
        try:
            resp = await asyncio.to_thread(requests.get, url, headers=headers, timeout=20)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception as e:
            log(f"[Premiumize Cloud] Error fetching item/listall: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    @app.post("/api/web/platforms/tmdb/start_auth")
    async def web_tmdb_start_auth_api():
        try:
            tmdb_h = getattr(app.state, 'tmdb_handler', None)
            api_key = getattr(tmdb_h, 'api_key', None) if tmdb_h else None
            if not api_key or api_key in ('empty_setting', ''):
                stored_key = get_config_value("tmdb_api_key", app.state.config_db_path) or get_config_value("tmdb_api", app.state.config_db_path)
                api_key = stored_key if stored_key and stored_key not in ('empty_setting', '') else "872d408b3d926ebb32f84f5167764cc3"

            url = f"https://api.themoviedb.org/3/authentication/token/new?api_key={api_key}"
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                return JSONResponse(status_code=400, content={"success": False, "error": f"TMDb error ({resp.status_code}): Failed to get request token"})
            
            data = resp.json()
            request_token = data.get("request_token")
            if not request_token:
                return JSONResponse(status_code=500, content={"success": False, "error": "Invalid response from TMDb"})
            
            auth_url = f"https://www.themoviedb.org/authenticate/{request_token}"
            return JSONResponse(status_code=200, content={
                "success": True,
                "request_token": request_token,
                "auth_url": auth_url
            })
        except Exception as e:
            log(f"Error starting TMDb authentication: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/tmdb/check_auth")
    async def web_tmdb_check_auth_api(request: Request):
        try:
            body = await request.json()
            request_token = (body.get("request_token") or "").strip()
            if not request_token:
                return JSONResponse(status_code=400, content={"success": False, "error": "Missing request token"})

            tmdb_h = getattr(app.state, 'tmdb_handler', None)
            api_key = getattr(tmdb_h, 'api_key', None) if tmdb_h else None
            if not api_key or api_key in ('empty_setting', ''):
                stored_key = get_config_value("tmdb_api_key", app.state.config_db_path) or get_config_value("tmdb_api", app.state.config_db_path)
                api_key = stored_key if stored_key and stored_key not in ('empty_setting', '') else "872d408b3d926ebb32f84f5167764cc3"

            url = f"https://api.themoviedb.org/3/authentication/session/new?api_key={api_key}"
            resp = requests.post(url, json={"request_token": request_token}, timeout=10)
            
            if resp.status_code != 200:
                return JSONResponse(status_code=200, content={"success": False, "status": "pending", "error": "Not yet approved on TMDb"})

            session_data = resp.json()
            if not session_data.get("success") or not session_data.get("session_id"):
                return JSONResponse(status_code=200, content={"success": False, "status": "pending", "error": "Approval pending"})

            session_id = session_data["session_id"]

            # Fetch username
            account_url = f"https://api.themoviedb.org/3/account?api_key={api_key}&session_id={session_id}"
            acc_resp = requests.get(account_url, timeout=10)
            if acc_resp.status_code != 200:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to fetch account info from TMDb"})

            account_data = acc_resp.json()
            username = account_data.get("username") or "tmdb_user"

            # Save in config.db
            success = update_config_values({
                "tmdb_user": username,
                "tmdb.user": username,
                "tmdb_session_id": session_id,
                "tmdb.session_id": session_id,
                "tmdb_api_key": api_key
            }, app.state.config_db_path)

            if success:
                log(f"[TMDb] Successfully authorised as user: {username}", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True, "username": username})
            else:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to save TMDb tokens in config database"})
        except Exception as e:
            log(f"Error checking TMDb authentication: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/platforms/tmdb/revoke")
    async def web_tmdb_revoke_api():
        try:
            success = update_config_values({
                "tmdb_user": "empty_setting",
                "tmdb.user": "empty_setting",
                "tmdb_session_id": "empty_setting",
                "tmdb.session_id": "empty_setting"
            }, app.state.config_db_path)

            if success:
                log(f"[TMDb] Successfully revoked TMDb authorisation", level=LOGINFO)
                return JSONResponse(status_code=200, content={"success": True})
            else:
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to clear TMDb configuration values"})
        except Exception as e:
            log(f"Error revoking TMDb authorisation: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.get("/api/web/library_lists")
    async def web_library_lists_api():
        try:
            with db_connect(app.state.lists_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT list_id, name, source FROM lists WHERE add_to_library = 1 OR add_to_library = 'true' OR add_to_library = 'True'")
                rows = cursor.fetchall()
                lists = [{"list_id": row[0], "name": row[1], "source": row[2]} for row in rows]
                return JSONResponse(status_code=200, content={"success": True, "lists": lists})
        except Exception as e:
            log(f"Error fetching library lists: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.get("/api/web/list_items")
    async def web_list_items_api(list_id: str):
        try:
            items = []
            with db_connect(app.state.lists_db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT media_type, tmdb_id, trakt_id 
                    FROM list_items 
                    WHERE list_id = ?
                """, (list_id,))
                list_rows = [dict(r) for r in cursor.fetchall()]

            # Fetch details from static DBs
            movies_conn = db_connect(app.state.movies_static_db_path)
            movies_conn.row_factory = sqlite3.Row
            movies_cur = movies_conn.cursor()

            tvshows_conn = db_connect(app.state.tvshows_static_db_path)
            tvshows_conn.row_factory = sqlite3.Row
            tvshows_cur = tvshows_conn.cursor()

            try:
                for row in list_rows:
                    media_type = row["media_type"]
                    tmdb_id = row["tmdb_id"]
                    trakt_id = row["trakt_id"]

                    item_details = {
                        "media_type": media_type,
                        "tmdb_id": tmdb_id,
                        "trakt_id": trakt_id,
                        "title": "Unknown",
                        "year": 0,
                        "poster_path": None,
                        "rating": 0.0,
                        "overview": ""
                    }

                    if media_type == "movie":
                        if tmdb_id:
                            movies_cur.execute("""
                                SELECT title, year, poster_path, rating, overview 
                                FROM movies 
                                WHERE tmdb_id = ?
                            """, (tmdb_id,))
                        elif trakt_id:
                            movies_cur.execute("""
                                SELECT title, year, poster_path, rating, overview 
                                FROM movies 
                                WHERE trakt_id = ?
                            """, (trakt_id,))
                        else:
                            movies_cur.execute("SELECT 1 WHERE 0")
                        
                        m_row = movies_cur.fetchone()
                        if m_row:
                            from resources.lib.formatting_utils import format_image_url
                            item_details.update({
                                "title": m_row["title"],
                                "year": m_row["year"],
                                "poster_path": format_image_url(m_row["poster_path"], "w780", app.state.tmdb_handler),
                                "rating": m_row["rating"] or 0.0,
                                "overview": m_row["overview"] or ""
                            })
                    elif media_type == "show":
                        if tmdb_id:
                            tvshows_cur.execute("""
                                SELECT title, year, poster_path, rating, overview 
                                FROM shows 
                                WHERE show_tmdb_id = ?
                            """, (tmdb_id,))
                        elif trakt_id:
                            tvshows_cur.execute("""
                                SELECT title, year, poster_path, rating, overview 
                                FROM shows 
                                WHERE show_trakt_id = ?
                            """, (trakt_id,))
                        else:
                            tvshows_cur.execute("SELECT 1 WHERE 0")
                        
                        s_row = tvshows_cur.fetchone()
                        if s_row:
                            from resources.lib.formatting_utils import format_image_url
                            item_details.update({
                                "title": s_row["title"],
                                "year": s_row["year"],
                                "poster_path": format_image_url(s_row["poster_path"], "w780", app.state.tmdb_handler),
                                "rating": s_row["rating"] or 0.0,
                                "overview": s_row["overview"] or ""
                            })
                    items.append(item_details)
            finally:
                movies_conn.close()
                tvshows_conn.close()

            return JSONResponse(status_code=200, content={"success": True, "items": items})
        except Exception as e:
            log(f"Error fetching list items for {list_id}: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.get("/api/web/index_items")
    async def web_index_items_api(index_id: str, item_type: str, index_type: str = "external"):
        try:
            items = []
            if index_type == "external":
                with db_connect(app.state.ext_indexes_db_path) as conn:
                    cursor = conn.cursor()
                    status, body, _ = handle_discover_request(item_type, {'name': [index_id]}, app.state.tmdb_handler, cursor)
                    if status == 200:
                        raw_results = json.loads(body)
                        from resources.lib.formatting_utils import format_image_url
                        for r in raw_results:
                            items.append({
                                "media_type": item_type if item_type != 'tv' else 'tvshow',
                                "tmdb_id": r.get("tmdb_id") or r.get("id"),
                                "trakt_id": r.get("trakt_id"),
                                "title": r.get("title") or r.get("name") or "Unknown",
                                "year": r.get("year") or (int(r.get("premiered", "0").split("-")[0]) if r.get("premiered") else 0),
                                "poster_path": format_image_url(r.get("poster_path"), "w780", app.state.tmdb_handler),
                                "rating": r.get("rating") or r.get("vote_average") or 0.0,
                                "overview": r.get("overview") or ""
                            })
                    else:
                        return JSONResponse(status_code=status, content={"success": False, "error": f"Discover query failed with status {status}"})
            elif index_type == "internal":
                if item_type == 'movie':
                    static_db = app.state.movies_static_db_path
                    dynamic_db = app.state.movies_dynamic_db_path
                elif item_type in ('tvshow', 'show', 'episode'):
                    static_db = app.state.tvshows_static_db_path
                    dynamic_db = app.state.tvshows_dynamic_db_path
                else:
                    static_db = app.state.movies_static_db_path
                    dynamic_db = app.state.movies_dynamic_db_path
                user = await get_t_user(app) or ""
                results = get_internal_index_contents(
                    app.state.ext_indexes_db_path, index_id, item_type, static_db, dynamic_db, user=user, tags_db_path=app.state.tags_db_path
                )
                from resources.lib.formatting_utils import format_image_url
                for r in results:
                    items.append({
                        "media_type": r.get("media_type") or item_type,
                        "tmdb_id": r.get("tmdb_id"),
                        "trakt_id": r.get("trakt_id"),
                        "title": r.get("title") or "Unknown",
                        "year": r.get("year") or 0,
                        "poster_path": format_image_url(r.get("poster_path"), "w780", app.state.tmdb_handler),
                        "rating": r.get("rating") or 0.0,
                        "overview": r.get("overview") or ""
                    })

            return JSONResponse(status_code=200, content={"success": True, "items": items, "index_id": index_id, "index_type": index_type})
        except Exception as e:
            log(f"Error executing index items for {index_id} ({index_type}): {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/list_items/remove")
    async def web_remove_list_item_api(request: Request):
        try:
            data = await request.json()
            list_id = data.get("list_id")
            media_type = data.get("media_type")
            trakt_id = data.get("trakt_id")
            tmdb_id = data.get("tmdb_id")

            if not list_id or not media_type or (not trakt_id and not tmdb_id):
                return JSONResponse(status_code=400, content={"success": False, "error": "Missing required fields"})

            with db_connect(app.state.lists_db_path) as conn:
                cursor = conn.cursor()
                if trakt_id and tmdb_id:
                    cursor.execute("""
                        DELETE FROM list_items 
                        WHERE list_id = ? AND media_type = ? AND (trakt_id = ? OR tmdb_id = ?)
                    """, (list_id, media_type, str(trakt_id), str(tmdb_id)))
                elif trakt_id:
                    cursor.execute("""
                        DELETE FROM list_items 
                        WHERE list_id = ? AND media_type = ? AND trakt_id = ?
                    """, (list_id, media_type, str(trakt_id)))
                else:
                    cursor.execute("""
                        DELETE FROM list_items 
                        WHERE list_id = ? AND media_type = ? AND tmdb_id = ?
                    """, (list_id, media_type, str(tmdb_id)))
                conn.commit()

            return JSONResponse(status_code=200, content={"success": True})
        except Exception as e:
            log(f"Error removing list item: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.get("/api/web/logs")
    async def web_logs_api():
        try:
            log_path = os.environ.get("ORAC_LOG_PATH", "orac.log")
            if not os.path.exists(log_path):
                return JSONResponse(status_code=200, content={"success": True, "logs": []})
            
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                # Read last 3000 lines efficiently, expecting enough matches
                lines = deque(f, 3000)
                
            filtered_logs = []
            for line in lines:
                line = line.strip()
                if '[INFO]' in line or '[WARNING]' in line or '[ERROR]' in line:
                    filtered_logs.append(line)
            
            # Return only the last 200 matching lines
            return JSONResponse(status_code=200, content={"success": True, "logs": filtered_logs[-200:]})
        except Exception as e:
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.get("/api/web/genres")
    async def web_genres_api():
        """Returns standard TMDb genres for Movies and TV Shows."""
        movie_genres = [
            {"id": 28, "name": "Action"}, {"id": 12, "name": "Adventure"}, {"id": 16, "name": "Animation"},
            {"id": 35, "name": "Comedy"}, {"id": 80, "name": "Crime"}, {"id": 99, "name": "Documentary"},
            {"id": 18, "name": "Drama"}, {"id": 10751, "name": "Family"}, {"id": 14, "name": "Fantasy"},
            {"id": 36, "name": "History"}, {"id": 27, "name": "Horror"}, {"id": 10402, "name": "Music"},
            {"id": 9648, "name": "Mystery"}, {"id": 10749, "name": "Romance"}, {"id": 878, "name": "Science Fiction"},
            {"id": 10770, "name": "TV Movie"}, {"id": 53, "name": "Thriller"}, {"id": 10752, "name": "War"},
            {"id": 37, "name": "Western"}
        ]
        tv_genres = [
            {"id": 10759, "name": "Action & Adventure"}, {"id": 16, "name": "Animation"}, {"id": 35, "name": "Comedy"},
            {"id": 80, "name": "Crime"}, {"id": 99, "name": "Documentary"}, {"id": 18, "name": "Drama"},
            {"id": 10751, "name": "Family"}, {"id": 10762, "name": "Kids"}, {"id": 9648, "name": "Mystery"},
            {"id": 10763, "name": "News"}, {"id": 10764, "name": "Reality"}, {"id": 10765, "name": "Sci-Fi & Fantasy"},
            {"id": 10766, "name": "Soap"}, {"id": 10767, "name": "Talk"}, {"id": 10768, "name": "War & Politics"},
            {"id": 37, "name": "Western"}
        ]
        return JSONResponse(status_code=200, content={"success": True, "movie_genres": movie_genres, "tv_genres": tv_genres})

    @app.get("/api/web/diagnostics")
    async def web_diagnostics_api():
        """Returns deep system diagnostics, database health, token status, and external connectivity."""
        try:
            diag = {
                "timestamp": time.time(),
                "services": [],
                "connectivity": {},
                "databases": {},
                "platforms": {},
                "debrid": {},
                "summary": {}
            }

            # 1. Read config database
            with db_connect(app.state.config_db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT key, value FROM config")
                cfg = {r[0]: r[1] for r in cur.fetchall()}

            # 2. Check each platform authorization status and prepare pings
            tmdb_k = cfg.get("tmdb_api_key")
            tmdb_u = cfg.get("tmdb_user") or cfg.get("tmdb.user")
            is_tmdb_auth = bool(tmdb_k and tmdb_k not in ('empty_setting', ''))

            trakt_u = cfg.get("trakt_user") or cfg.get("trakt.user")
            trakt_t = cfg.get("trakt_token") or cfg.get("trakt.token")
            is_trakt_auth = bool(trakt_u and trakt_t and trakt_t not in ('empty_setting', ''))

            simkl_u = cfg.get("simkl.user") or cfg.get("simkl_user")
            simkl_t = cfg.get("simkl.token") or cfg.get("simkl_token")
            is_simkl_auth = bool(simkl_t and simkl_t not in ('empty_setting', ''))

            mdb_k = cfg.get("mdblist_api") or cfg.get("mdblist_api_key")
            is_mdb_auth = bool(mdb_k and mdb_k not in ('empty_setting', ''))

            fanart_k = cfg.get("fanart_api_key")
            is_fanart_auth = bool(fanart_k and fanart_k not in ('empty_setting', ''))

            ping_tasks = []

            if is_trakt_auth:
                t_cid = cfg.get("client_id") or "f986871799a140dc20a166adfa637c98c8fa474dc80757aabad8668b99e184de"
                def ping_trakt():
                    r = requests.get(
                        "https://api.trakt.tv/users/me",
                        headers={"trakt-api-version": "2", "trakt-api-key": t_cid, "Authorization": f"Bearer {trakt_t}"},
                        timeout=4
                    )
                    return r.status_code == 200, f"HTTP {r.status_code}"
                ping_tasks.append(({
                    "id": "trakt",
                    "name": "Trakt",
                    "authorized": True,
                    "username": trakt_u or "Authorised"
                }, ping_trakt))

            if is_tmdb_auth:
                def ping_tmdb():
                    r = requests.get(f"https://api.themoviedb.org/3/configuration?api_key={tmdb_k}", timeout=4)
                    return r.status_code == 200, f"HTTP {r.status_code}"
                ping_tasks.append(({
                    "id": "tmdb",
                    "name": "TMDb",
                    "authorized": True,
                    "username": tmdb_u or "API Active"
                }, ping_tmdb))

            if is_simkl_auth:
                s_cid = cfg.get("simkl.client") or cfg.get("simkl.client_id") or cfg.get("simkl_client") or "4c920ba05273be800e843c0a2a4c148e1a17adbbba14c441bc3861214088a296"
                def ping_simkl():
                    r = requests.get(
                        "https://api.simkl.com/sync/all-items",
                        headers={"Content-Type": "application/json", "simkl-api-key": s_cid, "Authorization": f"Bearer {simkl_t}"},
                        timeout=4
                    )
                    return r.status_code == 200, f"HTTP {r.status_code}"
                ping_tasks.append(({
                    "id": "simkl",
                    "name": "Simkl",
                    "authorized": True,
                    "username": simkl_u or "Authorised"
                }, ping_simkl))

            if is_mdb_auth:
                def ping_mdblist():
                    r = requests.get(f"https://mdblist.com/api/user?apikey={mdb_k}", timeout=4)
                    return r.status_code == 200, f"HTTP {r.status_code}"
                ping_tasks.append(({
                    "id": "mdblist",
                    "name": "MDBList",
                    "authorized": True,
                    "username": "API Active"
                }, ping_mdblist))

            if is_fanart_auth:
                def ping_fanart():
                    r = requests.get(f"https://webservice.fanart.tv/v3/movies/11?api_key={fanart_k}", timeout=4)
                    return r.status_code == 200, f"HTTP {r.status_code}"
                ping_tasks.append(({
                    "id": "fanart",
                    "name": "Fanart.tv",
                    "authorized": True,
                    "username": "API Active"
                }, ping_fanart))

            aio_u = cfg.get("aio.username")
            aio_p = cfg.get("aio.password")
            aio_inst = cfg.get("aiostreams_instance") or "0"
            aio_c_url = cfg.get("aio.custom_url")
            is_aio_auth = bool(
                aio_u and aio_u not in ('empty_setting', '') and
                aio_p and aio_p not in ('empty_setting', '') and
                (aio_inst != "1" or (aio_c_url and aio_c_url not in ('empty_setting', '')))
            )
            if is_aio_auth:
                inst_map = {
                    "0": "https://aiostreams.stremio.ru",
                    "2": "https://aiostreams.viren070.me",
                    "3": "https://aiostreams.fortheweak.cloud",
                    "4": "https://aiostreamsfortheweebsstable.midnightignite.me"
                }
                aio_base = aio_c_url.rstrip('/') if aio_inst == "1" and aio_c_url else inst_map.get(aio_inst, "https://aiostreams.stremio.ru")
                def ping_aiostreams():
                    try:
                        r = requests.get(f"{aio_base}/api/v1/search", auth=(aio_u, aio_p), timeout=4)
                        return r.status_code in (200, 400), f"HTTP {r.status_code}"
                    except Exception as err:
                        return False, str(err)
                ping_tasks.append(({
                    "id": "aiostreams",
                    "name": "AIOStreams",
                    "authorized": True,
                    "username": aio_u or "Authorised"
                }, ping_aiostreams))

            # 3. Check authorized debrid services and prepare pings
            debrid_svcs = [
                ("Real-Debrid", "rd.token", "rd.enabled", "rd.priority", 2, "rd"),
                ("Premiumize", "pm.token", "pm.enabled", "pm.priority", 3, "pm"),
                ("TorBox", "tb.token", "tb.enabled", "tb.priority", 1, "tb"),
                ("OffCloud", "oc.token", "oc.enabled", "oc.priority", 5, "oc"),
                ("EasyDebrid", "ed.token", "ed.enabled", "ed.priority", 6, "ed"),
                ("EasyNews", "easynews_user", "provider.easynews", "en.priority", 7, "easynews")
            ]

            debrid_ping_tasks = []
            unconfigured_debrids = []
            for d_name, token_key, enabled_key, prio_key, def_prio, d_id in debrid_svcs:
                tok = cfg.get(token_key)
                en = cfg.get(enabled_key, "false").lower() in ("true", "1")
                has_tok = bool(tok and tok != "empty_setting")
                try:
                    prio_val = int(cfg.get(prio_key, def_prio))
                except (ValueError, TypeError):
                    prio_val = def_prio

                diag["debrid"][d_name] = {
                    "configured": has_tok,
                    "enabled": en and has_tok,
                    "priority": prio_val
                }

                acct_id = None
                if d_id == "pm":
                    acct_id = cfg.get("pm.account_id")
                    if acct_id == "empty_setting":
                        acct_id = None
                elif d_id == "rd":
                    acct_id = cfg.get("rd.account_id")
                    if acct_id == "empty_setting":
                        acct_id = None

                svc_info = {
                    "id": d_id,
                    "name": d_name,
                    "enabled": en and has_tok,
                    "priority": prio_val,
                    "configured": has_tok,
                    "account_id": acct_id
                }

                if not has_tok:
                    unconfigured_debrids.append({
                        **svc_info,
                        "online": False,
                        "status": "not_configured",
                        "latency_ms": None,
                        "detail": "Not authorised"
                    })
                    continue

                if d_name == "Real-Debrid":
                    def ping_rd(t=tok, info=svc_info):
                        r = requests.get("https://api.real-debrid.com/rest/1.0/user", headers={"Authorization": f"Bearer {t}"}, timeout=4)
                        return r.status_code == 200, f"HTTP {r.status_code}"
                    debrid_ping_tasks.append((svc_info, ping_rd))
                elif d_name == "Premiumize":
                    def ping_pm(t=tok, info=svc_info):
                        r = requests.get("https://www.premiumize.me/api/account/info", headers={"Authorization": f"Bearer {t}"}, timeout=4)
                        ok = r.status_code == 200 and r.json().get("status") == "success"
                        return ok, f"HTTP {r.status_code}"
                    debrid_ping_tasks.append((svc_info, ping_pm))
                elif d_name == "TorBox":
                    def ping_tb(t=tok, info=svc_info):
                        r = requests.get("https://api.torbox.app/v1/api/user/me", headers={"Authorization": f"Bearer {t}"}, timeout=4)
                        ok = r.status_code == 200 and r.json().get("success") is True
                        return ok, f"HTTP {r.status_code}"
                    debrid_ping_tasks.append((svc_info, ping_tb))
                elif d_name == "OffCloud":
                    def ping_oc(t=tok, info=svc_info):
                        r = requests.get(f"https://offcloud.com/api/remote/account?key={t}", timeout=4)
                        return r.status_code == 200, f"HTTP {r.status_code}"
                    debrid_ping_tasks.append((svc_info, ping_oc))
                elif d_name == "EasyDebrid":
                    def ping_ed(t=tok, info=svc_info):
                        r = requests.get("https://easydebrid.com/api/v1/user/details", headers={"Authorization": f"Bearer {t}"}, timeout=4)
                        return r.status_code == 200, f"HTTP {r.status_code}"
                    debrid_ping_tasks.append((svc_info, ping_ed))
                elif d_name == "EasyNews":
                    pwd = cfg.get("easynews_password")
                    def ping_en(u=tok, p=pwd, info=svc_info):
                        r = requests.get("https://account.easynews.com/editinfo.php", auth=(u, p), timeout=4)
                        return r.status_code == 200, f"HTTP {r.status_code}"
                    debrid_ping_tasks.append((svc_info, ping_en))

            def run_all_pings():
                def execute_one(item):
                    cat, (info, fn) = item
                    t0 = time.perf_counter()
                    try:
                        ok, detail = fn()
                        lat = round((time.perf_counter() - t0) * 1000)
                        return cat, {
                            **info,
                            "online": ok,
                            "status": "ok" if ok else "error",
                            "latency_ms": lat,
                            "detail": detail
                        }
                    except Exception as e:
                        return cat, {
                            **info,
                            "online": False,
                            "status": "unreachable",
                            "latency_ms": None,
                            "detail": str(e)
                        }

                tasks = [("service", t) for t in ping_tasks] + [("debrid", t) for t in debrid_ping_tasks]
                if not tasks:
                    return [], []

                with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
                    results = list(ex.map(execute_one, tasks))

                srv_res = [r[1] for r in results if r[0] == "service"]
                deb_res = [r[1] for r in results if r[0] == "debrid"]
                return srv_res, deb_res

            srv_results, deb_results = await asyncio.to_thread(run_all_pings)
            all_debrid = deb_results + unconfigured_debrids
            all_debrid.sort(key=lambda x: x.get("priority", 10))
            diag["services"] = srv_results
            diag["debrid_services"] = all_debrid

            for s in diag["services"]:
                diag["connectivity"][s["id"]] = {
                    "status": s["status"],
                    "latency_ms": s["latency_ms"]
                }

            # Database sizes & health
            db_paths = {
                "movies_static": getattr(app.state, 'movies_static_db_path', None),
                "movies_dynamic": getattr(app.state, 'movies_dynamic_db_path', None),
                "tvshows_static": getattr(app.state, 'tvshows_static_db_path', None),
                "tvshows_dynamic": getattr(app.state, 'tvshows_dynamic_db_path', None),
                "lists": getattr(app.state, 'lists_db_path', None),
                "ext_indexes": getattr(app.state, 'ext_indexes_db_path', None),
                "tags": getattr(app.state, 'tags_db_path', None),
                "config": getattr(app.state, 'config_db_path', None)
            }

            total_size_bytes = 0
            for name, path in db_paths.items():
                if not path:
                    continue
                db_info = {"exists": os.path.exists(path), "size_kb": 0, "wal_size_kb": 0, "integrity": "unknown", "row_count": 0}
                if os.path.exists(path):
                    size = os.path.getsize(path)
                    total_size_bytes += size
                    db_info["size_kb"] = round(size / 1024, 1)
                    wal_path = f"{path}-wal"
                    if os.path.exists(wal_path):
                        wal_size = os.path.getsize(wal_path)
                        total_size_bytes += wal_size
                        db_info["wal_size_kb"] = round(wal_size / 1024, 1)

                    try:
                        with db_connect(path) as conn:
                            cur = conn.cursor()
                            cur.execute("PRAGMA quick_check")
                            res = cur.fetchone()
                            db_info["integrity"] = res[0] if res else "ok"

                            if name == "movies_static":
                                cur.execute("SELECT COUNT(*) FROM movies")
                                db_info["row_count"] = cur.fetchone()[0]
                            elif name == "tvshows_static":
                                cur.execute("SELECT COUNT(*) FROM shows")
                                db_info["row_count"] = cur.fetchone()[0]
                            elif name == "lists":
                                cur.execute("SELECT COUNT(*) FROM lists")
                                db_info["row_count"] = cur.fetchone()[0]
                            elif name == "ext_indexes":
                                cur.execute("SELECT COUNT(*) FROM external_indexes")
                                ext_c = cur.fetchone()[0]
                                try:
                                    cur.execute("SELECT COUNT(*) FROM internal_indexes")
                                    int_c = cur.fetchone()[0]
                                except:
                                    int_c = 0
                                db_info["row_count"] = ext_c + int_c
                            elif name == "tags":
                                cur.execute("SELECT COUNT(*) FROM tags")
                                db_info["row_count"] = cur.fetchone()[0]
                    except Exception as err:
                        db_info["integrity"] = f"error: {err}"

                diag["databases"][name] = db_info

            diag["summary"]["total_db_size_mb"] = round(total_size_bytes / (1024 * 1024), 2)

            # Platform & Token status summary
            trakt_exp = cfg.get("trakt_expires")
            diag["platforms"]["trakt"] = {
                "authorized": is_trakt_auth,
                "username": trakt_u or "Not configured",
                "expires": trakt_exp if trakt_exp and trakt_exp != "empty_setting" else None
            }
            diag["platforms"]["simkl"] = {
                "authorized": is_simkl_auth,
                "username": simkl_u or "Not configured"
            }
            diag["platforms"]["tmdb"] = {
                "authorized": is_tmdb_auth,
                "username": tmdb_u or "API Active"
            }
            diag["platforms"]["mdblist"] = {
                "authorized": is_mdb_auth,
                "has_api_key": is_mdb_auth
            }
            diag["platforms"]["fanart"] = {
                "authorized": is_fanart_auth,
                "has_api_key": is_fanart_auth
            }
            diag["platforms"]["aiostreams"] = {
                "authorized": is_aio_auth,
                "username": aio_u if is_aio_auth else "Not configured"
            }

            # Scraper DB Metrics
            try:
                scraper_db = ScraperDB('scrapers.db')
                metrics = scraper_db.get_all_scrapers()
                diag["scrapers"] = {
                    "total": len(metrics),
                    "active": len([m for m in metrics if m.get("active", True)]),
                    "total_scrapes": sum(m.get("scrapes", 0) for m in metrics)
                }
            except Exception:
                diag["scrapers"] = {"total": 0, "active": 0, "total_scrapes": 0}

            try:
                diag["results_sort_order"] = int(cfg.get("results.sort_order", "0"))
            except (ValueError, TypeError):
                diag["results_sort_order"] = 0

            return JSONResponse(status_code=200, content={"success": True, "diagnostics": diag})
        except Exception as e:
            log(f"Error generating system diagnostics: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/web/results_sort_order")
    async def set_results_sort_order(request: Request):
        try:
            body = await request.json()
            sort_order = body.get("sort_order")
            if sort_order is None:
                return JSONResponse(status_code=400, content={"status": "error", "error": "sort_order required"})
            sort_order = int(sort_order)
            if not (0 <= sort_order <= 5):
                return JSONResponse(status_code=400, content={"status": "error", "error": "sort_order must be 0-5"})
            update_config_values({"results.sort_order": str(sort_order)}, app.state.config_db_path)
            log(f"[ScrapeHandler] Updated results.sort_order to {sort_order}", level=LOGINFO)
            return JSONResponse(status_code=200, content={"status": "success", "sort_order": sort_order})
        except Exception as e:
            log(f"[ScrapeHandler] Error setting results sort order: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})

    @app.post("/api/web/debrid_priority")
    async def set_debrid_priority(request: Request):
        try:
            body = await request.json()
            provider = body.get("provider")
            priority = body.get("priority")
            if not provider or priority is None:
                return JSONResponse(status_code=400, content={"status": "error", "error": "provider and priority required"})
            
            p_key = {
                'torbox': 'tb.priority',
                'real-debrid': 'rd.priority',
                'realdebrid': 'rd.priority',
                'premiumize': 'pm.priority',
                'premiumize.me': 'pm.priority',
                'alldebrid': 'ad.priority',
                'offcloud': 'oc.priority',
                'easydebrid': 'ed.priority',
                'easynews': 'en.priority'
            }.get(str(provider).lower().strip())
            
            if not p_key:
                return JSONResponse(status_code=400, content={"status": "error", "error": f"Unknown provider {provider}"})
                
            update_config_values({p_key: str(priority)}, app.state.config_db_path)
            log(f"[DebridResolver] Updated priority for {provider} ({p_key}) to {priority}", level=LOGINFO)
            return JSONResponse(status_code=200, content={"status": "success", "provider": provider, "priority": priority})
        except Exception as e:
            log(f"[DebridResolver] Error setting debrid priority: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})

    @app.post("/api/web/vacuum")
    async def web_vacuum_api():
        """Performs database maintenance and VACUUM on all Orac SQLite databases."""
        try:
            db_paths = [
                getattr(app.state, 'movies_static_db_path', None),
                getattr(app.state, 'movies_dynamic_db_path', None),
                getattr(app.state, 'tvshows_static_db_path', None),
                getattr(app.state, 'tvshows_dynamic_db_path', None),
                getattr(app.state, 'lists_db_path', None),
                getattr(app.state, 'ext_indexes_db_path', None),
                getattr(app.state, 'tags_db_path', None),
                getattr(app.state, 'config_db_path', None)
            ]
            if os.path.exists('scrapers.db'):
                db_paths.append('scrapers.db')

            def run_vacuums():
                results = []
                for p in db_paths:
                    if not p or not os.path.exists(p):
                        continue
                    initial_size = os.path.getsize(p)
                    try:
                        with db_connect(p) as conn:
                            conn.execute("VACUUM")
                            conn.execute("PRAGMA optimize")
                        new_size = os.path.getsize(p)
                        results.append({
                            "database": os.path.basename(p),
                            "status": "success",
                            "freed_kb": round((initial_size - new_size) / 1024, 1)
                        })
                    except Exception as err:
                        results.append({
                            "database": os.path.basename(p),
                            "status": "error",
                            "error": str(err)
                        })
                return results

            results = await asyncio.to_thread(run_vacuums)
            return JSONResponse(status_code=200, content={"success": True, "results": results})
        except Exception as e:
            log(f"Error during manual database vacuum: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    # --- VERSION / UPDATE CHECK ROUTES ---
    @app.get("/api/version")
    async def api_version():
        """Returns the running version and latest available version from GitHub."""
        return JSONResponse(status_code=200, content=get_update_state())

    @app.get("/api/web/version")
    async def web_version_api():
        """Web-dashboard version endpoint — same data wrapped with success flag."""
        return JSONResponse(status_code=200, content={"success": True, **get_update_state()})

    # --- PUT ROUTES ---

    @app.put("/watched")
    async def put_watched(request: Request):
        query = parse_qs_fastapi(request)
        watched_type = query.get("type", [None])[0]
        username = await get_t_user(app)
        
        if watched_type == "episode":
            season = query.get("season", [None])[0]
            episode = query.get("episode", [None])[0]
            watched_tmdb_id = query.get("tmdb_id", [None])[0]
            if not season or not episode or not watched_tmdb_id:
                return PlainTextResponse("Missing tmdb_id, season or episode number", status_code=400)
            
            show_trakt_id = int(query.get("show_trakt_id", [0])[0])
            pw = query.get("percent_watched", [None])[0]
            percent = int(float(pw)) if pw else 100
            update_next_episode(
                app.state.tvshows_static_db_path, app.state.tvshows_dynamic_db_path, app.state.trakt_update_queue_path,
                app.state.trakt_handler, app.state.tmdb_handler, watched_type, int(watched_tmdb_id), show_trakt_id, int(season), int(episode),
                percent_watched=percent, username=username
            )
            return Response(status_code=204)
        elif watched_type == "movie":
            movie_tmdb_id = query.get("tmdb_id", [None])[0]
            if not movie_tmdb_id:
                return PlainTextResponse("Missing tmdb_id for movie", status_code=400)
            pw = query.get("percent_watched", [None])[0]
            percent = int(float(pw)) if pw else 100
            mark_movie_watched(
                app.state.movies_static_db_path, app.state.movies_dynamic_db_path, app.state.trakt_update_queue_path, app.state.trakt_handler, app.state.tmdb_handler, int(movie_tmdb_id), percent_watched=percent, username=username
            )
            return Response(status_code=204)
        elif watched_type == "tvshow":
            watched_tmdb_id = query.get("tmdb_id", [None])[0]
            if not watched_tmdb_id:
                 return PlainTextResponse("Missing tmdb_id for tvshow", status_code=400)
            pw = query.get("percent_watched", [None])[0]
            percent = int(float(pw)) if pw else 100
            mark_tvshow_watched(
                app.state.tvshows_static_db_path, app.state.tvshows_dynamic_db_path, app.state.trakt_update_queue_path, app.state.trakt_handler, app.state.tmdb_handler, int(watched_tmdb_id), percent_watched=percent, username=username
            )
            return Response(status_code=204)
        return Response(status_code=400)

    @app.put("/drop_tvshow")
    async def drop_tv(request: Request):
        query = parse_qs_fastapi(request)
        watched_tmdb_id = query.get("tmdb_id", [None])[0]
        if not watched_tmdb_id:
            return PlainTextResponse("Missing tmdb_id", status_code=400)
        username = await get_t_user(app)
        drop_tvshow(app.state.tvshows_static_db_path, app.state.tvshows_dynamic_db_path, app.state.trakt_update_queue_path, app.state.trakt_handler, int(watched_tmdb_id), username=username, config_db_path=getattr(app.state, 'config_db_path', None))
        return Response(status_code=204)

    @app.put("/add_to_list")
    async def p_add_to_list(request: Request):
        query = parse_qs_fastapi(request)
        result = add_to_list(query, app.state.trakt_handler, app.state.tmdb_handler, app.state.lists_db_path, app.state.movies_static_db_path, app.state.tvshows_static_db_path, app.state.trakt_update_queue_path)
        return JSONResponse(status_code=200 if result.get("status") == "success" else 400, content=result)

    @app.put("/remove_from_list")
    async def p_rm_from_list(request: Request):
        query = parse_qs_fastapi(request)
        result = remove_from_list(query, app.state.trakt_handler, app.state.tmdb_handler, app.state.lists_db_path, app.state.movies_static_db_path, app.state.tvshows_static_db_path, app.state.trakt_update_queue_path)
        return JSONResponse(status_code=200 if result.get("status") == "success" else 400, content=result)

    @app.put("/update_trakt_tokens")
    async def update_t_tokens(request: Request):
        params = flat_qs(request)
        trakt_token = params.get("trakt_token")
        if not trakt_token or trakt_token in ("empty_setting", ""):
            from .config_handler import clear_trakt_config
            clear_trakt_config(app.state.config_db_path)
            if app.state.trakt_handler:
                app.state.trakt_handler.reload_credentials()
            log("[Orac] Trakt tokens cleared / authorization revoked.", level=LOGINFO)
            return JSONResponse(status_code=200, content={"status": "success", "message": "Trakt tokens cleared"})

        result = update_config_values(params, app.state.config_db_path)
        if app.state.trakt_handler:
            app.state.trakt_handler.reload_credentials()
            await app.state.trakt_handler.fetch_username()
        return JSONResponse(status_code=200, content={"status": "success", "message": "Trakt tokens updated"})


    @app.put("/update_simkl_tokens")
    async def update_s_tokens(request: Request):
        result = update_config_values(flat_qs(request), app.state.config_db_path)
        return JSONResponse(status_code=200, content={"status": "success", "message": "Simkl tokens updated"})

    @app.post("/sync_trakt_to_mdblist")
    async def trigger_trakt_to_mdblist_sync(request: Request):
        from resources.lib.trakt_to_mdblist_sync import sync_trakt_lists_to_mdblist_task
        asyncio.create_task(sync_trakt_lists_to_mdblist_task(
            app.state.config_db_path,
            app.state.trakt_handler,
            lists_db_path=app.state.lists_db_path,
            tmdb_handler=app.state.tmdb_handler
        ))
        return JSONResponse(status_code=200, content={"status": "success", "message": "Trakt to MDBList sync triggered"})

    @app.put("/update_mdblist_tokens")
    async def update_m_tokens(request: Request):
        success = update_config_values(flat_qs(request), app.state.config_db_path)
        return JSONResponse(status_code=200 if success else 500, content={"status": "success" if success else "error"})

    @app.put("/update_debrid_tokens")
    async def update_deb_tokens(request: Request):
        success = update_config_values(flat_qs(request), app.state.config_db_path)
        return JSONResponse(status_code=200 if success else 500, content={"status": "success" if success else "error"})

    @app.put("/update_aiostreams_settings")
    async def update_aio_settings(request: Request):
        params = flat_qs(request)
        # Mask password for logging
        log_params = {k: (v[:2] + '***' if k == 'aio.password' and v and v not in ('empty_setting', '') else v)
                      for k, v in params.items()}
        log(f"[AIOStreams] PUT /update_aiostreams_settings received: {log_params}", level=LOGINFO)
        success = update_config_values(params, app.state.config_db_path)
        log(f"[AIOStreams] update_config_values result: {success}", level=LOGINFO)
        if success:
            username = get_config_value("aio.username", app.state.config_db_path, "empty_setting")
            password = get_config_value("aio.password", app.state.config_db_path, "empty_setting")
            instance = get_config_value("aiostreams_instance", app.state.config_db_path, "0")
            custom_url = get_config_value("aio.custom_url", app.state.config_db_path, "empty_setting")
            masked_pw = (password[:2] + '***') if password and password not in ('empty_setting', '') else repr(password)
            log(f"[AIOStreams] Values now in config DB: username={repr(username)}, password={masked_pw}, instance={repr(instance)}, custom_url={repr(custom_url)}", level=LOGINFO)

            is_active = (
                username not in (None, "", "empty_setting") and
                password not in (None, "", "empty_setting") and
                (instance != "1" or custom_url not in (None, "", "empty_setting"))
            )
            if not is_active:
                log(f"[AIOStreams] is_active=False because: "
                    f"username_ok={username not in (None, '', 'empty_setting')}, "
                    f"password_ok={password not in (None, '', 'empty_setting')}, "
                    f"instance={repr(instance)}, "
                    f"custom_url_ok={custom_url not in (None, '', 'empty_setting')}", level=LOGINFO)
            else:
                log(f"[AIOStreams] is_active=True - scraper will be enabled", level=LOGINFO)
            scraper_db = ScraperDB('scrapers.db')
            scraper_db.set_active_status('aiostreams', is_active)
            log(f"[AIOStreams] Scraper active status set to: {is_active}", level=LOGINFO)
        return JSONResponse(status_code=200 if success else 500, content={"status": "success" if success else "error"})

    @app.put("/update_tmdb_tokens")
    async def update_tmdb(request: Request):
        params = flat_qs(request)
        success = update_config_values(params, app.state.config_db_path)
        if "tmdb_api_key" in params and app.state.tmdb_handler:
            app.state.tmdb_handler.api_key = params["tmdb_api_key"]
        return JSONResponse(status_code=200 if success else 500, content={"status": "success" if success else "error"})


    @app.put("/mark_undesirable")
    async def mark_und(request: Request):
        params = flat_qs(request)
        stream_name = params.get('stream_name')
        if not stream_name:
            return JSONResponse(status_code=400, content={"status": "error", "message": "Missing 'stream_name'"})
        extracted = stream_name.split('-')[-1].strip().lower() if '-' in stream_name else stream_name.strip().lower()
        from resources.scrapers.modules.undesirables import Undesirables
        undesirables_db = Undesirables()
        undesirables_db.set_many([(extracted, True, True)])
        return JSONResponse(status_code=200, content={"status": "success", "message": f"Added '{extracted}'"})

    @app.put("/add_ext_index")
    async def add_ext(request: Request):
        try:
            json_body = await request.json()
            success = add_external_index(json_body, app.state.ext_indexes_db_path)
            return JSONResponse(status_code=200 if success else 500, content={"status": "success" if success else "error"})
        except:
            return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid body"})

    @app.put("/del_ext_index")
    async def del_ext(request: Request):
        params = flat_qs(request)
        success = del_external_index(params, app.state.ext_indexes_db_path)
        return JSONResponse(status_code=200 if success else 500, content={"status": "success" if success else "error"})

    @app.put("/add_internal_index")
    async def add_int(request: Request):
        try:
            json_body = await request.json()
            success = add_internal_index(json_body, app.state.ext_indexes_db_path)
            return JSONResponse(status_code=200 if success else 500, content={"status": "success" if success else "error"})
        except:
            return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid body"})

    @app.put("/del_internal_index")
    async def del_int(request: Request):
        params = flat_qs(request)
        success = del_internal_index(params, app.state.ext_indexes_db_path)
        return JSONResponse(status_code=200 if success else 500, content={"status": "success" if success else "error"})

    @app.put("/update_list_library_status")
    async def up_list_stat(request: Request):
        success = update_list_library_status(flat_qs(request), app.state.lists_db_path, app.state.ext_indexes_db_path)
        return JSONResponse(status_code=200 if success else 500, content={"status": "success" if success else "error"})

    @app.put("/unlike_trakt_list")
    async def unlike(request: Request):
        params = flat_qs(request)
        list_name = params.get("list_name")
        trakt_user = params.get("user") or await get_t_user(app)
        slug = params.get("slug")
        if not list_name or not trakt_user or not slug:
             return JSONResponse(status_code=400, content={"status": "error"})
        with db_connect(app.state.trakt_update_queue_path) as conn:
            cursor = conn.cursor()
            queue_payload = {"list_name": list_name, "item_type": 'list', "tmdb_id": None, "user": trakt_user, "slug": slug}
            cursor.execute("INSERT INTO trakt_update_queue (trakt_id, update_type, payload, status, media_type) VALUES (?, ?, ?, 'pending', ?)", (trakt_user, 'unlike_trakt_list', json.dumps(queue_payload), 'list'))
            conn.commit()
        list_id = None
        with db_connect(app.state.lists_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT list_id FROM lists WHERE slug = ?", (slug,))
            row = cursor.fetchone()
            if row: list_id = row[0]
        if not list_id: list_id = f"{trakt_user}:{slug}"
        delete_list_locally(list_id, app.state.lists_db_path)
        return JSONResponse(status_code=200, content={"status": "success"})

    @app.put("/add_tag")
    async def add_tag(request: Request):
        try:
            json_body = await request.json()
            params = flat_qs(request)
            media_type = params.get('media_type')
            tmdb_id = int(params.get('tmdb_id'))
            tag_name = json_body.get('tag')
            success = add_tag_to_item(app.state.tags_db_path, media_type, tmdb_id, tag_name, movies_static_db_path=app.state.movies_static_db_path, tvshows_static_db_path=app.state.tvshows_static_db_path)
            return JSONResponse(status_code=200 if success else 500, content={"success": success})
        except:
            return JSONResponse(status_code=400, content={"success": False, "error": "Invalid request"})

    @app.put("/remove_tag")
    async def remove_tag(request: Request):
         params = flat_qs(request)
         success = remove_tag_from_item(app.state.tags_db_path, params.get('media_type'), int(params.get('tmdb_id')), params.get('tag'))
         return JSONResponse(status_code=200 if success else 500, content={"success": success})

    @app.post("/api/config/fanart")
    async def update_fanart_settings_endpoint(request: Request):
        try:
            data = await request.json()
            success = update_config_values(data, app.state.config_db_path)
            return JSONResponse(status_code=200 if success else 500, content={"success": success})
        except Exception as e:
            log(f"Error updating fanart settings: {e}", level=LOGERROR)
            return JSONResponse(status_code=400, content={"success": False, "error": str(e)})

    @app.post("/api/sync/fanart/latest")
    async def force_fanart_latest_sync(request: Request):
        try:
            from resources.lib.fanart_client import run_fanart_latest_sync
            import threading
            threading.Thread(
                target=run_fanart_latest_sync,
                args=(app.state.config_db_path, app.state.tmdb_handler),
                daemon=True,
                name="ManualFanartSync"
            ).start()
            return JSONResponse(status_code=200, content={"success": True, "message": "Fanart latest sync triggered."})
        except Exception as e:
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.get("/assets/images/{media_item}/{filename}")
    async def serve_asset_image(media_item: str, filename: str):
        assets_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "images")
        file_path = os.path.join(assets_dir, media_item, filename)

        # 1. If file exists on disk, serve it immediately
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            return FileResponse(file_path)

        # 2. Attempt on-demand download via Fanart if missing
        try:
            parts = media_item.split("_")
            if len(parts) >= 2:
                media_type = parts[0]  # "movie" or "show"
                item_id = int(parts[1])
                from resources.lib.fanart_client import sync_fanart_for_item
                await asyncio.to_thread(
                    sync_fanart_for_item,
                    item_id,
                    media_type,
                    app.state.tmdb_handler,
                    app.state.config_db_path,
                    force=True
                )
                if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    return FileResponse(file_path)
        except Exception as e:
            log(f"[Orac] Error attempting on-demand fanart download for {media_item}/{filename}: {e}", level=LOGWARNING)

        # 3. Fallback: redirect to TMDB if file is still not on disk
        try:
            parts = media_item.split("_")
            if len(parts) >= 2:
                media_type = parts[0]
                item_id = int(parts[1])
                if app.state.tmdb_handler:
                    endpoint = f"/movie/{item_id}" if media_type == "movie" else f"/tv/{item_id}"
                    tmdb_data = await asyncio.to_thread(app.state.tmdb_handler._get, endpoint)
                    if tmdb_data:
                        if "poster" in filename:
                            p_path = tmdb_data.get("poster_path")
                            if p_path:
                                return RedirectResponse(f"https://image.tmdb.org/t/p/w780{p_path}")
                        elif "fanart" in filename or "landscape" in filename:
                            b_path = tmdb_data.get("backdrop_path")
                            if b_path:
                                return RedirectResponse(f"https://image.tmdb.org/t/p/w1280{b_path}")
        except Exception as e:
            log(f"[Orac] Error fetching TMDb fallback for {media_item}/{filename}: {e}", level=LOGWARNING)

        raise HTTPException(status_code=404, detail="Asset not found")

    # Serve static UI files at /web
    # The directory needs to exist to mount properly
    ui_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web_ui")
    if os.path.exists(ui_dir):
        app.mount("/web", StaticFiles(directory=ui_dir, html=True), name="web")

    # Serve static downloaded image files at /assets/images
    assets_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "images")
    os.makedirs(assets_dir, exist_ok=True)
    app.mount("/assets/images", StaticFiles(directory=assets_dir), name="assets_images")

    return app

def start_http_server(**kwargs):
    import uvicorn
    app = app_factory(**kwargs)
    port = kwargs.get("port", 5555)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    def run():
        # Uvicorn run loop
        asyncio.run(server.serve())
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return server