import sqlite3
from resources.lib.db_utils import db_connect
from resources.lib.log_utils import log, LOGDEBUG, LOGERROR, LOGINFO

def get_all_lists(db_path, ext_indexes_db_path=None, exclude_empty=False):
    list_name = None  # Not used in current implementation
    item_type = 'all'
    my_lists = get_my_lists(db_path, list_name, item_type, ext_indexes_db_path, exclude_empty=exclude_empty)
    generic_lists = get_generic_lists(db_path, list_name, item_type, ext_indexes_db_path)
    all_lists = my_lists + generic_lists
    log(f"[Orac] Total lists found: {len(all_lists)}", level=LOGDEBUG)
    return all_lists

def get_my_lists(db_path, list_name, item_type, ext_indexes_db_path=None, exclude_empty=False):
    formatted_lists = []

    conn = db_connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Base condition: owned_by_user = 1 OR user NOT IN ('trakt', 'tmdb', 'simkl', 'punchplay') OR it's an external index added to library
    base_condition = "(owned_by_user = 1 OR user NOT IN ('trakt', 'tmdb', 'simkl', 'punchplay') OR (list_id LIKE 'tmdb:index:%' AND add_to_library = 1))"

    
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
            'list_id': row['list_id'],
            'name': row['name'],
            'user': user_display,
            'owner': row['user'],
            'source': row['source'], 
            'slug': row['slug'],
            'item_count': row['item_count'],
            'add_to_library': row['add_to_library']
        })

    conn.close()

    if ext_indexes_db_path:
        try:
            with db_connect(ext_indexes_db_path) as ext_conn:
                ext_conn.row_factory = sqlite3.Row
                ext_cursor = ext_conn.cursor()
                if exclude_empty:
                    ext_cursor.execute("SELECT id, media_type, parameters, add_to_library FROM external_indexes WHERE add_to_library = 1")
                else:
                    ext_cursor.execute("SELECT id, media_type, parameters, add_to_library FROM external_indexes")
                ext_rows = ext_cursor.fetchall()
                
                existing_keys = {(l.get('user'), l['slug']) for l in formatted_lists}
                
                for ext_row in ext_rows:
                    m_type = ext_row['media_type']
                    if item_type == 'movie' and m_type != 'movie':
                        continue
                    if item_type == 'tvshow' and m_type not in ['show', 'tvshow', 'tv']:
                        continue
                        
                    slug = ext_row['id'].lower().replace(' ', '-').replace('_', '-')
                    add_lib = 1 if ext_row['add_to_library'] else 0
                    
                    if ('External Index', slug) not in existing_keys:
                        formatted_lists.append({
                            'list_id': f"tmdb:index:{slug}",
                            'name': ext_row['id'],
                            'user': 'External Index',
                            'owner': 'tmdb',
                            'source': 'tmdb',
                            'slug': slug,
                            'item_count': 0,
                            'add_to_library': add_lib
                        })
                        existing_keys.add(('External Index', slug))
        except Exception as e:
            log(f"[Orac] Error reading external indexes into my lists: {e}", level=LOGERROR)

    log(f"[Orac] Found {len(formatted_lists)} my lists", level=LOGDEBUG)
    return formatted_lists

