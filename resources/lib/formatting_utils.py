import json
from resources.lib.log_utils import log, LOGDEBUG

def get_lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

def get_asset_url(path_value):
    if not path_value:
        return None
    if path_value.startswith(('http://', 'https://')):
        return path_value
    from resources.lib.config_loader import ConfigLoader
    port = ConfigLoader().server_config.get("port", 5555)
    ip = get_lan_ip()
    return f"http://{ip}:{port}/assets/images/{path_value.replace('\\', '/')}"

movie_genres = [
    {'id': 28, 'name': 'Action'}, {'id': 12, 'name': 'Adventure'}, {'id': 16, 'name': 'Animation'},
    {'id': 35, 'name': 'Comedy'}, {'id': 80, 'name': 'Crime'}, {'id': 99, 'name': 'Documentary'},
    {'id': 18, 'name': 'Drama'}, {'id': 10751, 'name': 'Family'}, {'id': 14, 'name': 'Fantasy'},
    {'id': 36, 'name': 'History'}, {'id': 27, 'name': 'Horror'}, {'id': 10402, 'name': 'Music'},
    {'id': 9648, 'name': 'Mystery'}, {'id': 10749, 'name': 'Romance'}, {'id': 878, 'name': 'Science Fiction'},
    {'id': 10770, 'name': 'TV Movie'}, {'id': 53, 'name': 'Thriller'}, {'id': 10752, 'name': 'War'},
    {'id': 37, 'name': 'Western'}
]

MOVIE_GENRE_MAP = {genre['id']: genre['name'] for genre in movie_genres}

def format_movie(item, tmdb_handler, media_type='movie'):
    """
    Standardizes movie data into a common Orac schema.
    Works for raw TMDb API results AND local DB rows (if passed as dict).
    """
    # Extract IDs
    tmdb_id = item.get("tmdb_id") or item.get("id")
    # If item is from TMDb search results, it has 'id'. If it's from DB, it might have 'tmdb_id'.
    
    # Handle external IDs if available (Search results don't have this by default)
    imdb_id = item.get("imdb_id")
    if not imdb_id and "external_ids" in item:
        imdb_id = item["external_ids"].get("imdb_id")
    
    # Genres handling
    genre_names = []
    if "genres" in item and isinstance(item["genres"], list):
        # Already formatted genres
        if item["genres"] and isinstance(item["genres"][0], str):
            genre_names = item["genres"]
        else:
            # TMDb details format: [{'id': 12, 'name': 'Adventure'}, ...]
            genre_names = [g['name'] for g in item["genres"] if 'name' in g]
    elif "genre_ids" in item:
        # TMDb search format: [12, 18, ...]
        genre_names = [MOVIE_GENRE_MAP.get(gid) for gid in item["genre_ids"] if MOVIE_GENRE_MAP.get(gid)]

    # Release Date & Year
    release_date = item.get("release_date") or item.get("released") or ""
    
    # Ensure release_date is a formatted string YYYY-MM-DD
    if isinstance(release_date, int):
        # Convert YYYYMMDD integer to "YYYY-MM-DD"
        rd_str = str(release_date)
        if len(rd_str) == 8:
            release_date = f"{rd_str[:4]}-{rd_str[4:6]}-{rd_str[6:]}"
        else:
            # Fallback if weird integer
            release_date = str(release_date)
    elif isinstance(release_date, str):
        # Convert YYYYMMDD string to "YYYY-MM-DD" if needed
        if len(release_date) == 8 and release_date.isdigit():
             release_date = f"{release_date[:4]}-{release_date[4:6]}-{release_date[6:]}"
            
    year = item.get("year")
    if not year and release_date:
        try:
            year = int(str(release_date)[:4])
        except (ValueError, IndexError):
            year = None

    # Studio/Production Companies
    studio = item.get("studio")
    if not studio and "production_companies" in item:
        studio = [company['name'] for company in item['production_companies']]
    elif isinstance(studio, str):
        try:
            studio = json.loads(studio)
        except:
            pass

    # Belongs to collection
    belongs_to_collection = item.get("belongs_to_collection")
    if isinstance(belongs_to_collection, str):
        try:
            belongs_to_collection = json.loads(belongs_to_collection)
        except:
            pass

    # Image URL construction helper
    def get_url(path, size='w500'):
        if not path: return None
        if path.startswith('http'): return path
        return tmdb_handler._build_url(path, size)

    # Standardized response object
    formatted = {
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "trakt_id": item.get("trakt_id"),
        "title": item.get("title") or item.get("original_title") or item.get("name"),
        "original_title": item.get("original_title") or item.get("title") or item.get("original_name"),
        "year": year,
        "released": release_date,
        "overview": item.get("overview"),
        "runtime": item.get("runtime"),
        "tagline": item.get("tagline"),
        "country": item.get("country") or (item.get("production_countries", [{}])[0].get("iso_3166_1") if item.get("production_countries") else None),
        "rating": item.get("rating") or item.get("vote_average"),
        "votes": item.get("votes") or item.get("vote_count"),
        "language": item.get("language") or item.get("original_language"),
        "certification": item.get("certification"),
        "poster_path": get_url(item.get("poster_path"), "w780"),
        "fanart_path": get_url(item.get("fanart_path") or item.get("backdrop_path"), "w1280"),
        "thumbnail_path": get_url(item.get("thumbnail_path"), "w780") or get_url(item.get("poster_path"), "w780"),
        "landscape_path": get_url(item.get("landscape_path"), "w1280") or get_url(item.get("fanart_path") or item.get("backdrop_path"), "w1280"),
        "clearlogo_path": get_url(item.get("clearlogo_path"), "w500"),
        "belongs_to_collection": belongs_to_collection,
        "studio": studio,
        "genres": genre_names,
        "media_type": media_type,
        "watched": item.get("watched", 0),
        "watched_status": item.get("watched_status", 0)
    }
    
    # Fanart.tv Overrides
    try:
        from resources.lib.config_handler import get_fanart_config
        fanart_cfg = get_fanart_config()
        if fanart_cfg["fanart_enabled"]:
            f_poster = item.get("fanart_poster_path")
            f_fanart = item.get("fanart_fanart_path")
            f_clearlogo = item.get("fanart_clearlogo_path")
            
            if f_poster:
                url_poster = get_asset_url(f_poster)
                formatted["poster_path"] = url_poster
                formatted["thumbnail_path"] = url_poster
            if f_fanart:
                url_fanart = get_asset_url(f_fanart)
                formatted["fanart_path"] = url_fanart
                formatted["landscape_path"] = url_fanart
            if f_clearlogo:
                formatted["clearlogo_path"] = get_asset_url(f_clearlogo)
    except Exception as e:
        log(f"Error overriding fanart in format_movie: {e}")

    return formatted
