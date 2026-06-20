# -*- coding: utf-8 -*-
import sqlite3
import json
from resources.lib.log_utils import log, LOGERROR, LOGDEBUG, LOGINFO, LOGWARNING
from resources.lib.database_manager import DatabaseManager
from datetime import datetime, timedelta
import re


def add_internal_index(params, db_path):
    """Add or update an internal index in the database."""
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO internal_indexes (id, media_type, parameters, add_to_library)
                VALUES (?, ?, ?, ?)
            """, (
                params.get('label'),
                params.get('item_type'),
                json.dumps(params.get('parameters', {})),
                params.get('add_to_library', 0)
            ))
            conn.commit()
            log(f"[InternalIndexing] Added/Updated internal index: {params.get('label')}", level=LOGINFO)
            return True
    except Exception as e:
        log(f"[InternalIndexing] Error adding internal index: {e}", level=LOGERROR)
        return False


def del_internal_index(params, db_path):
    """Delete an internal index from the database."""
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM internal_indexes
                WHERE id = ? AND media_type = ?
            """, (
                params.get('index_id'),
                params.get('item_type')
            ))
            conn.commit()
            log(f"[InternalIndexing] Deleted internal index: {params.get('index_id')}", level=LOGINFO)
            return True
    except Exception as e:
        log(f"[InternalIndexing] Error deleting internal index: {e}", level=LOGERROR)
        return False


