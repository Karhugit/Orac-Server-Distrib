import sqlite3
from resources.lib.log_utils import log, LOGDEBUG, LOGERROR, LOGINFO


def normalize_tag(tag_name):
    """
    Normalize tag name: convert to lowercase and replace spaces with hyphens.
    
    Args:
        tag_name: Raw tag name from user input
        
    Returns:
        Normalized tag name in lowercase with hyphens instead of spaces
    """
    if not tag_name:
        return ""
    return tag_name.lower().strip().replace(" ", "-")


def get_all_tags(db_path):
    """
    Get list of all unique tags.
    
    Args:
        db_path: Path to tags database
        
    Returns:
        List of tag names (strings)
    """
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT tag_name FROM tags ORDER BY tag_name")
            rows = cursor.fetchall()
            return [row[0] for row in rows]
    except Exception as e:
        log(f"[Orac] Error getting all tags: {e}", level=LOGERROR)
        return []


def get_all_tags_with_counts(db_path):
    """
    Get list of all unique tags with item counts.
    
    Args:
        db_path: Path to tags database
        
    Returns:
        List of dicts: {'tag_name': str, 'total_count': int, 'movie_count': int, 'show_count': int}
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT 
                    tag_name,
                    COUNT(*) as total_count,
                    SUM(CASE WHEN media_type = 'movie' THEN 1 ELSE 0 END) as movie_count,
                    SUM(CASE WHEN media_type = 'show' OR media_type = 'tvshow' THEN 1 ELSE 0 END) as show_count
                FROM tag_items
                GROUP BY tag_name
                ORDER BY tag_name
            """)
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        log(f"[Orac] Error getting tags with counts: {e}", level=LOGERROR)
        return []


def get_tags_for_item(db_path, media_type, tmdb_id):
    """
    Get all tags associated with a specific item.
    
    Args:
        db_path: Path to tags database
        media_type: 'movie' or 'show'
        tmdb_id: TMDB ID of the item
        
    Returns:
        List of tag names associated with the item
    """
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT tag_name FROM tag_items 
                WHERE media_type = ? AND tmdb_id = ?
                ORDER BY tag_name
            """, (media_type, tmdb_id))
            rows = cursor.fetchall()
            return [row[0] for row in rows]
    except Exception as e:
        log(f"[Orac] Error getting tags for item {media_type}/{tmdb_id}: {e}", level=LOGERROR)
        return []


def add_tag_to_item(db_path, media_type, tmdb_id, tag_name, trakt_id=None, 
                    movies_static_db_path=None, tvshows_static_db_path=None):
    """
    Add a tag to an item. Creates the tag if it doesn't exist.
    If trakt_id is not provided, it will be looked up from static databases.
    
    Args:
        db_path: Path to tags database
        media_type: 'movie' or 'show'
        tmdb_id: TMDB ID of the item
        tag_name: Tag to add (will be normalized)
        trakt_id: Optional Trakt ID (will be looked up if not provided)
        movies_static_db_path: Path to movies static database (for Trakt ID lookup)
        tvshows_static_db_path: Path to TV shows static database (for Trakt ID lookup)
        
    Returns:
        True if successful, False otherwise
    """
    # Normalize the tag
    tag_name = normalize_tag(tag_name)
    if not tag_name:
        log("[Orac] Cannot add empty tag", level=LOGERROR)
        return False
    
    # Look up trakt_id if not provided
    if trakt_id is None:
        trakt_id = _get_trakt_id(media_type, tmdb_id, movies_static_db_path, tvshows_static_db_path)
        if trakt_id is None:
            log(f"[Orac] Could not find trakt_id for {media_type}/{tmdb_id}, continuing with TMDB ID only", level=LOGINFO)
            # Proceed with None/NULL for trakt_id
    
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            
            # Create tag if it doesn't exist
            cursor.execute("INSERT OR IGNORE INTO tags (tag_name) VALUES (?)", (tag_name,))
            
            # Create tag-item association
            cursor.execute("""
                INSERT OR IGNORE INTO tag_items (tag_name, media_type, tmdb_id, trakt_id)
                VALUES (?, ?, ?, ?)
            """, (tag_name, media_type, tmdb_id, trakt_id))
            
            conn.commit()
            log(f"[Orac] Added tag '{tag_name}' to {media_type}/{tmdb_id}", level=LOGINFO)
            return True
    except Exception as e:
        log(f"[Orac] Error adding tag to item: {e}", level=LOGERROR)
        return False


def remove_tag_from_item(db_path, media_type, tmdb_id, tag_name):
    """
    Remove a tag association from an item.
    
    Args:
        db_path: Path to tags database
        media_type: 'movie' or 'show'
        tmdb_id: TMDB ID of the item
        tag_name: Tag to remove (will be normalized)
        
    Returns:
        True if successful, False otherwise
    """
    # Normalize the tag
    tag_name = normalize_tag(tag_name)
    if not tag_name:
        log("[Orac] Cannot remove empty tag", level=LOGERROR)
        return False
    
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            
            # Remove tag-item association
            cursor.execute("""
                DELETE FROM tag_items 
                WHERE tag_name = ? AND media_type = ? AND tmdb_id = ?
            """, (tag_name, media_type, tmdb_id))
            
            # Clean up orphaned tags (tags with no items)
            cursor.execute("""
                DELETE FROM tags 
                WHERE tag_name NOT IN (SELECT DISTINCT tag_name FROM tag_items)
            """)
            
            conn.commit()
            log(f"[Orac] Removed tag '{tag_name}' from {media_type}/{tmdb_id}", level=LOGINFO)
            return True
    except Exception as e:
        log(f"[Orac] Error removing tag from item: {e}", level=LOGERROR)
        return False


def get_items_with_tag(db_path, tag_name):
    """
    Get all items that have a specific tag.
    
    Args:
        db_path: Path to tags database
        tag_name: Tag to search for (will be normalized)
        
    Returns:
        List of dictionaries with keys: media_type, tmdb_id, trakt_id
    """
    # Normalize the tag
    tag_name = normalize_tag(tag_name)
    if not tag_name:
        return []
    
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT media_type, tmdb_id, trakt_id 
                FROM tag_items 
                WHERE tag_name = ?
                ORDER BY media_type, tmdb_id
            """, (tag_name,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        log(f"[Orac] Error getting items with tag '{tag_name}': {e}", level=LOGERROR)
        return []


def _get_trakt_id(media_type, tmdb_id, movies_static_db_path, tvshows_static_db_path):
    """
    Helper function to look up trakt_id from tmdb_id.
    
    Args:
        media_type: 'movie' or 'show'
        tmdb_id: TMDB ID
        movies_static_db_path: Path to movies static database
        tvshows_static_db_path: Path to TV shows static database
        
    Returns:
        Trakt ID if found, None otherwise
    """
    try:
        if media_type == 'movie' and movies_static_db_path:
            with sqlite3.connect(movies_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT trakt_id FROM movies WHERE tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                return row[0] if row else None
        elif media_type == 'show' and tvshows_static_db_path:
            with sqlite3.connect(tvshows_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT show_trakt_id FROM shows WHERE show_tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                return row[0] if row else None
    except Exception as e:
        log(f"[Orac] Error looking up trakt_id for {media_type}/{tmdb_id}: {e}", level=LOGERROR)
    return None