def get_generic_lists(db_path, list_name, item_type, ext_indexes_db_path=None):
    formatted_lists = []
    log(f"[Orac] Fetching generic lists of type: {item_type}", level=LOGDEBUG)
    conn = db_connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    not_ext_index_cond = "(list_id IS NULL OR list_id NOT LIKE 'tmdb:index:%')"

    if item_type == 'movie':
        cursor.execute(f"SELECT list_id, name, user, source, slug, add_to_library, item_count_movies AS item_count FROM lists WHERE item_count_movies > 0 AND user IN ('trakt', 'tmdb', 'simkl', 'punchplay') AND {not_ext_index_cond}")
    elif item_type == 'tvshow':
        cursor.execute(f"SELECT list_id, name, user, source, slug, add_to_library, item_count_shows AS item_count FROM lists WHERE item_count_shows > 0 AND user IN ('trakt', 'tmdb', 'simkl', 'punchplay') AND {not_ext_index_cond}")
    elif item_type == 'all':
        cursor.execute(f"SELECT list_id, name, user, source, slug, add_to_library, item_count_movies + item_count_shows AS item_count FROM lists WHERE user IN ('trakt', 'tmdb', 'simkl', 'punchplay') AND {not_ext_index_cond}")
    else:
        cursor.execute(f"SELECT list_id, name, user, source, slug, add_to_library, item_count_movies + item_count_shows AS item_count FROM lists WHERE item_count_movies + item_count_shows > 0 AND user IN ('trakt', 'tmdb', 'simkl', 'punchplay') AND {not_ext_index_cond}")

    rows = cursor.fetchall()

    for row in rows:
        formatted_lists.append({
            'name': row['name'],
            'user': row['user'],
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

    norm_type = 'movie' if str(item_type).lower() == 'movie' else 'tvshow'
    db_media_type = 'movie' if norm_type == 'movie' else 'show'
    tmdb_id_str = str(tmdb_id)

    trakt_id = None
    # Step 1: Get the trakt_id from the tmdb_id if present in static DB
    try:
        if norm_type == 'movie':
            with db_connect(movies_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT trakt_id FROM movies WHERE tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                if row:
                    trakt_id = row[0]
        else:
            with db_connect(tvshows_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT show_trakt_id FROM shows WHERE show_tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                if row:
                    trakt_id = row[0]
    except Exception as e:
        log(f"[Orac] Error getting trakt_id for tmdb_id {tmdb_id}: {e}", level=LOGERROR)

    trakt_id_str = str(trakt_id) if trakt_id else '0'

    # Step 2: Get all personal lists that do NOT contain this item (matched by tmdb_id or trakt_id)
    formatted_lists = []
    try:
        with db_connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT list_id, name, user, slug, source, item_count_movies + item_count_shows AS item_count
                FROM lists
                WHERE owned_by_user = true AND list_id NOT IN (
                    SELECT list_id FROM list_items
                    WHERE media_type = ? AND (tmdb_id = ? OR (? != '0' AND trakt_id = ?))
                )
            """, (db_media_type, tmdb_id_str, trakt_id_str, trakt_id_str))

            rows = cursor.fetchall()
            for row in rows:
                d = dict(row)
                slug_val = (d.get('slug') or '').lower()
                if norm_type == 'movie' and slug_val == 'collection-tvshows':
                    continue
                if norm_type == 'tvshow' and slug_val == 'collection-movies':
                    continue
                formatted_lists.append(d)
    except Exception as e:
        log(f"[Orac] Error getting add options: {e}", level=LOGERROR)
        return []

    # For trakt lists, check number of items as free Trakt custom lists have a 100-item limit
    valid_trakt_lists = []
    if formatted_lists:
        for lst in formatted_lists:
            if lst['source'] == 'trakt' and lst.get('slug') not in ('watchlist', 'favorites', 'collection-movies', 'collection-tvshows'):
                if lst['item_count'] < 100:
                    valid_trakt_lists.append(lst)
            else:
                valid_trakt_lists.append(lst)

    formatted_lists = valid_trakt_lists

    log(f"[Orac] Found {len(formatted_lists)} lists to add item (tmdb={tmdb_id_str}, trakt={trakt_id_str}) to.", level=LOGDEBUG)
    return formatted_lists

def get_remove_options(db_path, item_type, tmdb_id, movies_static_db_path, tvshows_static_db_path, username=None, tmdb_handler=None):
    """
    Get lists that a given item can be removed from.
    This includes all personal lists that already contain the item.
    """
    if not tmdb_id:
        log("[Orac] get_remove_options called without a tmdb_id.", level=LOGERROR)
        return []

    norm_type = 'movie' if str(item_type).lower() == 'movie' else 'tvshow'
    db_media_type = 'movie' if norm_type == 'movie' else 'show'
    tmdb_id_str = str(tmdb_id)

    trakt_id = None
    # Step 1: Get the trakt_id from the tmdb_id if present in static DB
    try:
        if norm_type == 'movie':
            with db_connect(movies_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT trakt_id FROM movies WHERE tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                if row:
                    trakt_id = row[0]
        else:
            with db_connect(tvshows_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT show_trakt_id FROM shows WHERE show_tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                if row:
                    trakt_id = row[0]
    except Exception as e:
        log(f"[Orac] Error getting trakt_id for tmdb_id {tmdb_id}: {e}", level=LOGERROR)

    trakt_id_str = str(trakt_id) if trakt_id else '0'

    # Step 2: Get all personal lists that contain this item (matched by tmdb_id or trakt_id)
    formatted_lists = []
    try:
        with db_connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT list_id, name, user, slug, source, item_count_movies + item_count_shows AS item_count
                FROM lists
                WHERE owned_by_user = true AND list_id IN (
                    SELECT list_id FROM list_items
                    WHERE media_type = ? AND (tmdb_id = ? OR (? != '0' AND trakt_id = ?))
                )
            """, (db_media_type, tmdb_id_str, trakt_id_str, trakt_id_str))

            rows = cursor.fetchall()
            for row in rows:
                formatted_lists.append(dict(row))
    except Exception as e:
        log(f"[Orac] Error getting remove options: {e}", level=LOGERROR)
        return []
    log(f"[Orac] Found {len(formatted_lists)} lists to remove item (tmdb={tmdb_id_str}, trakt={trakt_id_str}) from.", level=LOGDEBUG)
    return formatted_lists

def update_list_library_status(params, db_path, ext_indexes_db_path=None):
    user = params.get('user')
    slug = params.get('slug')
    update = params.get('update')
    if not slug or not user or update is None:
        log("[Orac] Missing parameters for update_list_library_status.", level=LOGERROR)
        return False
    
    add_to_library = 1 if update == 'Add' else 0

    if ext_indexes_db_path:
        try:
            with db_connect(ext_indexes_db_path) as ext_conn:
                ext_cursor = ext_conn.cursor()
                ext_cursor.execute("UPDATE external_indexes SET add_to_library = ? WHERE LOWER(REPLACE(REPLACE(id, ' ', '-'), '_', '-')) = ?", (add_to_library, slug.lower()))
                ext_conn.commit()
        except Exception as e:
            log(f"[Orac] Error updating add_to_library in external_indexes: {e}", level=LOGERROR)
    
    # Always resolve list_id from DB because we moved to a Source:Type:Identifier schema
    # and we can't reconstruct it reliably from just user + slug.
    list_id = params.get('list_id')
    list_name = params.get('list_name')
    
    if not list_id:
        try:
            with db_connect(db_path) as conn:
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

    params['resolved_list_id'] = list_id

    try:
        with db_connect(db_path) as conn:
            cursor = conn.cursor()
            if add_to_library == 1:
                cursor.execute(
                    "UPDATE lists SET add_to_library = ?, last_checked = '1970-01-01T00:00:00.000Z' WHERE list_id = ?",
                    (add_to_library, list_id)
                )
            else:
                cursor.execute("UPDATE lists SET add_to_library = ? WHERE list_id = ?", (add_to_library, list_id))
                cursor.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
            conn.commit()
        log(f"[Orac] Updated add_to_library status for list_id {list_id} to {add_to_library}", level=LOGINFO)
    except Exception as e:
        log(f"[Orac] Error updating add_to_library status for list_id {list_id}: {e}", level=LOGERROR)
        return False
    return True

def delete_list_locally(list_id, db_path):
    """Removes a list and all its items from the local database."""
    try:
        with db_connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
            cursor.execute("DELETE FROM lists WHERE list_id = ?", (list_id,))
            conn.commit()
        log(f"[Orac] Successfully deleted list {list_id} and its items locally", level=LOGDEBUG)
        return True
    except Exception as e:
        log(f"[Orac] Error deleting list {list_id} locally: {e}", level=LOGERROR)
        return False