def get_internal_indexes(db_path, media_type):
    """Get all internal indexes for a specific media type."""
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, media_type, parameters, add_to_library
                FROM internal_indexes
                WHERE media_type = ?
                ORDER BY id
            """, (media_type,))
            rows = cursor.fetchall()
            
            indexes = []
            for row in rows:
                index_item = dict(row)
                if 'parameters' in index_item and isinstance(index_item['parameters'], str):
                    try:
                        index_item['parameters'] = json.loads(index_item['parameters'])
                    except json.JSONDecodeError:
                        log(f"[InternalIndexing] Failed to parse parameters for index {index_item.get('id')}", level=LOGWARNING)
                        index_item['parameters'] = {}
                indexes.append(index_item)
            return indexes
    except Exception as e:
        log(f"[InternalIndexing] Error getting internal indexes: {e}", level=LOGERROR)
        return []


def parse_date_offset(value):
    """
    Parses a date string or 'T-x' offset into 'YYYY-MM-DD'.
    """
    if not value: return None
    
    # Check for T-x format (case insensitive)
    offset_match = re.match(r'^[Tt]([+-])(\d+)$', value)
    if offset_match:
        try:
            sign = offset_match.group(1)
            days = int(offset_match.group(2))
            
            today = datetime.now()
            if sign == '-':
                target_date = today - timedelta(days=days)
            else:
                target_date = today + timedelta(days=days)
                
            return target_date.strftime('%Y-%m-%d')
        except Exception as e:
            log(f"[InternalIndexing] Error parsing date offset {value}: {e}", level=LOGWARNING)
            return None
            
    # Check for static date YYYY-MM-DD
    # Simple check, we assume input is generally valid from client
    if re.match(r'^\d{4}-\d{2}-\d{2}$', value):
        return value
        
    return None


def get_internal_index_contents(db_path, index_id, media_type, static_db, dynamic_db, user="", tags_db_path=None):
    """
    Query local media based on an internal index's filter parameters.
    Standardizes output to match normal list requests.
    """
    from resources.lib.formatting_utils import format_movie
    from collections import defaultdict
    
    # TMDb genre IDs mapped to Trakt genre names (database uses Trakt names)
    GENRE_ID_TO_NAMES = {
        '28': ['action'], '12': ['adventure'], '16': ['animation'], '35': ['comedy'],
        '80': ['crime'], '99': ['documentary'], '18': ['drama'], '10751': ['family'],
        '14': ['fantasy'], '36': ['history'], '27': ['horror'], '10402': ['music'],
        '9648': ['mystery'], '10749': ['romance'], '878': ['science-fiction'],
        '10770': ['tv-movie'], '53': ['thriller'], '10752': ['war'], '37': ['western'],
        # TV Specific TMDb IDs mapped to one or more Trakt genre names
        '10759': ['action', 'adventure'], 
        '10762': ['children', 'family'], 
        '10763': ['news'], 
        '10764': ['reality'], 
        '10765': ['science-fiction', 'fantasy'], 
        '10766': ['soap'], 
        '10767': ['talk'], 
        '10768': ['war', 'politics']
    }
    
    try:
        # First, get the index parameters
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT parameters FROM internal_indexes
                WHERE id = ? AND media_type = ?
            """, (index_id, media_type))
            row = cursor.fetchone()
            
            if not row:
                log(f"[InternalIndexing] Index not found: {index_id}", level=LOGWARNING)
                return []
            
            params = json.loads(row['parameters']) if isinstance(row['parameters'], str) else row['parameters']
        
        log(f"[InternalIndexing] Index found for {media_type}. Parameters: {params}", level=LOGINFO)
        
        with_genres = params.get('with_genres', '')
        without_genres = params.get('without_genres', '')
        
        # Convert genre IDs to names
        with_genre_names = []
        if with_genres:
            for genre_id in with_genres.split('|'):
                names = GENRE_ID_TO_NAMES.get(genre_id, [])
                with_genre_names.extend(names)
        
        without_genre_names = []
        if without_genres:
            for genre_id in without_genres.split('|'):
                names = GENRE_ID_TO_NAMES.get(genre_id, [])
                without_genre_names.extend(names)
        
        log(f"[InternalIndexing] Filtering by genres: with={with_genre_names}, without={without_genre_names}", level=LOGINFO)
        
        # Helper to get list items
        with_lists = params.get('with_lists', '')
        list_tmdb_ids = set()
        if with_lists:
            try:
                # with_lists format: user|slug,user|slug
                list_identifiers = with_lists.split(',')
                # Build tuples of (user, slug)
                target_lists = []
                for item in list_identifiers:
                    if '|' in item:
                        target_lists.append(tuple(item.split('|', 1)))
                
                if target_lists:
                    with DatabaseManager().connection('lists') as conn:
                        cursor = conn.cursor()
                        # Get list_ids first
                        placeholders = ','.join(['(?,?)' for _ in target_lists])
                        # Flatten list of tuples for SQL args
                        sql_args = [elem for t in target_lists for elem in t]
                        
                        # SQLite doesn't strictly support (col1, col2) IN ((val1, val2)...) in all versions properly or efficiently in this context sometimes,
                        # but constructing ORs is safer: (user=? AND slug=?) OR ...
                        or_conditions = ' OR '.join(['(user=? AND slug=?)' for _ in target_lists])
                        
                        cursor.execute(f"SELECT list_id FROM lists WHERE {or_conditions}", sql_args)
                        list_ids = [row[0] for row in cursor.fetchall()]
                        
                        if list_ids:
                            placeholders = ','.join(['?' for _ in list_ids])
                            cursor.execute(f"SELECT tmdb_id FROM list_items WHERE list_id IN ({placeholders}) AND tmdb_id IS NOT NULL", list_ids)
                            list_tmdb_ids = {int(row[0]) for row in cursor.fetchall()}
                            
                log(f"[InternalIndexing] Filtering by lists: {with_lists} -> Found {len(list_tmdb_ids)} items", level=LOGINFO)
            except Exception as e:
                log(f"[InternalIndexing] Error processing list filter: {e}", level=LOGERROR)

        # Helper to get tag items
        with_tags = params.get('with_tags', '')
        tag_tmdb_ids = set()
        if with_tags and tags_db_path:
            try:
                # with_tags format: tag1|tag2|tag3 (pipe separated)
                tags = with_tags.split('|')
                
                # Fetch items for each tag and combine (UNION logic - ANY match)
                # Consistent with "items would be included if they match any of the tags"
                from resources.lib.tags_handler import get_items_with_tag
                
                for tag in tags:
                    items = get_items_with_tag(tags_db_path, tag)
                    # Filter items by current media_type
                    # Note: get_items_with_tag returns dicts: {media_type, tmdb_id, ...}
                    # We only care about tmdb_id for the current media_type
                    
                    for item in items:
                        # Normalize media type check (show/tvshow)
                        item_mt = item['media_type']
                        if item_mt == 'show': item_mt = 'tvshow'
                        
                        target_mt = media_type
                        if target_mt == 'show': target_mt = 'tvshow'
                        
                        if item_mt == target_mt:
                            tag_tmdb_ids.add(item['tmdb_id'])
                
                log(f"[InternalIndexing] Filtering by tags: {with_tags} -> Found {len(tag_tmdb_ids)} items", level=LOGINFO)
            except Exception as e:
                log(f"[InternalIndexing] Error processing tag filter: {e}", level=LOGERROR)
                
        # Parse filters
        min_year = params.get('min_year') or params.get('min_first_aired_year')
        max_year = params.get('max_year') or params.get('max_first_aired_year')
        min_runtime = params.get('min_runtime')
        max_runtime = params.get('max_runtime')
        min_rating = params.get('min_rating')
        min_votes = params.get('min_votes')
        country = params.get('country')
        language = params.get('language')
        certification = params.get('certification')
        min_user_rating = params.get('min_user_rating')
        watched_status = params.get('watched_status')
        status = params.get('status')
        dropped = params.get('dropped')
        released = params.get('released')
        network = params.get('network')
        sort_by = params.get('sort_by')
        
        # Release Date filters (Movies)
        release_date_gte_param = params.get('release_date.gte')
        release_date_lte_param = params.get('release_date.lte')
        
        release_date_gte = parse_date_offset(release_date_gte_param)
        release_date_lte = parse_date_offset(release_date_lte_param)
        
        # Query static DB
        with sqlite3.connect(static_db, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(f"ATTACH DATABASE '{dynamic_db}' AS dynamic")
            
            conditions = []
            params_list = []
            
            if media_type == 'movie':
                # Build the query for movies
                query = """
                    SELECT 
                        m.tmdb_id, m.imdb_id, m.title, m.year, m.released, 
                        m.tagline, m.overview, m.runtime, m.country, m.rating, 
                        m.language, m.certification, m.original_title, m.trakt_id, 
                        m.poster_path, m.fanart_path, m.thumbnail_path, 
                        m.landscape_path, m.clearlogo_path, m.belongs_to_collection,
                        m.studio, ms.watched, ms.watched_status, mg_all.genre as item_genre
                    FROM movies m
                    LEFT JOIN movie_genres mg_all ON m.tmdb_id = mg_all.tmdb_id
                    LEFT JOIN dynamic.movie_status ms ON m.trakt_id = ms.trakt_id
                """
                
                if with_genre_names:
                    placeholders = ','.join(['?' for _ in with_genre_names])
                    conditions.append(f"m.tmdb_id IN (SELECT tmdb_id FROM movie_genres WHERE genre IN ({placeholders}))")
                    params_list.extend(with_genre_names)
                if without_genre_names:
                    placeholders = ','.join(['?' for _ in without_genre_names])
                    conditions.append(f"m.tmdb_id NOT IN (SELECT tmdb_id FROM movie_genres WHERE genre IN ({placeholders}))")
                    params_list.extend(without_genre_names)
                
                if with_lists:
                    # If lists were selected but no items found, then result should be empty (or matching 0 items)
                    # We can use "m.tmdb_id IN (...)" or if empty "0=1"
                    if list_tmdb_ids:
                        placeholders = ','.join(['?' for _ in list_tmdb_ids])
                        conditions.append(f"m.tmdb_id IN ({placeholders})")
                        params_list.extend(list(list_tmdb_ids))
                    else:
                        conditions.append("0=1") # No items matched list filter
                        
                if with_tags:
                    if tag_tmdb_ids:
                        placeholders = ','.join(['?' for _ in tag_tmdb_ids])
                        conditions.append(f"m.tmdb_id IN ({placeholders})")
                        params_list.extend(list(tag_tmdb_ids))
                    else:
                        conditions.append("0=1") # Tags selected but no items found

                    
                if min_year: conditions.append("m.year >= ?"); params_list.append(int(min_year))
                if max_year: conditions.append("m.year <= ?"); params_list.append(int(max_year))
                if min_runtime: conditions.append("m.runtime >= ?"); params_list.append(int(min_runtime))
                if max_runtime: conditions.append("m.runtime <= ?"); params_list.append(int(max_runtime))
                if min_rating: conditions.append("m.rating >= ?"); params_list.append(float(min_rating))
                if country: conditions.append("m.country LIKE ?"); params_list.append(f"%{country}%")
                if language: conditions.append("m.language = ?"); params_list.append(language)
                if certification: conditions.append("m.certification = ?"); params_list.append(certification)
                if min_user_rating: conditions.append("ms.user_rating >= ?"); params_list.append(float(min_user_rating))
                if watched_status != '' and watched_status is not None:
                    conditions.append("COALESCE(ms.watched_status, 0) = ?"); params_list.append(int(watched_status))
                
                if release_date_gte: 
                    # Movies 'released' column is INTEGER YYYYMMDD
                    try:
                        date_int = int(release_date_gte.replace('-', ''))
                        conditions.append("m.released >= ?")
                        params_list.append(date_int)
                    except ValueError:
                        pass # Ignore invalid date format if somehow it slipped through
                        
                if release_date_lte: 
                    try:
                        date_int = int(release_date_lte.replace('-', ''))
                        conditions.append("m.released <= ?")
                        params_list.append(date_int)
                    except ValueError:
                        pass
                
                
                if conditions: query += " WHERE " + " AND ".join(conditions)
                
                # Dynamic Sorting
                # Default to title ascending
                order_clause = "m.title ASC" 
                
                if sort_by:
                    if sort_by == 'release_date.desc': order_clause = "m.released DESC"
                    elif sort_by == 'release_date.asc': order_clause = "m.released ASC"
                    elif sort_by == 'rating.desc': order_clause = "m.rating DESC"
                    elif sort_by == 'rating.asc': order_clause = "m.rating ASC"
                    elif sort_by == 'title.desc': order_clause = "m.title DESC"
                    elif sort_by == 'title.asc': order_clause = "m.title ASC"
                    elif sort_by == 'random': order_clause = "RANDOM()"
                
                query += f" ORDER BY {order_clause}"
                
            elif media_type == 'tvshow':
                query = """
                    SELECT 
                        s.show_trakt_id, s.show_tmdb_id, s.imdb_id, s.title, s.year, s.slug, s.last_updated, s.first_aired, 
                        s.poster_path, s.fanart_path, s.thumbnail_path, s.landscape_path, s.clearlogo_path,
                        s.original_title, s.trailer, s.overview, s.tagline, s.status, s.certification, s.network, s.country, s.rating, s.votes,
                        tg_all.genre as item_genre,
                        uss.watched_status
                    FROM shows s
                    LEFT JOIN tvshows_genres tg_all ON s.show_tmdb_id = tg_all.tmdb_id
                    LEFT JOIN dynamic.user_show_sync uss ON s.show_tmdb_id = uss.show_tmdb_id AND uss.user = ?
                """
                
                if with_genre_names:
                    placeholders = ','.join(['?' for _ in with_genre_names])
                    conditions.append(f"s.show_tmdb_id IN (SELECT tmdb_id FROM tvshows_genres WHERE genre IN ({placeholders}))")
                    params_list.extend(with_genre_names)
                if without_genre_names:
                    placeholders = ','.join(['?' for _ in without_genre_names])
                    conditions.append(f"s.show_tmdb_id NOT IN (SELECT tmdb_id FROM tvshows_genres WHERE genre IN ({placeholders}))")
                    params_list.extend(without_genre_names)
                    
                if with_lists:
                    if list_tmdb_ids:
                        placeholders = ','.join(['?' for _ in list_tmdb_ids])
                        conditions.append(f"s.show_tmdb_id IN ({placeholders})")
                        params_list.extend(list(list_tmdb_ids))
                    else:
                        conditions.append("0=1")

                if with_tags:
                    if tag_tmdb_ids:
                        placeholders = ','.join(['?' for _ in tag_tmdb_ids])
                        conditions.append(f"s.show_tmdb_id IN ({placeholders})")
                        params_list.extend(list(tag_tmdb_ids))
                    else:
                        conditions.append("0=1")
                    
                if min_year: conditions.append("s.year >= ?"); params_list.append(int(min_year))
                if max_year: conditions.append("s.year <= ?"); params_list.append(int(max_year))
                if min_rating: conditions.append("s.rating >= ?"); params_list.append(float(min_rating))
                if min_votes: conditions.append("s.votes >= ?"); params_list.append(int(min_votes))
                if country: conditions.append("s.country LIKE ?"); params_list.append(f"%{country}%")
                if certification: conditions.append("s.certification = ?"); params_list.append(certification)
                if status:
                    status_list = [s.strip().lower() for s in status.split('|') if s.strip()]
                    if status_list:
                        placeholders = ','.join(['?' for _ in status_list])
                        conditions.append(f"LOWER(s.status) IN ({placeholders})")
                        params_list.extend(status_list)
                if dropped != '' and dropped is not None: conditions.append("s.dropped = ?"); params_list.append(int(dropped))
                if network: conditions.append("s.network LIKE ?"); params_list.append(f"%{network}%")
                if language: conditions.append("s.language = ?"); params_list.append(language)
                
                if watched_status != '' and watched_status is not None:
                    conditions.append("COALESCE(uss.watched_status, 0) = ?"); params_list.append(int(watched_status))
                
                if conditions: query += " WHERE " + " AND ".join(conditions)
                
                # Dynamic Sorting
                order_clause = "s.title ASC"
                
                if sort_by:
                    if sort_by == 'first_air_date.desc': order_clause = "s.first_aired DESC"
                    elif sort_by == 'first_air_date.asc': order_clause = "s.first_aired ASC"
                    elif sort_by == 'rating.desc': order_clause = "s.rating DESC"
                    elif sort_by == 'rating.asc': order_clause = "s.rating ASC"
                    elif sort_by == 'title.desc': order_clause = "s.title DESC"
                    elif sort_by == 'title.asc': order_clause = "s.title ASC"
                    elif sort_by == 'random': order_clause = "RANDOM()"
                    
                query += f" ORDER BY {order_clause}"
            elif media_type == 'episode':
                # Build the query for episodes
                # Episodes join with shows for genre filtering and show info
                query = """
                    SELECT 
                        e.episode_trakt_id, e.show_id, e.season, e.episode_number, e.episode_title, e.episode_overview, 
                        e.air_date, e.tmdb_id, e.imdb_id, e.tvdb_id, e.rating, e.first_aired, e.votes, e.runtime,
                        e.episode_type, e.original_title, e.episode_poster_path, e.episode_fanart_path, 
                        e.episode_thumbnail_path, e.episode_clearlogo_path, e.episode_landscape_path,
                        s.title as show_title, s.show_tmdb_id, s.poster_path as show_poster_path, 
                        s.fanart_path as show_fanart_path, s.clearlogo_path as show_clearlogo_path, 
                        s.landscape_path as show_landscape_path, s.overview as show_overview,
                        tg_all.genre as item_genre,
                        we.watched_status,
                        we.percent_watched as watched
                    FROM episodes e
                    JOIN shows s ON e.show_id = s.show_trakt_id
                    LEFT JOIN tvshows_genres tg_all ON s.show_tmdb_id = tg_all.tmdb_id
                    LEFT JOIN dynamic.watched_episodes we ON e.tmdb_id = we.tmdb_id AND we.user = ?
                """

                if with_genre_names:
                    placeholders = ','.join(['?' for _ in with_genre_names])
                    conditions.append(f"s.show_tmdb_id IN (SELECT tmdb_id FROM tvshows_genres WHERE genre IN ({placeholders}))")
                    params_list.extend(with_genre_names)
                if without_genre_names:
                    placeholders = ','.join(['?' for _ in without_genre_names])
                    conditions.append(f"s.show_tmdb_id NOT IN (SELECT tmdb_id FROM tvshows_genres WHERE genre IN ({placeholders}))")
                    params_list.extend(without_genre_names)

                if with_lists:
                    if list_tmdb_ids:
                        placeholders = ','.join(['?' for _ in list_tmdb_ids])
                        # Episode query uses e.tmdb_id for episodes, but list items usually store show/movie TMDB IDs.
                        # If list contains SHOWS, we filter by s.show_tmdb_id.
                        conditions.append(f"s.show_tmdb_id IN ({placeholders})")
                        params_list.extend(list(list_tmdb_ids))
                    else:
                        conditions.append("0=1")

                if with_tags:
                    if tag_tmdb_ids:
                        placeholders = ','.join(['?' for _ in tag_tmdb_ids])
                        # Assuming tags are on the EPISODE itself if media_type is episode
                        conditions.append(f"e.tmdb_id IN ({placeholders})")
                        params_list.extend(list(tag_tmdb_ids))
                    else:
                        conditions.append("0=1")

                # Episode specific filters
                air_date_gte = params.get('air_date_gte')
                air_date_lte = params.get('air_date_lte')
                first_air_date_gte = params.get('first_air_date_gte')
                first_air_date_lte = params.get('first_air_date_lte')
                min_rating = params.get('min_rating') or params.get('episode_rating_gte')
                max_rating = params.get('max_rating') or params.get('episode_rating_lte')
                min_votes = params.get('min_votes') or params.get('episode_votes_gte')

                if air_date_gte: conditions.append("e.air_date >= ?"); params_list.append(air_date_gte)
                if air_date_lte: conditions.append("e.air_date <= ?"); params_list.append(air_date_lte)
                if first_air_date_gte: conditions.append("s.first_aired >= ?"); params_list.append(first_air_date_gte)
                if first_air_date_lte: conditions.append("s.first_aired <= ?"); params_list.append(first_air_date_lte)
                if min_rating: conditions.append("e.rating >= ?"); params_list.append(float(min_rating))
                if max_rating: conditions.append("e.rating <= ?"); params_list.append(float(max_rating))
                if min_votes: conditions.append("e.votes >= ?"); params_list.append(int(min_votes))
                if watched_status != '' and watched_status is not None:
                    conditions.append("COALESCE(we.watched_status, 0) = ?"); params_list.append(int(watched_status))

                if conditions: query += " WHERE " + " AND ".join(conditions)
                query += " ORDER BY e.air_date DESC, e.show_id, e.season, e.episode_number"
            else:
                return []

            if media_type == 'tvshow':
                params_list.insert(0, user)
            elif media_type == 'episode':
                params_list.insert(0, user)
            
            log(f"[InternalIndexing] Executing query: {query} with params: {params_list}", level=LOGINFO)
            cursor.execute(query, params_list)
            rows = cursor.fetchall()
            cursor.execute("DETACH DATABASE dynamic")
            
            # Group by media ID and format
            results_dict = defaultdict(lambda: {
                "trakt_id": None, "tmdb_id": None, "imdb_id": None, "title": None, "year": None,
                "overview": None, "poster_path": None, "fanart_path": None, "thumbnail_path": None,
                "landscape_path": None, "clearlogo_path": None, "rating": None, "votes": None,
                "genres": [], "media_type": media_type, "watched": 0, "watched_status": 0
            })
            
            for row in rows:
                if media_type == 'movie':
                    item_key = row['tmdb_id']
                elif media_type == 'tvshow':
                    item_key = row['show_tmdb_id']
                else: # episode
                    item_key = row['tmdb_id'] # Use TMDB ID for episodes unique identifier in this dict

                if results_dict[item_key]["tmdb_id"] is None:
                    if media_type == 'movie':
                        results_dict[item_key].update({
                            "trakt_id": row['trakt_id'], "tmdb_id": row['tmdb_id'], "imdb_id": row['imdb_id'], "title": row['title'], "year": row['year'],
                            "released": row['released'], "tagline": row['tagline'], "overview": row['overview'], "runtime": row['runtime'],
                            "country": row['country'], "rating": row['rating'], "language": row['language'], "certification": row['certification'],
                            "poster_path": row['poster_path'], "fanart_path": row['fanart_path'], "thumbnail_path": row['thumbnail_path'],
                            "landscape_path": row['landscape_path'], "clearlogo_path": row['clearlogo_path'],
                            "belongs_to_collection": json.loads(row['belongs_to_collection']) if row['belongs_to_collection'] else None,
                            "studio": json.loads(row['studio']) if row['studio'] else None,
                            "watched": row['watched'] if row['watched'] is not None else 0,
                            "watched_status": row['watched_status'] if row['watched_status'] is not None else 0
                        })
                    elif media_type == 'tvshow': # tvshow
                        results_dict[item_key].update({
                            "trakt_id": row['show_trakt_id'], "tmdb_id": row['show_tmdb_id'], "imdb_id": row['imdb_id'], "title": row['title'], "year": row['year'],
                            "premiered": row['first_aired'], "overview": row['overview'], "poster_path": row['poster_path'],
                            "fanart_path": row['fanart_path'], "thumbnail_path": row['thumbnail_path'], "landscape_path": row['landscape_path'],
                            "clearlogo_path": row['clearlogo_path'], "status": row['status'], "certification": row['certification'],
                            "network": row['network'], "country": row['country'], "rating": row['rating'], "votes": row['votes'],
                            "watched": 100 if row['watched_status'] == 2 else 0, 
                            "watched_status": row['watched_status'] if row['watched_status'] is not None else 0,
                            "media_type": "tvshow"
                        })
                    else: # episode
                        results_dict[item_key].update({
                            "episode_trakt_id": row['episode_trakt_id'],
                            "show_trakt_id": row['show_id'],
                            "season": row['season'],
                            "episode": row['episode_number'],
                            "episode_title": row['episode_title'],
                            "episode_overview": row['episode_overview'],
                            "air_date": row['air_date'],
                            "tmdb_id": row['tmdb_id'],
                            "imdb_id": row['imdb_id'],
                            "tvdb_id": row['tvdb_id'],
                            "episode_rating": row['rating'],
                            "first_aired": row['first_aired'],
                            "votes": row['votes'],
                            "runtime": row['runtime'],
                            "episode_type": row['episode_type'],
                            "original_title": row['original_title'],
                            "episode_poster_path": row['episode_poster_path'],
                            "episode_fanart_path": row['episode_fanart_path'],
                            "episode_thumbnail_path": row['episode_thumbnail_path'],
                            "episode_clearlogo_path": row['episode_clearlogo_path'],
                            "episode_landscape_path": row['episode_landscape_path'],
                            "title": row['show_title'], # Show title for context
                            "show_tmdb_id": row['show_tmdb_id'],
                            "show_poster_path": row['show_poster_path'],
                            "show_fanart_path": row['show_fanart_path'],
                            "show_clearlogo_path": row['show_clearlogo_path'],
                            "show_landscape_path": row['show_landscape_path'],
                            "show_overview": row['show_overview'],
                            "watched": row['watched'] if row['watched'] is not None else 0,
                            "watched_status": row['watched_status'] if row['watched_status'] is not None else 0,
                            "media_type": "episode"
                        })
                
                genre = row['item_genre']
                if genre and genre not in results_dict[item_key]["genres"]:
                    results_dict[item_key]["genres"].append(genre)
            
            # Final formatting
            final_results = []
            for item_id, item in results_dict.items():
                if media_type == 'movie':
                    final_results.append(format_movie(item, None))
                else:
                    final_results.append(item)
            
            if media_type == 'episode':
                # Episodes are already sorted by air_date DESC in query
                # But dict iteration might lose it, so we sort again if needed
                # However, since we want to preserve air_date DESC:
                final_results.sort(key=lambda x: (x.get('air_date') or '', x.get('show_trakt_id') or 0, x.get('season') or 0, x.get('episode') or 0), reverse=True)
            log(f"[InternalIndexing] Found {len(final_results)} {media_type}s for index '{index_id}'", level=LOGINFO)
            return final_results
            
    except Exception as e:
        log(f"[InternalIndexing] Error getting index contents: {e}", level=LOGERROR)
        import traceback
        log(f"[InternalIndexing] Traceback: {traceback.format_exc()}", level=LOGERROR)
        return []


def get_available_languages(db_path):
    """
    Get list of distinct languages from the database.
    Currently queries the movies table as it contains the 'language' column.
    """
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            # Check if movies table exists just in case
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='movies'")
            if cursor.fetchone():
                cursor.execute("SELECT DISTINCT language FROM movies WHERE language IS NOT NULL AND language != '' ORDER BY language")
                rows = cursor.fetchall()
                # Filter out garbage if any
                languages = [row[0] for row in rows if len(row[0]) <= 5] # Basic sanity check for ISO codes
                return languages
            return []
    except Exception as e:
        log(f"[InternalIndexing] Error getting languages: {e}", level=LOGERROR)
        return []
