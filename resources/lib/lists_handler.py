import sqlite3
from resources.lib.log_utils import log, LOGDEBUG, LOGERROR, LOGINFO

def get_all_lists(db_path, ext_indexes_db_path=None, exclude_empty=False):
    list_name = None  # Not used in current implementation
    item_type = 'all'
    my_lists = get_my_lists(db_path, list_name, item_type, ext_indexes_db_path, exclude_empty=exclude_empty)
    generic_lists = get_generic_lists(db_path, list_name, item_type)
    all_lists = my_lists + generic_lists
    log(f"[Orac] Total lists found: {len(all_lists)}", level=LOGDEBUG)
    return all_lists

def get_my_lists(db_path, list_name, item_type, ext_indexes_db_path=None, exclude_empty=False):
    formatted_lists = []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Base condition: user NOT IN ('trakt', 'tmdb', 'flixpatrol') OR it's an external index added to library
    base_condition = "(user NOT IN ('trakt', 'tmdb', 'flixpatrol') OR (list_id LIKE 'tmdb:index:%' AND add_to_library = 1))"
    
    # If exclude_empty is True, we only require item_count > 0.
    # If exclude_empty is False (default), we include lists owned by user even if empty.
    
    if item_type == 'movie':
        count_check = "item_count_movies > 0" if exclude_empty else "(item_count_movies > 0 OR owned_by_user = 1)"
        query = f"SELECT list_id, name, user, source, slug, add_to_library, item_count_movies AS item_count FROM lists WHERE {count_check} AND {base_condition}"
        cursor.execute(query)
    elif item_type == 'tvshow':
        count_check = "item_count_shows > 0" if exclude_empty else "(item_count_shows > 0 OR owned_by_user = 1)"
        query = f"SELECT list_id, name, user, source, slug, add_to_library, item_count_shows AS item_count FROM lists WHERE {count_check} AND {base_condition}"
        cursor.execute(query)
    elif item_type == 'all':
        # logic for 'all' typically doesn't check counts strictly unless we want to hide empty ones entirely?
        # Existing logic: WHERE user NOT IN ... (no count check at all)
        # If excluding empty, maybe check combined count?
        if exclude_empty:
             query = f"SELECT list_id, name, user, source, slug, add_to_library, item_count_movies + item_count_shows AS item_count FROM lists WHERE (item_count_movies + item_count_shows > 0) AND {base_condition}"
        else:
             query = f"SELECT list_id, name, user, source, slug, add_to_library, item_count_movies + item_count_shows AS item_count FROM lists WHERE {base_condition}"
        cursor.execute(query)
    else:
         # Default/Catch-all
        count_check = "(item_count_movies + item_count_shows > 0)" if exclude_empty else "(item_count_movies + item_count_shows > 0 OR owned_by_user = 1)"
        query = f"SELECT list_id, name, user, source, slug, add_to_library, item_count_movies + item_count_shows AS item_count FROM lists WHERE {count_check} AND {base_condition}"
        cursor.execute(query)
    rows = cursor.fetchall()

    for row in rows:
        user_display = row['user']
        if row['list_id'] and row['list_id'].startswith('tmdb:index:'):
            user_display = 'External Index'
            
        formatted_lists.append({
            'name': row['name'],
            'user': user_display,
            'owner': row['user'],
            'source': row['source'], 
            'slug': row['slug'],
            'item_count': row['item_count'],
            'add_to_library': row['add_to_library']
        })

    conn.close()


    log(f"[Orac] Found {len(formatted_lists)} my lists", level=LOGDEBUG)
    return formatted_lists

