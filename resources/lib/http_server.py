import asyncio
import json
import sqlite3
import threading
import os
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlparse, unquote

import xbmc
from fastapi import FastAPI, Request, Response, HTTPException, Path, Query
from fastapi.responses import JSONResponse, PlainTextResponse
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
from resources.lib.indexing import get_genres
from resources.scrapers.scraper_manager import ScraperManager
from resources.lib.scraper_db import ScraperDB

from .movies_handler import handle_movie_request
from .discover_handler import handle_discover_request
from .shows_handler import handle_show_request
from .search_handler import search_tmdb
from .config_handler import update_config_values, get_trakt_user, get_config_value
from .indexing import add_external_index, del_external_index
from .internal_indexing import add_internal_index, del_internal_index, get_internal_indexes, get_internal_index_contents, get_available_languages
from .scrape_handler import handle_scrape_request
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
            with sqlite3.connect(db_path) as conn:
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
    if app.state.trakt_handler and app.state.trakt_handler.username:
         return app.state.trakt_handler.username
    user = get_trakt_user(config_db_path=app.state.config_db_path)
    if user:
        if app.state.trakt_handler:
            app.state.trakt_handler.username = user
        return user
    if app.state.trakt_handler:
        user = await app.state.trakt_handler.fetch_username()
        if user:
             return user
    log("Trakt user not found in config DB or via TraktHandler.", LOGWARNING)
    return None

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

        
        sync_task = asyncio.create_task(hourly_sync_loop())
        yield
        sync_task.cancel()
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

    # Create responses safely
    def send_safe(status, body, content_type="application/json"):
        if isinstance(body, dict) or isinstance(body, list):
            return JSONResponse(status_code=status, content=body)
        if isinstance(body, bytes):
            return Response(content=body, status_code=status, media_type=content_type)
        if isinstance(body, str):
            return Response(content=body.encode("utf-8"), status_code=status, media_type=content_type)
        return Response(content=str(body), status_code=status, media_type=content_type)

    @app.get("/ping")
    async def ping():
        return PlainTextResponse("Yes, what do you want?")

    @app.get("/api/status")
    async def api_status():
        try:
            with sqlite3.connect(app.state.config_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT key, value FROM config 
                    WHERE key IN (
                        'trakt_user', 'trakt_token', 'trakt_refresh', 'trakt_expires',
                        'simkl.user', 'simkl_user', 'simkl.token',
                        'tmdb_user', 'tmdb.user', 'tmdb_session_id',
                        'mdblist_api'
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
                    }
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
            result = get_generic_lists(app.state.lists_db_path, list_name, item_type)
        
        if result is not None:
            return JSONResponse(status_code=200, content=result)
        else:
            return PlainTextResponse("Error getting lists", status_code=500)

    @app.get("/next_episodes")
    async def next_episodes(request: Request):
        query = parse_qs_fastapi(request)
        trakt_user = query.get("user", [None])[0] or await get_t_user(app)
        result = get_next_episodes(app.state.tvshows_dynamic_db_path, app.state.tvshows_static_db_path, trakt_user)
        if result is not None:
            return JSONResponse(status_code=200, content=result)
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
            global_loop=asyncio.get_running_loop()
        )
        return send_safe(status, body, content_type)

    @app.get("/fast_start_episode")
    async def fast_start_episode(request: Request):
        query = parse_qs_fastapi(request)
        query['results_limit'] = ['4']
        status, body, content_type = await handle_scrape_request(
            query, app.state.scraper_manager, app.state.movies_static_db_path, app.state.tvshows_static_db_path,
            tmdb_handler=app.state.tmdb_handler, results_limit=4, global_loop=asyncio.get_running_loop()
        )
        return send_safe(status, body, content_type)

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
                cursor.execute("SELECT * FROM external_indexes where media_type = ?", (media_type,))
                rows = cursor.fetchall()
                indexes = []
                for row in rows:
                    index_item = dict(row)
                    if 'parameters' in index_item and isinstance(index_item['parameters'], str):
                        index_item['parameters'] = json.loads(index_item['parameters'])
                    indexes.append(index_item)
                return JSONResponse(status_code=200, content={"success": True, "indexes": indexes})
        except Exception as e:
            return JSONResponse(status_code=500, content={"success": False, "error": "Error fetching external indexes"})

    @app.get("/get_internal_indexes")
    async def get_int_indexes(request: Request):
        query = parse_qs_fastapi(request)
        media_type = query.get("item_type", [None])[0]
        if not media_type:
            return JSONResponse(status_code=400, content={"success": False, "error": "Missing item_type"})
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
            elif media_type == 'tvshow':
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
        except:
            max_reviews = 20
        reviews = app.state.tmdb_handler.get_reviews(tmdb_id, media_type=media_type, max_reviews=max_reviews)
        if media_type == 'movie':
            try:
                with sqlite3.connect(app.state.movies_static_db_path) as conn:
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
            with sqlite3.connect(app.state.config_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM config WHERE key IN ('trakt_user', 'simkl_user', 'tmdb_user', 'mdblist.user', 'mdblist_user')")
                rows = cursor.fetchall()
                
                # Normalize keys slightly in case of duplicates or variants
                data = {row[0]: row[1] for row in rows}
                
                platforms = []
                if 'trakt_user' in data:
                    platforms.append({"name": "Trakt", "username": data['trakt_user']})
                if 'simkl_user' in data:
                    platforms.append({"name": "Simkl", "username": data['simkl_user']})
                if 'tmdb_user' in data:
                    platforms.append({"name": "TMDb", "username": data['tmdb_user']})
                
                mdblist_user = data.get('mdblist.user') or data.get('mdblist_user')
                if mdblist_user:
                    platforms.append({"name": "MDBList", "username": mdblist_user})
                    
                return JSONResponse(status_code=200, content={"success": True, "platforms": platforms})
        except Exception as e:
            log(f"Error fetching platform tokens from config DB: {e}", level=LOGERROR)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.get("/api/web/library_lists")
    async def web_library_lists_api():
        try:
            with sqlite3.connect(app.state.lists_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name, source FROM lists WHERE add_to_library = 1 OR add_to_library = 'true' OR add_to_library = 'True'")
                rows = cursor.fetchall()
                lists = [{"name": row[0], "source": row[1]} for row in rows]
                return JSONResponse(status_code=200, content={"success": True, "lists": lists})
        except Exception as e:
            log(f"Error fetching library lists: {e}", level=LOGERROR)
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
        drop_tvshow(app.state.tvshows_static_db_path, app.state.tvshows_dynamic_db_path, app.state.trakt_update_queue_path, app.state.trakt_handler, int(watched_tmdb_id), username=username)
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
        result = update_config_values(flat_qs(request), app.state.config_db_path)
        if app.state.trakt_handler:
            app.state.trakt_handler.reload_credentials()
            await app.state.trakt_handler.fetch_username()
        return PlainTextResponse("Trakt tokens updated", status_code=200)

    @app.put("/update_simkl_tokens")
    async def update_s_tokens(request: Request):
        result = update_config_values(flat_qs(request), app.state.config_db_path)
        return PlainTextResponse("Simkl tokens updated.", status_code=200)

    @app.put("/update_mdblist_tokens")
    async def update_m_tokens(request: Request):
        success = update_config_values(flat_qs(request), app.state.config_db_path)
        return Response(status_code=204) if success else PlainTextResponse("Error", status_code=500)

    @app.put("/update_aiostreams_settings")
    async def update_aio_settings(request: Request):
        params = flat_qs(request)
        success = update_config_values(params, app.state.config_db_path)
        if success:
            username = get_config_value("aio.username", app.state.config_db_path, "empty_setting")
            password = get_config_value("aio.password", app.state.config_db_path, "empty_setting")
            instance = get_config_value("aiostreams_instance", app.state.config_db_path, "0")
            custom_url = get_config_value("aio.custom_url", app.state.config_db_path, "empty_setting")
            
            is_active = (
                username not in (None, "", "empty_setting") and
                password not in (None, "", "empty_setting") and
                (instance != "1" or custom_url not in (None, "", "empty_setting"))
            )
            scraper_db = ScraperDB('scrapers.db')
            scraper_db.set_active_status('aiostreams', is_active)
        return Response(status_code=204) if success else PlainTextResponse("Error", status_code=500)

    @app.put("/update_tmdb_tokens")
    async def update_tmdb(request: Request):
        params = flat_qs(request)
        success = update_config_values(params, app.state.config_db_path)
        if "tmdb_api_key" in params and app.state.tmdb_handler:
            app.state.tmdb_handler.api_key = params["tmdb_api_key"]
        return Response(status_code=204) if success else PlainTextResponse("Error", status_code=500)

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
        success = update_list_library_status(flat_qs(request), app.state.lists_db_path)
        return JSONResponse(status_code=200 if success else 500, content={"status": "success" if success else "error"})

    @app.put("/unlike_trakt_list")
    async def unlike(request: Request):
        params = flat_qs(request)
        list_name = params.get("list_name")
        trakt_user = params.get("user") or await get_t_user(app)
        slug = params.get("slug")
        if not list_name or not trakt_user or not slug:
             return JSONResponse(status_code=400, content={"status": "error"})
        with sqlite3.connect(app.state.trakt_update_queue_path) as conn:
            cursor = conn.cursor()
            queue_payload = {"list_name": list_name, "item_type": 'list', "tmdb_id": None, "user": trakt_user, "slug": slug}
            cursor.execute("INSERT INTO trakt_update_queue (trakt_id, update_type, payload, status, media_type) VALUES (?, ?, ?, 'pending', ?)", (trakt_user, 'unlike_trakt_list', json.dumps(queue_payload), 'list'))
            conn.commit()
        list_id = None
        with sqlite3.connect(app.state.lists_db_path) as conn:
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

    # Serve static UI files at /web
    # The directory needs to exist to mount properly
    ui_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web_ui")
    if os.path.exists(ui_dir):
        app.mount("/web", StaticFiles(directory=ui_dir, html=True), name="web")

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