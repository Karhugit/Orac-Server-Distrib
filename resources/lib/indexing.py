import sqlite3
from resources.lib.log_utils import log, LOGERROR, LOGDEBUG, LOGINFO, LOGWARNING
import json


def get_genres(movies_static_db_path, movies_dynamic_db_path, tvshows_static_db_path, tvshows_dynamic_db_path, item_type):
    """Get all available genres for the specified item type"""
    try:
        if item_type == "tvshow":
            with sqlite3.connect(tvshows_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM genres ORDER BY name")
                genres = [row[0] for row in cursor.fetchall()]
                return genres
        
        elif item_type == "movie":
            with sqlite3.connect(movies_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM genres ORDER BY name")
                genres = [row[0] for row in cursor.fetchall()]
                return genres
        
        else:
            log(f"[Orac] Unknown item_type: {item_type}", level=LOGWARNING)
            return []
            
    except Exception as e:
        log(f"[Orac] Error getting genres for {item_type}: {str(e)}", level=LOGERROR)
        return []

def add_external_index(params, external_indexes_db_path):
    """Add or update an external index in the external indexes database"""
    try:
        # Set a longer timeout to handle potential locks
        with sqlite3.connect(external_indexes_db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO external_indexes (id, media_type, parameters, add_to_library)
                VALUES (?, ?, ?, ?)
            """, (
                params.get('label'),
                params.get('item_type'),
                json.dumps(params.get('parameters', {})),
                params.get('add_to_library', False)
            ))
            conn.commit()  # Explicitly commit the transaction
            log(f"[Orac] Added/Updated external index for ID {params.get('label')}", level=LOGDEBUG)
            return True
    except Exception as e:
        log(f"[Orac] Error adding/updating external index: {str(e)}", level=LOGERROR)
        return False

def del_external_index(params, external_indexes_db_path):
    """Delete an external index from the external indexes database"""
    try:
        # Set a longer timeout to handle potential locks
        with sqlite3.connect(external_indexes_db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM external_indexes
                WHERE id = ? AND media_type = ?
            """, (
                params.get('index_id'),
                params.get('item_type')
            ))
            conn.commit()  # Explicitly commit the transaction
            log(f"[Orac] Deleted external index for ID {params.get('index_id')}", level=LOGDEBUG)
            return True
    except Exception as e:
        log(f"[Orac] Error deleting external index: {str(e)}", level=LOGERROR)
        return False

def get_active_external_indexes(external_indexes_db_path):
    """
    Get all external indexes that are marked for addition to the library.
    """
    try:
        with sqlite3.connect(external_indexes_db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, media_type, parameters 
                FROM external_indexes 
                WHERE add_to_library = 1
            """)
            rows = cursor.fetchall()
            
            indexes = []
            for row in rows:
                index_item = dict(row)
                if 'parameters' in index_item and isinstance(index_item['parameters'], str):
                    try:
                        index_item['parameters'] = json.loads(index_item['parameters'])
                    except json.JSONDecodeError:
                        log(f"[Orac] Failed to parse parameters for index {index_item.get('id')}", level=LOGWARNING)
                        continue
                indexes.append(index_item)
            return indexes
            
    except Exception as e:
        log(f"[Orac] Error getting active external indexes: {str(e)}", level=LOGERROR)
        return []