def get_generic_lists(db_path, list_name, item_type):
    formatted_lists = []
    log(f"[Orac] Fetching generic lists of type: {item_type}", level=LOGDEBUG)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if item_type == 'movie':
        cursor.execute("SELECT list_id, name, user, source, slug, add_to_library, item_count_movies AS item_count FROM lists WHERE item_count_movies > 0 AND user IN ('trakt', 'tmdb', 'flixpatrol')")
    elif item_type == 'tvshow':
        cursor.execute("SELECT list_id, name, user, source, slug, add_to_library, item_count_shows AS item_count FROM lists WHERE item_count_shows > 0 AND user IN ('trakt', 'tmdb', 'flixpatrol')")
    elif item_type == 'all':
        cursor.execute("SELECT list_id, name, user, source, slug, add_to_library, item_count_movies + item_count_shows AS item_count FROM lists WHERE user IN ('trakt', 'tmdb', 'flixpatrol')")
    else:
        cursor.execute("SELECT list_id, name, user, source, slug, add_to_library, item_count_movies + item_count_shows AS item_count FROM lists WHERE item_count_movies + item_count_shows > 0 AND user IN ('trakt', 'tmdb', 'flixpatrol')")
    rows = cursor.fetchall()

    for row in rows:
        user_display = row['user']
        if row['list_id'] and row['list_id'].startswith('tmdb:index:'):
            user_display = 'External Index'
            
        formatted_lists.append({
            'name': row['name'],
            'user': user_display,
            'owner': row['user'],
            'source': row['source'], 
            'slug': row['slug'],
            'item_count': row['item_count'],
            'add_to_library': row['add_to_library']
        })
    
    conn.close()
    log(f"[Orac] Found {len(formatted_lists)} generic lists", level=LOGDEBUG)
    return formatted_lists

