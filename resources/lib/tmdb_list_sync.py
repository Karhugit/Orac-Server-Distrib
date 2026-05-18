import asyncio
import sqlite3
import json
from datetime import datetime
from resources.lib.tmdb_lists import TMDB_GENERIC_LISTS
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
from resources.lib.trakt_list_sync import run_list_sync
from resources.lib.sync_trakt_with_db import resolve_tmdb_to_trakt

async def tmdb_list_sync_task(trakt_auth, tmdb_handler, tmdb_user, tmdb_session_id, lists_db_path, movie_static_db_path, movie_dynamic_db_path, tvshows_static_db_path, tvshows_dynamic_db_path, trakt_queue_path):
    try:
        log(f"[Orac] Starting TMDB list sync for user: {tmdb_user}", level=LOGINFO)

        # 1. Get Account Details (ID is needed for v3 list calls)
        account_details = tmdb_handler.get_account_details(tmdb_session_id)
        if not account_details or 'id' not in account_details:
            log(f"[Orac] Failed to get TMDB account details for session.", level=LOGERROR)
            return
        
        account_id = account_details['id']

        # 2. Fetch Lists
        lists_to_sync_data = []

        # Watchlist Movies
        watchlist_movies = []
        page = 1
        while True:
            resp = tmdb_handler.get_watchlist_movies(account_id, tmdb_session_id, page=page)
            if resp and 'results' in resp:
                watchlist_movies.extend(resp['results'])
                if page >= resp.get('total_pages', 0):
                    break
                page += 1
            else:
                break
        
        if watchlist_movies:
            lists_to_sync_data.append(normalize_tmdb_watchlist(watchlist_movies, tmdb_user, 'movie'))

        # Watchlist TV
        watchlist_shows = []
        page = 1
        while True:
            resp = tmdb_handler.get_watchlist_shows(account_id, tmdb_session_id, page=page)
            if resp and 'results' in resp:
                watchlist_shows.extend(resp['results'])
                if page >= resp.get('total_pages', 0):
                    break
                page += 1
            else:
                break

        if watchlist_shows:
            # Check if we already have a watchlist (from movies), if so merge
            found = False
            for idx, (slug, _, _) in enumerate(lists_to_sync_data):
                if slug == 'watchlist':
                     # Merge
                    _, meta, existing_items = lists_to_sync_data[idx]
                    new_items = normalize_tmdb_watchlist(watchlist_shows, tmdb_user, 'show')[2]
                    existing_items.extend(new_items)
                    
                    # Update counts
                    meta['item_count']['shows'] = len(new_items)
                    lists_to_sync_data[idx] = ('watchlist', meta, existing_items)
                    found = True
                    break
            
            if not found:
                 lists_to_sync_data.append(normalize_tmdb_watchlist(watchlist_shows, tmdb_user, 'show'))

        # User Created Lists
        page = 1
        while True:
            resp = tmdb_handler.get_created_lists(account_id, tmdb_session_id, page=page)
            if resp and 'results' in resp:
                for list_info in resp['results']:
                    # Fetch list details to get items
                    list_id = list_info['id']
                    list_details = tmdb_handler.get_list_details(list_id, tmdb_session_id)
                    if list_details and 'items' in list_details:
                         lists_to_sync_data.append(normalize_tmdb_user_list(list_details, tmdb_user))
                
                if page >= resp.get('total_pages', 0):
                    break
                page += 1
            else:
                break

        # 3. Generic Lists (Trending etc)
        for list_def in TMDB_GENERIC_LISTS:
            try:
                slug = list_def['slug']
                
                # Check library status
                is_in_library = False
                list_id = f"tmdb:generic:{slug}"
                with sqlite3.connect(lists_db_path) as lists_conn:
                    cursor = lists_conn.cursor()
                    cursor.execute("SELECT add_to_library FROM lists WHERE list_id = ?", (list_id,))
                    row = cursor.fetchone()
                    if row and row[0] == 1:
                        is_in_library = True

                # Dynamic API call
                method_name = list_def['api_method']
                if not hasattr(tmdb_handler, method_name):
                    log(f"[Orac] TMDB Handler missing method {method_name}", level=LOGERROR)
                    continue
                
                method = getattr(tmdb_handler, method_name)
                
                # Always fetch at least one page to get counts/metadata
                page_1 = method(page=1)
                
                if not page_1 or 'results' not in page_1:
                    continue
                
                # If NOT in library, just update the list metadata (count, name) locally and STOP
                if not is_in_library:
                    # Calculate counts based on first page or total estimates
                    # Generic lists usually mix types, but our config says 'type' (e.g. movie/show)
                    # We'll trust the config 'type' for the main count usually
                    total_results = page_1.get('total_results', len(page_1['results']))
                    item_count_movies = total_results if list_def['type'] == 'movie' else 0
                    item_count_shows = total_results if list_def['type'] == 'show' else 0
                    
                    list_meta = {
                        "ids": {"slug": slug},
                        "name": list_def['name'],
                        "description": list_def['description'],
                        "item_count": {"movies": item_count_movies, "shows": item_count_shows},
                        "user": {"ids": {"slug": "tmdb"}},
                        "owned_by_user": False,
                        "source": "tmdb",
                        "list_id": list_id
                    }
                    
                    # Update Lists DB directly
                    update_lists_table_metadata(lists_db_path, list_meta)
                    # Helper cleanup: Remove items from this list to keep DB clean
                    cleanup_list_items(lists_db_path, list_id)
                    log(f"[Orac] Generic list '{slug}' not in library. Updated metadata only.", level=LOGDEBUG)
                    continue

                # If IN library, fetch more pages to sync content
                all_items = []
                all_items.extend(page_1['results'])
                
                for page in range(2, 4): # Fetch 2 more pages
                    resp = method(page=page)
                    if resp and 'results' in resp:
                         all_items.extend(resp['results'])
                    else:
                        break
                
                if all_items:
                    lists_to_sync_data.append(normalize_tmdb_generic_list(all_items, list_def))

            except Exception as e:
                log(f"[Orac] Error processing TMDB generic list {list_def['slug']}: {e}", level=LOGERROR)

        # 4. Process Lists (Resolve IDs and Sync)
        for slug, list_meta, list_items in lists_to_sync_data:
            # Pre-resolve Trakt IDs
            resolved_items = []
            
            # Batch resolving could be optimized, but ensuring accuracy first
            for item in list_items:
                tmdb_id = item[item['type']]['ids']['tmdb']
                media_type = item['type']
                
                # Try to find trakt_id
                trakt_id = None
                with sqlite3.connect(movie_static_db_path if media_type == 'movie' else tvshows_static_db_path) as conn:
                     trakt_id = await resolve_tmdb_to_trakt(tmdb_id, media_type, trakt_auth, conn.cursor())
                
                if not trakt_id:
                    # Use placeholder
                    trakt_id = -int(tmdb_id)
                
                item[media_type]['ids']['trakt'] = trakt_id
                resolved_items.append(item)
            
            # Sync
            log(f"[Orac] **SYNC** TMDB List {slug}...", level=LOGINFO)
            await run_list_sync(
                lists_db_path,
                tmdb_user, # User is 'tmdb' conceptually but we use the actual username for the slug context
                resolved_items,
                slug,
                movie_static_db_path,
                movie_dynamic_db_path,
                tvshows_static_db_path,
                tvshows_dynamic_db_path,
                trakt_queue_path,
                trakt_auth,
                tmdb_handler,
                list_meta
            )

    except Exception as e:
        log(f"[Orac] Error in TMDB list sync task: {e}", level=LOGERROR)

