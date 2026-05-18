import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from resources.lib.trakt_utils import get_trakt_watchlist
from datetime import datetime
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING
from resources.lib.db_utils import add_tvshow, insert_tv_season
import json
#from resources.lib.tmdb_utils import update_show_images
import sqlite3
import threading
import time

# Database and threading setup
lock = threading.Lock()

def _add_movie_in_thread(movie_item, movies_static_db_path, movies_dynamic_db_path, tmdb_handler):
    """
    Worker function to be executed in a thread for adding a single movie.
    Opens its own database connections for thread safety.
    """
    static_conn_thread = None
    dynamic_conn_thread = None
    try:
        media_id = {"trakt": movie_item["ids"]["trakt"], "tmdb": movie_item["ids"].get("tmdb")}

        static_conn_thread = sqlite3.connect(movies_static_db_path, timeout=10)
        static_cursor_thread = static_conn_thread.cursor()
        
        dynamic_conn_thread = sqlite3.connect(movies_dynamic_db_path, timeout=10)
        dynamic_cursor_thread = dynamic_conn_thread.cursor()

        add_movie(
            static_cursor_thread,
            dynamic_cursor_thread,
            movie_item,
            media_id,
            tmdb_handler
        )
        
        static_conn_thread.commit()
        dynamic_conn_thread.commit()
        return True
    except Exception as e:
        log(f"Error adding movie '{movie_item.get('title', 'Unknown')}' in thread: {e}", level=LOGERROR)
        return False
    finally:
        if static_conn_thread: static_conn_thread.close()
        if dynamic_conn_thread: dynamic_conn_thread.close()

def _add_show_in_thread(show_item, tvshows_static_db_path, trakt_queue_path, trakt_auth, tmdb_handler):
    """
    Worker function to be executed in a thread for adding a single TV show.
    Opens its own database connections for thread safety.
    """
    static_conn_thread = None
    trakt_queue_conn_thread = None
    try:
        media_id = {"trakt": show_item["ids"]["trakt"], "tmdb": show_item["ids"].get("tmdb")}

        static_conn_thread = sqlite3.connect(tvshows_static_db_path, timeout=10)
        static_cursor_thread = static_conn_thread.cursor()
        
        trakt_queue_conn_thread = sqlite3.connect(trakt_queue_path, timeout=10)
        trakt_queue_cursor_thread = trakt_queue_conn_thread.cursor()

        add_tvshow(
            static_cursor_thread,
            None,  # dynamic_cursor is not used in add_tvshow
            trakt_queue_cursor_thread,
            media_id,
            trakt_auth,
            tmdb_handler,
            show_item
        )
        
        static_conn_thread.commit()
        trakt_queue_conn_thread.commit()
        return True
    except Exception as e:
        log(f"Error adding show '{show_item.get('title', 'Unknown')}' in thread: {e}", level=LOGERROR)
        return False
    finally:
        if static_conn_thread: static_conn_thread.close()
        if trakt_queue_conn_thread: trakt_queue_conn_thread.close()


# Get last known update timestamp
def get_local_list_updated_at(db_path, list_id):
    with lock, sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT last_checked FROM lists WHERE list_id=?", (list_id,))
        row = cur.fetchone()
        return row[0] if row else None

# New version
def update_list_in_db(db_path, list_meta, items):
    current_pairs = []
    
    # Use explicit list_id if provided (e.g. for official lists or pre-calculated IDs)
    if 'list_id' in list_meta:
        list_id = list_meta['list_id']
    else:
        # Default for personal lists fetched via Trakt API
        slug = list_meta['ids']['slug']
        list_id = f"trakt:personal:{slug}"

    with lock, sqlite3.connect(db_path) as conn:
        list_cur = conn.cursor()

        # 1. Upsert list metadata.
        # We use a two-step "upsert" to avoid overwriting user-configurable fields
        # like 'add_to_library' which are not present in the Trakt list metadata.

        # Step 1a: Ensure the list exists. INSERT OR IGNORE is perfect for this.
        # It will create a new row with default values if the list_id is new,
        # or do nothing if it already exists. For personal lists (owned_by_user = true),
        # we automatically set add_to_library to 1.
        is_owned = 1 if list_meta.get('owned_by_user', False) else 0
        add_to_library_default = 1 if is_owned else 0
        list_cur.execute("INSERT OR IGNORE INTO lists (list_id, add_to_library) VALUES (?, ?)", (list_id, add_to_library_default))

        # Step 1b: Update the metadata for the list. This approach ensures that
        # any columns not specified here (like 'add_to_library') retain their
        # existing values for lists that are already in the database.
        list_cur.execute("""
            UPDATE lists SET
                source = ?,
                user = ?,
                slug = ?,
                name = ?,
                description = ?,
                last_checked = ?,
                owned_by_user = ?,
                item_count_movies = ?,
                item_count_shows = ?
            WHERE list_id = ?
        """, (
            list_meta.get('source', 'trakt'), list_meta['user']['ids']['slug'], list_meta['ids']['slug'],
            list_meta['name'], list_meta.get('description', ''), datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            1 if list_meta.get('owned_by_user', False) else 0,
            list_meta.get('item_count', {}).get('movies', 0), 
            list_meta.get('item_count', {}).get('shows', 0),
            list_id
        ))

        # 2. Get current DB items for this list
        list_cur.execute("SELECT media_type, trakt_id FROM list_items WHERE list_id=?", (list_id,))
        existing_items = set(list_cur.fetchall())

        # 3. Build set of incoming items
        new_items = set()
        incoming_items_by_key = {}  # We'll use this to map back later
