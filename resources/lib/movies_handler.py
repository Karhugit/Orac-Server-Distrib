import sqlite3
import json
from resources.lib.log_utils import log, LOGERROR, LOGDEBUG, LOGINFO
from time import time
from collections import defaultdict
from resources.lib.formatting_utils import format_movie

def _format_tmdb_movie_details(tmdb_data, tmdb_handler):
    """Formats a raw TMDb movie details object into the application's standard format."""
    # Use the unified formatter
    return format_movie(tmdb_data, tmdb_handler)

def handle_movie_request(movie_tmdb_id, movies_dynamic_db_path, movies_static_db_path, tmdb_handler=None):
    """
    Returns enriched movie data
    for a given TMDB ID, including user-specific data if available.
    """

    try:
        starttime = time()
        with sqlite3.connect(movies_static_db_path) as static_conn:
            static_cursor = static_conn.cursor()

            static_cursor.execute(f"""attach database '{movies_dynamic_db_path}' as dynamic;""")

            static_cursor.execute(f"""
                SELECT 
                    m.tmdb_id, 
                    m.imdb_id, 
                    m.title, 
                    m.year, 
                    m.released, 
                    m.tagline, 
                    m.overview, 
                    m.runtime, 
                    m.country, 
                    m.rating, 
                    m.language, 
                    m.certification, 
                    m.original_title,
                    m.trakt_id, 
                    m.poster_path, 
                    m.fanart_path, 
                    m.thumbnail_path, 
                    m.landscape_path, 
                    m.clearlogo_path, 
                    m.belongs_to_collection,
                    m.studio,
                    m.fanart_poster_path,
                    m.fanart_fanart_path,
                    m.fanart_clearlogo_path,
                    m.fanart_last_updated,
                    ms.watched, 
                    mg.genre,
                    m.trailer
                FROM movies m
                LEFT JOIN movie_genres mg ON m.tmdb_id = mg.tmdb_id
                LEFT JOIN dynamic.movie_status ms ON m.tmdb_id = ms.tmdb_id
                WHERE m.tmdb_id = ?
            """, (movie_tmdb_id,))
            rows = static_cursor.fetchall()

            static_cursor.execute("detach database dynamic;")

            if not rows:
                # Movie not found in DB, attempt to fetch from TMDB if handler is available
                if tmdb_handler:
                    log(f"Movie with TMDB ID {movie_tmdb_id} not found in DB. Fetching from TMDB.", LOGINFO)
                    # Fetch comprehensive movie data from TMDB
                    movie_data = tmdb_handler._get(f"/movie/{movie_tmdb_id}?append_to_response=images,external_ids")
                    if not movie_data:
                        return 404, "Movie not found in local database or TMDB", "text/plain"
                    
                    formatted_movie = _format_tmdb_movie_details(movie_data, tmdb_handler)
                    endtime = time()
                    log(f"[Orac] handle_movie_request (TMDB fallback) took {endtime - starttime:.2f} seconds for TMDB ID {movie_tmdb_id}", level=LOGDEBUG)
                    return 200, json.dumps(formatted_movie), "application/json"
                else:
                    return 404, "Movie not found", "text/plain"


            # Step 3: Format and group results in Python
            # Use a dictionary to store movies, keyed by tmdb_id, to easily add genres
            movies_dict = defaultdict(lambda: {
                "tmdb_id": None, "imdb_id": None, "title": None, "year": None, "released": None,
                "tagline": None, "overview": None, "runtime": None, "country": None,
                "rating": None, "language": None, "certification": None, "original_title": None,
                "trakt_id": None, "poster_path": None, "fanart_path": None, "thumbnail_path": None,
                "landscape_path": None, "clearlogo_path": None, "belongs_to_collection": None,
                "studio": None, "watched": None, "genres": [], "trailer": None
            })
            
            for row in rows:
                tmdb_id = row[0] # tmdb_id is index 0
    
                # If this is the first time we encounter this movie (or initial details haven't been set)
                # Populate the main movie details only once
                if movies_dict[tmdb_id].get("tmdb_id") is None:
                    movies_dict[tmdb_id].update({
                        "tmdb_id": tmdb_id,
                        "imdb_id": row[1],
                        "title": row[2],
                        "year": row[3],
                        "released": row[4],
                        "tagline": row[5],
                        "overview": row[6],
                        "runtime": row[7],
                        "country": row[8],
                        "rating": row[9],
                        "language": row[10],
                        "certification": row[11],
                        "original_title": row[12],
                        "trakt_id": row[13],
                        "poster_path": row[14],
                        "fanart_path": row[15],
                        "thumbnail_path": row[16],
                        "landscape_path": row[17],
                        "clearlogo_path": row[18],
                        "belongs_to_collection": json.loads(row[19]) if row[19] else None,
                        "studio": json.loads(row[20]) if row[20] else None,
                        "fanart_poster_path": row[21],
                        "fanart_fanart_path": row[22],
                        "fanart_clearlogo_path": row[23],
                        "fanart_last_updated": row[24],
                        "watched": row[25] if row[25] is not None else 0,
                        "trailer": row[27]
                    })
    
                    # Add the genre if it exists (it will be None for movies without genres)
                genre = row[26]
                if genre is not None:
                    # Avoid duplicate genres if the same movie_genre entry exists multiple times (shouldn't happen with PRIMARY KEY)
                    if genre not in movies_dict[tmdb_id]["genres"]:
                        movies_dict[tmdb_id]["genres"].append(genre)
 
        # Convert the dictionary values back to a list
        movie_list = list(movies_dict.values())
        movie = movie_list[0] if movie_list else None
        
        if not movie:
            return 404, "Movie not found", "text/plain"

        # Check if we need to fetch fanart on the fly
        try:
            from resources.lib.config_handler import get_fanart_config
            config = get_fanart_config()
            if config["fanart_enabled"] and movie.get("fanart_last_updated") is None:
                log(f"[MoviesHandler] Fanart enabled but not populated for TMDB ID {movie_tmdb_id}, fetching on-the-fly...", LOGINFO)
                from resources.lib.fanart_client import sync_fanart_for_item
                sync_fanart_for_item(movie_tmdb_id, "movie", tmdb_handler, config_db_path=None, force=True)
                # Re-query the fanart columns to get the new paths
                with sqlite3.connect(movies_static_db_path) as conn2:
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        SELECT fanart_poster_path, fanart_fanart_path, fanart_clearlogo_path, fanart_last_updated
                        FROM movies WHERE tmdb_id = ?
                    """, (movie_tmdb_id,))
                    row2 = cursor2.fetchone()
                    if row2:
                        movie["fanart_poster_path"] = row2[0]
                        movie["fanart_fanart_path"] = row2[1]
                        movie["fanart_clearlogo_path"] = row2[2]
                        movie["fanart_last_updated"] = row2[3]
        except Exception as e:
            log(f"[MoviesHandler] Error syncing fanart on-the-fly: {e}", LOGERROR)

        # Pass through the formatter to ensure standard keys
        movie = format_movie(movie, tmdb_handler)

        endtime = time()
        log(f"[Orac] handle_movie_request took {endtime - starttime:.2f} seconds for TMDB ID {movie_tmdb_id}", level=LOGDEBUG)
        # Convert to JSON
        result = json.dumps(movie) # Return single movie object
        return 200, result, "application/json"

    except Exception as e:
        log(f"[Orac] Error in handle_movie_request: {e}", level=LOGERROR)
        return 500, "Error getting movie", "text/plain"