def normalize_tmdb_watchlist(items, username, item_type):
    # Watchlist is a system list. 
    # Calling it 'watchlist' might conflict with Trakt watchlist if we aren't careful with USER in DB.
    # The 'run_list_sync' uses 'user' to build the list_id.
    # So Trakt watchlist is "trakt_user:watchlist", TMDB is "tmdb_user:watchlist".
    
    slug = "watchlist"
    normalized_items = []
    
    for item in items:
        # TMDB items have 'id', 'title'/'name', 'release_date'/'first_air_date'
        tmdb_id = item['id']
        title = item.get('title') or item.get('name')
        year = (item.get('release_date') or item.get('first_air_date') or '')[:4]
        
        normalized_items.append({
            "type": item_type,
            item_type: {
                "ids": {"tmdb": tmdb_id}, # Trakt ID added later
                "title": title,
                "year": int(year) if year.isdigit() else 0
            }
        })

    item_count = {"movies": 0, "shows": 0}
    if item_type == 'movie': item_count['movies'] = len(normalized_items)
    else: item_count['shows'] = len(normalized_items)

    list_meta = {
        "ids": {"slug": slug},
        "name": "TMDB Watchlist",
        "description": "Your TMDB Watchlist",
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "item_count": item_count,
        "user": {"ids": {"slug": username}},
        "owned_by_user": True,
        "source": "tmdb", # Important for UI filtering
        "list_id": "tmdb:official:watchlist"
    }
    
    return slug, list_meta, normalized_items

