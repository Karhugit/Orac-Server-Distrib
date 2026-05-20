import sqlite3
import json
from resources.lib.log_utils import log, LOGERROR, LOGDEBUG, LOGINFO

def handle_collections_request(static_db_path, dynamic_db_path, tmdb_handler=None, user=None):
    """
    Query the static movies database for all movies that belong to a collection.
    Group them by collection ID.
    Returns:
        status: HTTP status code
        body: JSON serialized list of collections with their movies
        content_type: MIME type
    """
    collections = {}
    
    try:
        with sqlite3.connect(static_db_path) as conn:
            cursor = conn.cursor()
            # Select relevant fields where belongs_to_collection is populated
            cursor.execute('''
                SELECT tmdb_id, title, released, poster_path, fanart_path, belongs_to_collection
                FROM movies 
                WHERE belongs_to_collection IS NOT NULL AND belongs_to_collection != ''
            ''')
            rows = cursor.fetchall()
            
            for row in rows:
                tmdb_id, title, released, poster_path, fanart_path, belongs_to_collection_str = row
                
                try:
                    collection_data = json.loads(belongs_to_collection_str)
                except json.JSONDecodeError:
                    continue
                
                if not collection_data or not isinstance(collection_data, dict):
                    continue
                    
                collection_id = collection_data.get('id')
                if not collection_id:
                    continue
                
                if collection_id not in collections:
                    collections[collection_id] = {
                        'id': collection_id,
                        'name': collection_data.get('name', 'Unknown Collection'),
                        'poster_path': collection_data.get('poster_path'),
                        'backdrop_path': collection_data.get('backdrop_path'),
                        'movies': []
                    }
                
                collections[collection_id]['movies'].append({
                    'tmdb_id': tmdb_id,
                    'title': title,
                    'release_date': released,
                    'poster_path': poster_path,
                    'backdrop_path': fanart_path,
                    'media_type': 'movie'
                })
                
        # Convert to list and sort collections by name, and sort movies within collections by release date
        collection_list = list(collections.values())
        collection_list.sort(key=lambda x: x.get('name', ''))
        
        for coll in collection_list:
            coll['movies'].sort(key=lambda m: m.get('release_date', '') or '')
            
        return 200, json.dumps({"success": True, "collections": collection_list}), "application/json"
        
    except sqlite3.Error as e:
        log(f"Database error in handle_collections_request: {e}", level=LOGERROR)
        return 500, json.dumps({"success": False, "error": "Database error"}), "application/json"
    except Exception as e:
        log(f"Error in handle_collections_request: {e}", level=LOGERROR)
        return 500, json.dumps({"success": False, "error": str(e)}), "application/json"