def get_add_options(db_path, item_type, tmdb_id, movies_static_db_path, tvshows_static_db_path, username=None, tmdb_handler=None):
    """
    Get lists that a given item can be added to.
    This includes all personal lists that do not already contain the item.
    """
    if not tmdb_id:
        log("[Orac] get_add_options called without a tmdb_id.", level=LOGERROR)
        return []

    if not username:
        log("[Orac] get_add_options called without a username.", level=LOGERROR)
        return []

    trakt_id = None
    # Step 1: Get the trakt_id from the tmdb_id
    try:
        if item_type == 'movie':
            with sqlite3.connect(movies_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT trakt_id FROM movies WHERE tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                if row:
                    trakt_id = row[0]
        elif item_type == 'tvshow':
            with sqlite3.connect(tvshows_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT show_trakt_id FROM shows WHERE show_tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                if row:
                    trakt_id = row[0]
    except Exception as e:
        log(f"[Orac] Error getting trakt_id for tmdb_id {tmdb_id}: {e}", level=LOGERROR)
        return []

    if not trakt_id and tmdb_handler:
        log(f"[Orac] trakt_id not found locally for tmdb_id {tmdb_id}. Attempting fetch via TMDB handler.", level=LOGINFO)
        try:
             # Fetch external IDs from TMDB
             external_ids = tmdb_handler.get_external_ids(tmdb_id, item_type)
             if external_ids and 'imdb_id' in external_ids: # TMDB handler usually returns dict with external ids
                 # Wait, we need trakt_id, but TMDB API returns imdb_id, tvdb_id etc. 
                 # Trakt ID is NOT usually returned by TMDB API. 
                 # We generally rely on the fact that we can search Trakt by TMDB ID/IMDB ID, 
                 # OR we just need to ensure the item EXISTS in our system.
                 # Actually, list_items table uses trakt_id.
                 # So we need to resolve TMDB ID -> Trakt ID. 
                 # The tmdb_handler interacts with TMDB API. TMDB does not know Trakt IDs.
                 # We need a Trakt lookup.
                 
                 # Let's check if we can get it from correct source.
                 # If we are using Trakt lists, we really need the Trakt ID.
                 # But if we don't have it, we can't add it to a Trakt list easily without querying Trakt.
                 # Does `tmdb_handler` have a way to get Trakt ID? No.
                 # We need `trakt_handler`.
                 pass
        except Exception as e:
            log(f"[Orac] Error fetching external IDs: {e}", level=LOGERROR)

    # RE-EVALUATION: TMDB Handler cannot give us Trakt ID. We need Trakt Handler for that.
    # However, the user request and logs suggest we are missing `trakt_id`.
    # Code uses `trakt_id` to Query `list_items`.
    # If we can't find `trakt_id` locally, we can't check if it's in the list.
    # BUT, if it's not in the local DB, it's definitely NOT in any local list_items checks (unless logic is flawed).
    # So if trakt_id is missing, it's effectively "not in any list".
    
    # ISSUE: We need `trakt_id` to return it? No, the return value is a list of LISTS.
    # The query is `SELECT ... WHERE list_id NOT IN (SELECT list_id FROM list_items WHERE trakt_id = ?)`
    
    # If `trakt_id` is None, the `NOT IN` clause effectively works on "None", which matches nothing.
    # So `list_id NOT IN (empty set)` is TRUE for all lists.
    # So if we assume `trakt_id` is None (or dummy) for a new item, we effectively say "This item is in NO lists".
    # This is correct! It's a new item.
    
    # However, do we need the `trakt_id` for anything else?
    # The current code returns `[]` if `trakt_id` is found.
    # We should just proceed with `trakt_id = 0` (or similar) if not found, 
    # effectively assuming it's not in any list.
    
    # WAIT! When we actually perform `add_to_list`, we WILL need the ID.
    # But `get_add_options` just returns the UI choices.
    
    # Let's change logic: If `trakt_id` is missing, assume it's not in any list, and return ALL owned lists.
    
    if not trakt_id:
        log(f"[Orac] trakt_id not found locally for tmdb_id {tmdb_id}. Assuming item is not in any list.", level=LOGINFO)
        # Use a dummy ID that won't match any real trakt_id in list_items
        trakt_id = 0 

    # Step 2: Get all personal lists that do NOT contain this trakt_id
    formatted_lists = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT name, user, slug, source, item_count_movies + item_count_shows AS item_count
                FROM lists
                WHERE owned_by_user = true AND list_id NOT IN (
                    SELECT list_id FROM list_items WHERE trakt_id = ?
                )
            """, (str(trakt_id),))

            rows = cursor.fetchall()
            for row in rows:
                formatted_lists.append(dict(row))
    except Exception as e:
        log(f"[Orac] Error getting add options: {e}", level=LOGERROR)
        return []

# For trakt lists, we need to check the user status and number of items as you can only have 100 items in a trakt list.
    valid_trakt_lists = []
    if formatted_lists:
        for lst in formatted_lists:
            if lst['source'] == 'trakt':
                if lst['item_count'] < 100:
                    valid_trakt_lists.append(lst)
            else:
                valid_trakt_lists.append(lst)

    formatted_lists = valid_trakt_lists


    log(f"[Orac] Found {len(formatted_lists)} lists to add item {trakt_id} to.", level=LOGDEBUG)
    return formatted_lists

def get_remove_options(db_path, item_type, tmdb_id, movies_static_db_path, tvshows_static_db_path, username=None, tmdb_handler=None):
    """
    Get lists that a given item can be removed from.
    This includes all personal lists that already contain the item.
    """
    if not tmdb_id:
        log("[Orac] get_remove_options called without a tmdb_id.", level=LOGERROR)
        return []

    if not username:
        log("[Orac] get_remove_options called without a username.", level=LOGERROR)
        return []

    trakt_id = None
    # Step 1: Get the trakt_id from the tmdb_id
    try:
        if item_type == 'movie':
            with sqlite3.connect(movies_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT trakt_id FROM movies WHERE tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                if row:
                    trakt_id = row[0]
        elif item_type == 'tvshow':
            with sqlite3.connect(tvshows_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT show_trakt_id FROM shows WHERE show_tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                if row:
                    trakt_id = row[0]
    except Exception as e:
        log(f"[Orac] Error getting trakt_id for tmdb_id {tmdb_id}: {e}", level=LOGERROR)
        return []

    if not trakt_id:
        log(f"[Orac] Could not find trakt_id for tmdb_id {tmdb_id} of type {item_type}. Assuming not in any list.", level=LOGINFO)
        # If we can't find the ID, we can't be in any list (locally known).
        # So return empty list.
        return []

    # Step 2: Get all personal lists that contain this trakt_id
    formatted_lists = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT name, user, slug, source, item_count_movies + item_count_shows AS item_count
                FROM lists
                WHERE owned_by_user = true AND list_id IN (
                    SELECT list_id FROM list_items WHERE trakt_id = ?
                )
            """, (str(trakt_id),))

            rows = cursor.fetchall()
            for row in rows:
                formatted_lists.append(dict(row))
    except Exception as e:
        log(f"[Orac] Error getting remove options: {e}", level=LOGERROR)
        return []
    log(f"[Orac] Found {len(formatted_lists)} lists to remove item {trakt_id} from.", level=LOGDEBUG)
    return formatted_lists

def update_list_library_status(params, db_path):
    user = params.get('user')
    slug = params.get('slug')
    update = params.get('update')
    if not slug or not user or update is None:
        log("[Orac] Missing parameters for update_list_library_status.", level=LOGERROR)
        return False
    
    # Always resolve list_id from DB because we moved to a Source:Type:Identifier schema
    # and we can't reconstruct it reliably from just user + slug.
    list_id = None
    list_name = params.get('list_name')
    
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            
            # 1. Try exact slug match
            cursor.execute("SELECT list_id, name FROM lists WHERE slug = ?", (slug,))
            rows = cursor.fetchall()
            
            if len(rows) == 1:
                list_id = rows[0][0]
            elif len(rows) > 1:
                # Collision (e.g. Watchlist). Disambiguate by name if possible.
                if list_name:
                    for r_id, r_name in rows:
                        if r_name == list_name:
                            list_id = r_id
                            break
                    # If no name match, default to first (Trakt usually)
                    if not list_id:
                        log(f"[Orac] Ambiguous slug '{slug}' and no name match for '{list_name}'. Defaulting to {rows[0][0]}", level=LOGWARNING)
                        list_id = rows[0][0]
                else:
                    list_id = rows[0][0]
            
            # 2. Try calculated slug (for external indexes or rough matches) if no match yet
            if not list_id:
                simple_slug = slug.lower().replace(' ', '-')
                cursor.execute("SELECT list_id FROM lists WHERE slug = ?", (simple_slug,))
                row = cursor.fetchone()
                if row:
                    list_id = row[0]
            
            # 3. Try name match (case-insensitive)
            if not list_id:
                cursor.execute("SELECT list_id FROM lists WHERE name = ? COLLATE NOCASE", (slug,))
                row = cursor.fetchone()
                if row:
                    list_id = row[0]

    except Exception as e:
        log(f"[Orac] Error resolving list_id for slug '{slug}': {e}", level=LOGERROR)

    if list_id:
        log(f"[Orac] Resolved list '{slug}' to list_id: {list_id}", level=LOGDEBUG)
    else:
        log(f"[Orac] Could not resolve list '{slug}' to a local list_id. Falling back to legacy construction.", level=LOGWARNING)
        list_id = f"{user}:{slug}"
    add_to_library = 1 if update == 'Add' else 0

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            
            # Check if this is a personal list (owned_by_user = true)
            cursor.execute("SELECT owned_by_user FROM lists WHERE list_id = ?", (list_id,))
            row = cursor.fetchone()
            
            if row and row[0] == 1:  # owned_by_user = true
                if add_to_library == 0:
                    log(f"[Orac] Cannot remove personal list '{list_id}' from library. Personal lists must always be in library.", level=LOGWARNING)
                    return False
            
            cursor.execute("UPDATE lists SET add_to_library = ? WHERE list_id = ?", (add_to_library, list_id))
            conn.commit()
        log(f"[Orac] Updated add_to_library status for list_id {list_id} to {add_to_library}", level=LOGDEBUG)
    except Exception as e:
        log(f"[Orac] Error updating add_to_library status for list_id {list_id}: {e}", level=LOGERROR)
        return False
    return True

def delete_list_locally(list_id, db_path):
    """Removes a list and all its items from the local database."""
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
            cursor.execute("DELETE FROM lists WHERE list_id = ?", (list_id,))
            conn.commit()
        log(f"[Orac] Successfully deleted list {list_id} and its items locally", level=LOGDEBUG)
        return True
    except Exception as e:
        log(f"[Orac] Error deleting list {list_id} locally: {e}", level=LOGERROR)
        return False