def normalize_tmdb_user_list(list_details, username):
    slug = list_details['name'].lower().replace(' ', '-') # Simple slug
    # Use ID to ensure uniqueness if names collide? TMDB has list IDs (numeric or string)
    # Using list_details['id'] as slug might be safer but less readable. 
    # Let's use name for display, but maybe id for slug if we can.
    slug = str(list_details['id']) 
    
    name = list_details['name']
    description = list_details.get('description', '')
    
    normalized_items = []
    item_count = {"movies": 0, "shows": 0}

    for item in list_details.get('items', []):
        media_type = item.get('media_type')
        if media_type not in ['movie', 'tv']: continue
        
        normalized_type = 'movie' if media_type == 'movie' else 'show'
        tmdb_id = item['id']
        title = item.get('title') or item.get('name')
        year = (item.get('release_date') or item.get('first_air_date') or '')[:4]
        
        normalized_items.append({
            "type": normalized_type,
            normalized_type: {
                "ids": {"tmdb": tmdb_id},
                "title": title,
                "year": int(year) if year.isdigit() else 0
            }
        })
        
        if normalized_type == 'movie': item_count['movies'] += 1
        else: item_count['shows'] += 1

    list_meta = {
        "ids": {"slug": slug},
        "name": name,
        "description": description,
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"), 
        "item_count": item_count,
        "user": {"ids": {"slug": username}},
        "owned_by_user": True,
        "source": "tmdb",
        "list_id": f"tmdb:personal:{slug}"
    }

    return slug, list_meta, normalized_items

def normalize_tmdb_generic_list(items, list_def):
    slug = list_def['slug']
    name = list_def['name']
    description = list_def['description']
    item_type = list_def['type'] # 'movie' or 'show' (mapped to 'movie'/'show' type string used in logic)
    
    normalized_items = []
    
    count_key = 'movies' if item_type == 'movie' else 'shows'
    item_count = {"movies": 0, "shows": 0}

    for item in items:
        tmdb_id = item['id']
        title = item.get('title') or item.get('name')
        year = (item.get('release_date') or item.get('first_air_date') or '')[:4]
        
        normalized_items.append({
            "type": item_type,
            item_type: {
                "ids": {"tmdb": tmdb_id},
                "title": title,
                "year": int(year) if year.isdigit() else 0
            }
        })
    
    item_count[count_key] = len(normalized_items)

    list_meta = {
        "ids": {"slug": slug},
        "name": name,
        "description": description,
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "item_count": item_count,
        "user": {"ids": {"slug": "tmdb"}}, # Generic lists owned by 'tmdb' system user
        "owned_by_user": False,
        "source": "tmdb",
        "list_id": f"tmdb:generic:{slug}"
    }
    
    return slug, list_meta, normalized_items

def update_lists_table_metadata(db_path, list_meta):
    try:
        slug = list_meta['ids'].get('slug')
        username = list_meta['user']['ids'].get('slug')
        
        if 'list_id' in list_meta:
            list_id = list_meta['list_id']
        else:
            list_id = f"{username}:{slug}"
        name = list_meta['name']
        description = list_meta.get('description', '')
        item_count_movies = list_meta['item_count'].get('movies', 0)
        item_count_shows = list_meta['item_count'].get('shows', 0)
        source = list_meta.get('source', 'tmdb')
        
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO lists (list_id, source, user, slug, name, description, last_checked, item_count_movies, item_count_shows, owned_by_user)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(list_id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    last_checked=excluded.last_checked,
                    item_count_movies=excluded.item_count_movies,
                    item_count_shows=excluded.item_count_shows
            """, (
                list_id, source, username, slug, name, description,
                datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                item_count_movies, item_count_shows
            ))
            conn.commit()
    except Exception as e:
        log(f"[Orac] Error updating list metadata for {list_meta['ids']['slug']}: {e}", level=LOGERROR)

def cleanup_list_items(db_path, list_id):
    try:
        with sqlite3.connect(db_path) as conn:
             cursor = conn.cursor()
             # Just remove the links. We rely on general GC to remove orphaned movies/shows if needed.
             cursor.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
             conn.commit()
    except Exception as e:
        log(f"[Orac] Error cleaning up list items for {list_id}: {e}", level=LOGERROR)