#        log(f"[Orac] Syncing list '{list_meta['name']}' with {len(items)} items", level=LOGINFO)

        for item in items:
            media_type = item['type']
            trakt_id = str(item[media_type]['ids']['trakt'])
            tmdb_id = str(item[media_type]['ids'].get('tmdb', ''))
            key = (media_type, trakt_id)
            new_items.add(key)
            incoming_items_by_key[key] = (item, tmdb_id)  # store the full item and tmdb_id for later use
            current_pairs.append((list_id, trakt_id))

        # 4. Determine differences
        items_to_add = new_items - existing_items
        items_to_remove = existing_items - new_items

        # 5. Insert additions
        for media_type, trakt_id in items_to_add:
            item, tmdb_id = incoming_items_by_key[(media_type, trakt_id)]
            list_cur.execute("""
                INSERT INTO list_items (list_id, media_type, trakt_id, tmdb_id)
                VALUES (?, ?, ?, ?)
            """, (list_id, media_type, trakt_id, tmdb_id))

        # 6. Delete removals
        for media_type, trakt_id in items_to_remove:
            list_cur.execute("""
                DELETE FROM list_items
                WHERE list_id=? AND media_type=? AND trakt_id=?
            """, (list_id, media_type, trakt_id))

        conn.commit()

    # 7. Return media items to update in caches
    media_to_update = [
        incoming_items_by_key[(media_type, trakt_id)][0]  # Extract just the item, not tmdb_id
        for media_type, trakt_id in items_to_add
    ]

    return {
        "list_id": list_id,
        "added_items": len(items_to_add),
        "removed_items": len(items_to_remove),
        "current_pairs": current_pairs,
        "media_to_update": media_to_update
    }



# Remove items not present in any list
def remove_stale_list_items(db_path, valid_pairs):
    valid_list_ids = set(list_id for list_id, _ in valid_pairs)

    with lock, sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT list_id, trakt_id FROM list_items")
        all_current = set(
            row for row in cur.fetchall()
            if row[0] in valid_list_ids  # Only consider current list
        )

        valid_set = set(valid_pairs)
        to_remove = all_current - valid_set

        for list_id, trakt_id in to_remove:
            cur.execute("DELETE FROM list_items WHERE list_id = ? AND trakt_id = ?", (list_id, trakt_id))

        conn.commit()
        return len(to_remove)


# Orchestrate full sync
async def run_list_sync(
    lists_db_path,
    user,
    list_data,
    slug,
    movies_static_db_path,
    movies_dynamic_db_path,
    tvshows_static_db_path,
    tvshows_dynamic_db_path,
    trakt_queue_path,
    trakt_auth,
    tmdb_handler,
    list_meta=None
):
    # Step 1: Build minimal list_meta if not provided
    if list_meta is None:
        movie_count = sum(1 for i in list_data if i["type"] == "movie")
        show_count  = sum(1 for i in list_data if i["type"] == "show")
        
        # Determine list ID for system lists
        if slug in ['watchlist', 'collection-movies', 'collection-tvshows']:
            list_id = f"trakt:personal:{slug}"
        else:
            list_id = f"trakt:personal:{slug}"

        list_meta = {
            "ids": {"slug": slug},
            "name": slug.replace("_", " ").title(),
            "description": f"Trakt {slug.replace('_', ' ').title()}",
            "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "item_count": {
                "movies": movie_count,
                "shows": show_count
            },
            "user": {"ids": {"slug": user}},
            "owned_by_user": True,  # System lists like watchlist/collection are considered "owned"
            "list_id": list_id
        }

    # Step 2: Sync list to DB
    sync_result = update_list_in_db(lists_db_path, list_meta, list_data)

    removed = remove_stale_list_items(lists_db_path, sync_result["current_pairs"])

    # Step 3: Identify missing metadata for items in the list
    all_incoming_items = list_data
    movies_to_check = [i["movie"] for i in all_incoming_items if i["type"] == "movie"]
    shows_to_check = [i["show"] for i in all_incoming_items if i["type"] == "show"]

    movies_to_add = []
    if movies_to_check:
        movie_ids = [m["ids"]["trakt"] for m in movies_to_check]
        placeholders = ",".join(["?"] * len(movie_ids))
        with sqlite3.connect(movies_static_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT trakt_id FROM movies WHERE trakt_id IN ({placeholders})", movie_ids)
            existing_movie_ids = {str(row[0]) for row in cursor.fetchall()}
            movies_to_add = [m for m in movies_to_check if str(m["ids"]["trakt"]) not in existing_movie_ids]

    shows_to_add = []
    if shows_to_check:
        show_ids = [s["ids"]["trakt"] for s in shows_to_check]
        placeholders = ",".join(["?"] * len(show_ids))
        with sqlite3.connect(tvshows_static_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT show_trakt_id FROM shows WHERE show_trakt_id IN ({placeholders})", show_ids)
            existing_show_ids = {str(row[0]) for row in cursor.fetchall()}
            shows_to_add = [s for s in shows_to_check if str(s["ids"]["trakt"]) not in existing_show_ids]

    if movies_to_add or shows_to_add:
        log(f"[Orac] Missing metadata for {len(movies_to_add)} movies and {len(shows_to_add)} shows in list '{slug}'. Adding to worker queue...", level=LOGINFO)

    with ThreadPoolExecutor(max_workers=5) as executor:
        # Submit movie tasks
        movie_futures = [
            executor.submit(
                _add_movie_in_thread,
                movie_item,
                movies_static_db_path,
                movies_dynamic_db_path,
                tmdb_handler
            ) for movie_item in movies_to_add
        ]

        # Submit show tasks
        show_futures = [
            executor.submit(
                _add_show_in_thread,
                show_item,
                tvshows_static_db_path,
                trakt_queue_path,
                trakt_auth,
                tmdb_handler
            ) for show_item in shows_to_add
        ]

        # Wait for all futures to complete and handle exceptions
        for future in as_completed(movie_futures + show_futures):
            try:
                future.result()  # This will re-raise exceptions from threads
            except Exception as e:
                log(f"A media update task failed during list sync: {e}", level=LOGERROR)


    # Step 4: Build result summary
    result = {
        "synced_lists": 1,
        "added_items": sync_result["added_items"],
        "removed_items": sync_result["removed_items"],
        "removed_stale": removed
    }

#    log(f"[Orac] {slug.replace('_', ' ').title()} sync complete: {result}", level=LOGINFO)

 


    return result

def add_movie(movies_static_cursor, movies_dynamic_cursor, movie, media_id, tmdb_handler):

# Build movie metadata

        movie_id = media_id.get("trakt")
        title = movie["title"]
        year = movie.get("year", 0)
        tmdb_id = movie["ids"].get("tmdb")
        imdb_id = movie["ids"].get("imdb")
        
        # If TMDB ID is missing, try to resolve it via IMDB ID
        if not tmdb_id and imdb_id:
             result = tmdb_handler.find_by_external_id(imdb_id, source="imdb_id")
             if result:
                 tmdb_id = result.get("id")
                 log(f"[Orac] Resolved TMDB ID {tmdb_id} for movie '{title}' using IMDB ID {imdb_id}", level=LOGINFO)
        
        # If still missing, we cannot proceed safely
        if not tmdb_id:
            log(f"[Orac] Skipping movie '{title}' (Trakt: {movie_id}) due to missing TMDB ID.", level=LOGWARNING)
            return
            
        tagline = movie.get("tagline", "")
        overview = movie.get("overview", "")
        released = movie.get("released", "")
        runtime = movie.get("runtime", 0)
        country = movie.get("country", "")
        rating = movie.get("rating", None)
        language = movie.get("language", "")
        genres = movie.get("genres", [])
        certification = movie.get("certification", "")
        original_title = movie.get("original_title", "")

        # Insert movie into static database
        # Uses tmdb_id as Primary Key.
        # ON CONFLICT: backfill trakt_id/imdb_id if the row was previously inserted without them
        # (e.g. by FlixPatrol which only has a TMDB ID).
        movies_static_cursor.execute("""
            INSERT INTO movies (tmdb_id, trakt_id, title, year, imdb_id, tagline, overview, released, runtime, country,
            rating, language, certification, original_title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tmdb_id) DO UPDATE SET
                trakt_id = COALESCE(trakt_id, excluded.trakt_id),
                imdb_id  = COALESCE(imdb_id,  excluded.imdb_id)
        """, (tmdb_id, movie_id, title, year, imdb_id, tagline, overview, released, runtime, country,
              rating, language, certification, original_title))

        # Insert movie into dynamic database
        movies_dynamic_cursor.execute("""
            INSERT OR IGNORE INTO movie_status (tmdb_id, trakt_id, watched, user_rating, last_updated)
            VALUES (?, ?, ?, ?, ?)
        """, (tmdb_id, movie_id, 0, rating, released))



        for genre in genres:
            # a) Ensure the genre exists
            movies_static_cursor.execute(
                "INSERT OR IGNORE INTO genres(name) VALUES(?)",
                (genre,)
            )
            # b) Link movie <→ genre
            movies_static_cursor.execute(
                "INSERT OR IGNORE INTO movie_genres(trakt_id, tmdb_id, genre) VALUES(?, ?, ?)",
                (movie_id, tmdb_id, genre)
            )

        log(f"Updating static data for movie", level=LOGINFO)
        tmdb_handler.update_movie_static_data_from_tmdb(movie_id, tmdb_id, movies_static_cursor)
