from __future__ import annotations
import sqlite3
import requests
import time # Assuming time is needed for rate limiting
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING
import json


class TMDbAPI:
    TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p" # Make this a class constant

    def __init__(self, api_key, static_db_path=None): # Add static_db_path if this class will manage DB
        self.api_key = api_key
        self.base_url = "https://api.themoviedb.org/3"
        self.session = requests.Session() # Session for connection pooling
        self.static_db_path = static_db_path # For DB operations

    def _build_url(self, path, size='w500'):
        """Helper to build image URLs."""
        return f"{self.TMDB_IMAGE_BASE}/{size}{path}" if path else None

    def _get(self, path, params=None):
        """Internal helper for making GET requests to TMDb."""
        if params is None:
            params = {}
        params["api_key"] = self.api_key
        try:
            response = self.session.get(f"{self.base_url}{path}", params=params)
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            return response.json()
        except requests.exceptions.RequestException as e:
            log(f"[TMDbAPI] Request failed for {path}: {e}", level=LOGERROR)
            return None # Or raise a custom exception

    def _post(self, path, payload=None, params=None):
        """Internal helper for making POST requests to TMDb."""
        if params is None:
            params = {}
        params["api_key"] = self.api_key
        try:
            response = self.session.post(f"{self.base_url}{path}", json=payload, params=params)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            log(f"[TMDbAPI] POST Request failed for {path}: {e}", level=LOGERROR)
            return None


    def discover_media(self, item_type, params=None):
        """
        Discovers movies or TV shows based on filters.
        :param item_type: 'movie' or 'tv'
        :param params: Dictionary of filter parameters for the discover API.
        :return: JSON response from TMDb or None on failure.
        """
        return self._get(f"/discover/{item_type}", params=params)

    def get_watch_providers(self, media_type: str, region: str | None = None):
        """
        Returns the full watch-provider catalogue for *media_type* ('movie' or 'tv').
        Optionally scoped to *region* (ISO-3166-1 alpha-2, e.g. 'AU').
        :return: JSON response dict with a 'results' list, or None on failure.
        """
        params = {"language": "en-US"}
        if region:
            params["watch_region"] = region
        return self._get(f"/watch/providers/{media_type}", params=params)
    
    def _process_image_data(self, image_data, show_main_data=None):
        """
        Processes image data from a TMDb response to extract various image URLs.
        Can use main show data for fallbacks if available.
        """
        try:
            if not image_data:
                return {}

            # Use main show data for poster/fanart if available, otherwise image_data might have it.
            poster_path = show_main_data.get("poster_path") if show_main_data else None
            fanart_path = show_main_data.get("backdrop_path") if show_main_data else None

            result = {
                "poster": self._build_url(poster_path, "w780"),
                "fanart": self._build_url(fanart_path, "w1280"),
                "thumb": None,
                "clearlogo": None,
                "landscape": None,
            }

            # Find the best poster if not already set
            if not result["poster"] and image_data.get("posters"):
                result["poster"] = self._build_url(image_data["posters"][0].get("file_path"), "w780")

            # Find the best backdrop for fanart and landscape
            backdrops = image_data.get("backdrops", [])
            if backdrops:
                result["fanart"] = self._build_url(backdrops[0].get("file_path"), "w1280")
                result["landscape"] = self._build_url(backdrops[0].get("file_path"), "w1280")
                result["thumb"] = self._build_url(backdrops[0].get("file_path"), "w780")

            # ClearLogo
            for logo in image_data.get("logos", []):
                if logo.get("iso_639_1") in ("en", None):
                    result["clearlogo"] = self._build_url(logo["file_path"], "w500")
                    break

            return result
        except Exception as e:
            log(f"[TMDbAPI] Failed to process image data: {e}", level=LOGERROR)
            log(f"[TMDbAPI] Failed to process image data: {e}", level=LOGERROR)
            return {}

    def find_by_external_id(self, external_id, source="imdb_id"):
        """
        Finds a movie or TV show by an external ID (imdb_id, tvdb_id, etc.).
        Supported sources: imdb_id, freebase_mid, freebase_id, tvdb_id, tvrage_id, facebook_id, twitter_id, instagram_id
        """
        response = self._get(f"/find/{external_id}", params={"external_source": source})
        if response:
            # Check movie results first
            if response.get("movie_results"):
                return response["movie_results"][0]
            # Then TV results
            if response.get("tv_results"):
                return response["tv_results"][0]
        return None

    def get_show_images_from_data(self, show_data):
        """Extracts and processes image data from a full show details response."""
        return self._process_image_data(show_data.get("images", {}), show_main_data=show_data)

    def get_show_images(self, tmdb_id):
        """Fetches and processes images for a show directly from the /images endpoint."""
        image_data = self._get(f"/tv/{tmdb_id}/images")
        return self._process_image_data(image_data)

    def update_show_images(self, trakt_id, tmdb_id, tvshows_static_cursor):
        """Updates show image paths in the database."""

        images = self.get_show_images(tmdb_id)
        if not images:
            return

        try:
            tvshows_static_cursor.execute("""
                UPDATE shows
                SET poster_path = ?, fanart_path = ?, thumbnail_path = ?, clearlogo_path = ?, landscape_path = ?
                WHERE show_trakt_id = ?
            """, (
                images.get("poster"),
                images.get("fanart"),
                images.get("thumb"),
                images.get("clearlogo"),
                images.get("landscape"),
                trakt_id
            ))
            tvshows_static_cursor.connection.commit()
        except sqlite3.Error as e:
            log(f"[TMDbAPI] Database error updating show images for Trakt ID {trakt_id}: {e}", level=LOGERROR)

    def get_season_images_from_data(self, season_data, show_data):
        """Extracts image URLs for a season from pre-fetched data."""
        # A season's primary image is its poster. Other art types can fallback to the show's art.
        return {
            "poster": self._build_url(season_data.get("poster_path"), "w780"),
            "fanart": self._build_url(show_data.get("backdrop_path"), "w1280"),
            "thumb": self._build_url(season_data.get("poster_path"), "w780"),
            "landscape": self._build_url(show_data.get("backdrop_path"), "w1280"),
        }

    def get_episode_images_from_data(self, episode_data, show_data):
        """Extracts image URLs for an episode from pre-fetched data."""
        # An episode's primary image is its still (thumbnail). Others fallback to show art.
        return {
            "poster": self._build_url(show_data.get("poster_path"), "w780"),
            "fanart": self._build_url(show_data.get("backdrop_path"), "w1280"),
            "thumb": self._build_url(episode_data.get("still_path"), "w780"),
            "landscape": self._build_url(show_data.get("backdrop_path"), "w1280"),
        }

    def get_reviews(self, tmdb_id, media_type='movie', max_reviews=20):
        """
        Fetches up to max_reviews reviews for a movie or TV show from TMDB.
        media_type: 'movie' or 'tv'
        Returns a list of formatted text strings suitable for the Extras reviews panel.
        """
        reviews = []
        page = 1
        endpoint_type = 'tv' if media_type in ('tvshow', 'tv', 'episode') else 'movie'
        while len(reviews) < max_reviews:
            data = self._get(f'/{endpoint_type}/{tmdb_id}/reviews', params={'page': page})
            if not data:
                break
            results = data.get('results', [])
            if not results:
                break
            for r in results:
                if len(reviews) >= max_reviews:
                    break
                author = r.get('author') or 'Unknown'
                content = r.get('content') or ''
                rating = (r.get('author_details') or {}).get('rating')
                rating_str = '  [B]Rating: %s/10[/B]' % rating if rating else ''
                text = '[B]%s[/B]%s[CR][CR]%s' % (author, rating_str, content)
                reviews.append(text)
            total_pages = data.get('total_pages', 1)
            if page >= total_pages:
                break
            page += 1
        return reviews

    def get_movie_details_from_tmdb(self, tmdb_id):
        """Fetches movie release year from TMDb."""
        data = self._get(f"/movie/{tmdb_id}?append_to_response=images,release_dates")
        if data:
            belongs_to = data.get("belongs_to_collection")
            log(f"[TMDbAPI] Fetching movie details for TMDb ID {tmdb_id} - Belongs to Collection: {belongs_to}", level=LOGINFO)
            release_date = data.get("release_date", "")
            year = int(release_date[:4]) if release_date else None
            # Landscape (reuse a backdrop)

            images = {
                "poster": self._build_url(data.get("poster_path"), "w780"),
                "fanart": self._build_url(data.get("backdrop_path"), "w1280"),
                "thumb": None,
                "clearlogo": None,
                "landscape": None,
            }
            
            movie_images_data = data.get("images", {})
            backdrops = movie_images_data.get("backdrops", [])
            if backdrops:
                images["landscape"] = self._build_url(backdrops[0]["file_path"], "w1280")
                images["thumb"] = self._build_url(backdrops[0]["file_path"], "w780")  # Reuse the first backdrop as thumb
            elif data.get("poster_path"):
                # If no backdrops, use poster as fallback for thumb
                images["thumb"] = self._build_url(data.get("poster_path"), "w780")

            # ClearLogo
            for logo in movie_images_data.get("logos", []):
                if logo.get("iso_639_1") in ("en", None):
                    images["clearlogo"] = self._build_url(logo["file_path"], "w500")
                    break

            # Production companies(aka studios)
            studio_list = []
            production_companies = data.get("production_companies", [])
            if production_companies:
                studio_list = [company["name"] for company in production_companies]
            
            # Certification(MPAA)
            certification = ""
            release_dates = data.get("release_dates", {}).get("results", [])
            for country_release in release_dates:
                if country_release.get("iso_3166_1") == "US":
                    for release in country_release.get("release_dates", []):
                        if release.get("certification"):
                            certification = release.get("certification")
                            break
                    break

            details = {
                "year": year,
                "belongs_to": belongs_to,
                "images": images,
                "studios": studio_list,
                "overview": data.get("overview", ""),
                "tagline": data.get("tagline", ""),
                "rating": data.get("vote_average", 0.0),
                "votes": data.get("vote_count", 0), # Not in DB but good to have in return
                "runtime": data.get("runtime", 0),
                "country": data.get("origin_country", [""])[0] if data.get("origin_country") else "",
                "released": release_date.replace("-", "") if release_date else 0, # Storing as integer YYYYMMDD usually? Or is DB integer just year?
                # DB schema says 'released INTEGER'. Usually standard is YYYYMMDD or unix timestamp.
                # Let's check db_init: "released INTEGER". 
                # In flixpatrol_sync it uses `released = movie.get("released", "")` (string) then insert `released`.
                # If DB column is INTEGER, string "" becomes 0.
                # Let's use string format YYYYMMDD converted to int if possible or just 0.
                # For safety let's assume it wants YYYYMMDD integer.
                "original_title": data.get("original_title", ""),
                "certification": certification,
                "title": data.get("title", ""),
                "imdb_id": data.get("imdb_id", ""),
                "genres": data.get("genres", [])
            }

            return True, details
        return False, None


    def update_movie_static_data_from_tmdb(self, trakt_id, tmdb_id, static_db_cursor):
        """Updates movie static data (e.g., year) in the database."""
        found, details = self.get_movie_details_from_tmdb(tmdb_id)
        if not found:
            log(f"[TMDbAPI] No data found for TMDb ID {tmdb_id}", level=LOGWARNING)
            return False
            
        belongs_to = details.get("belongs_to")
        # Serialize the dictionary to a JSON string
        if belongs_to is not None:
            serialized_belongs_to = json.dumps(belongs_to)
        else:
            serialized_belongs_to = None # Store NULL in DB if no collection
        
        images = details.get("images", {})
        
        # Parse 'released' to int if it's a date string
        released_val = 0
        r_str = str(details.get("released", ""))
        if len(r_str) >= 8 and r_str.isdigit():
             released_val = int(r_str[:8])
        elif len(r_str) == 4 and r_str.isdigit():
             released_val = int(r_str) * 10000 + 101 # YYYY0101
        
        static_db_cursor.execute("""
            UPDATE movies
            SET belongs_to_collection = ?, poster_path = ?, fanart_path = ?, thumbnail_path = ?, landscape_path = ?, clearlogo_path = ?, studio = ?,
                overview = ?, tagline = ?, rating = ?, runtime = ?, country = ?, released = ?, certification = ?, original_title = ?, imdb_id = ?,
                title = ?, year = ?
            WHERE tmdb_id = ?
        """, (
            serialized_belongs_to, 
            images.get("poster"), 
            images.get("fanart"), 
            images.get("thumb"), 
            images.get("landscape"), 
            images.get("clearlogo"),
            json.dumps(details.get("studios", [])),
            details.get("overview", ""),
            details.get("tagline", ""),
            details.get("rating", 0.0),
            details.get("runtime", 0),
            details.get("country", ""),
            released_val,
            details.get("certification", ""),
            details.get("original_title", ""),
            details.get("imdb_id", ""),
            details.get("title", ""),
            details.get("year", 0),
            tmdb_id
        ))
        
        # Update Genres
        genres = details.get("genres", [])
        for genre_info in genres:
            # TMDB returns genres as list of dicts [{'id': 28, 'name': 'Action'}, ...] 
            # OR sometimes just names depending on how get_movie_details parses it.
            # let's check get_movie_details_from_tmdb... it sets "genres": data.get("genres", [])
            # TMDB API returns [{'id':.., 'name':...}]
            
            genre_name = genre_info.get("name")
            if genre_name:
                # Normalize genre name to slug format (lowercase, hyphenated) to avoid duplicates
                # e.g. "Science Fiction" -> "science-fiction"
                genre_name = genre_name.lower().replace(" ", "-")
                static_db_cursor.execute("INSERT OR IGNORE INTO genres(name) VALUES(?)", (genre_name,))
                safe_trakt_id = trakt_id if trakt_id is not None else -tmdb_id
                static_db_cursor.execute("INSERT OR IGNORE INTO movie_genres(trakt_id, tmdb_id, genre) VALUES(?, ?, ?)", (safe_trakt_id, tmdb_id, genre_name))

        static_db_cursor.connection.commit()
        return True


    def get_full_show_details(self, tmdb_id):
        """
        Fetches comprehensive details for a show, including all seasons, episodes, and images,
        in an optimized way using `append_to_response`.
        """
        if not tmdb_id:
            return None

        # Step 1: Get basic show info to find out what seasons exist.
        initial_show_info = self._get(f"/tv/{tmdb_id}")
        if not initial_show_info:
            log(f"[TMDbAPI] Could not fetch initial data for show TMDb ID {tmdb_id}", level=LOGERROR)
            return None

        season_numbers = [
            s.get("season_number")
            for s in initial_show_info.get("seasons", [])
            if s.get("season_number") is not None  # Include Season 0 (Specials)
        ]

        # Step 2: Build the append_to_response string for all seasons and images.
        # TMDb has a limit of 20 appends per request. We need to chunk if necessary.
        appends = ["images"]
        appends.extend([f"season/{s_num}" for s_num in season_numbers])

        chunk_size = 20
        append_chunks = [appends[i:i + chunk_size] for i in range(0, len(appends), chunk_size)]

        full_details = {}
        for i, chunk in enumerate(append_chunks):
            append_string = ",".join(chunk)
            params = {"append_to_response": append_string}
            chunk_data = self._get(f"/tv/{tmdb_id}", params=params)

            if not chunk_data:
                log(f"[TMDbAPI] Failed to fetch chunk {i+1} for show TMDb ID {tmdb_id}", level=LOGWARNING)
                continue

            if not full_details:
                full_details = chunk_data
            else:
                # Merge the new data into the main dictionary
                full_details.update(chunk_data)

        return full_details

    def get_seasons_and_episodes_from_full_data(self, full_data):
        """
        Extracts a list of season dictionaries from the comprehensive data
        fetched by get_full_show_details.
        """
        if not full_data or 'seasons' not in full_data:
            return []

        season_details = []
        for season_stub in full_data.get("seasons", []):
            season_number = season_stub.get("season_number")
            season_key = f"season/{season_number}"
            if season_key in full_data:
                season_details.append(full_data[season_key])

        return season_details

    def search(self, item_type, query, page=1):
        """Searches for items by name."""
        params = {"query": query, "include_adult": True, "language": "en-US", "page": page}
        if item_type == "movie":
            return self._get("/search/movie", params=params)
        elif item_type == "tv_show":
            return self._get("/search/tv", params=params)
        else:
            return self._get("/search/multi", params=params)

    def get_keywords(self, keyword, item_type='movie'):
        """Fetches keywords for a movie or TV show."""
        data = self._get(f"/search/keyword", params={"query": keyword, "page": 1})

        if data and 'results' in data:
            return [kw['name'] for kw in data['results']]
        return []

    def get_account_details(self, session_id):
        """Fetches account details using session_id."""
        return self._get("/account", params={"session_id": session_id})

    def get_created_lists(self, account_id, session_id, page=1):
        """Fetches lists created by the user."""
        return self._get(f"/account/{account_id}/lists", params={"session_id": session_id, "page": page})

    def get_watchlist_movies(self, account_id, session_id, page=1):
        """Fetches user's movie watchlist."""
        return self._get(f"/account/{account_id}/watchlist/movies", params={"session_id": session_id, "page": page, "sort_by": "created_at.desc"})

    def get_watchlist_shows(self, account_id, session_id, page=1):
        """Fetches user's TV show watchlist."""
        return self._get(f"/account/{account_id}/watchlist/tv", params={"session_id": session_id, "page": page, "sort_by": "created_at.desc"})

    def get_list_details(self, list_id, session_id=None, page=1):
        """Fetches details of a specific list."""
        params = {"page": page}
        if session_id:
            params["session_id"] = session_id
        return self._get(f"/list/{list_id}", params=params)

    def get_trending_movies(self, page=1):
        """Fetches trending movies for the week."""
        return self._get("/trending/movie/week", params={"page": page})

    def get_trending_shows(self, page=1):
        """Fetches trending TV shows for the week."""
        return self._get("/trending/tv/week", params={"page": page})

    def get_recommendations(self, media_type, media_id, page=1):
        """Fetches recommendations for a specific movie or TV show."""
        return self._get(f"/{media_type}/{media_id}/recommendations", params={"page": page})

    def add_to_watchlist(self, account_id, session_id, media_type, media_id):
        """Adds an item to the TMDB watchlist."""
        path = f"/account/{account_id}/watchlist"
        payload = {
            "media_type": media_type,
            "media_id": media_id,
            "watchlist": True
        }
        return self._post(path, payload=payload, params={"session_id": session_id})

    def remove_from_watchlist(self, account_id, session_id, media_type, media_id):
        """Removes an item from the TMDB watchlist."""
        path = f"/account/{account_id}/watchlist"
        payload = {
            "media_type": media_type,
            "media_id": media_id,
            "watchlist": False
        }
        return self._post(path, payload=payload, params={"session_id": session_id})

    def add_to_list(self, list_id, session_id, media_id, media_type="movie"):
        """Adds an item to a custom TMDB list (v3)."""
        path = f"/list/{list_id}/add_item"
        payload = {
            "media_id": media_id,
            "media_type": media_type
        }
        return self._post(path, payload=payload, params={"session_id": session_id})

    def remove_from_list(self, list_id, session_id, media_id, media_type="movie"):
        """Removes an item from a custom TMDB list (v3)."""
        path = f"/list/{list_id}/remove_item"
        payload = {
            "media_id": media_id,
            "media_type": media_type
        }
        return self._post(path, payload=payload, params={"session_id": session_id})
    def discover_media(self, media_type, params=None):
        """Discovers media based on filters."""
        if params is None:
            params = {}
        return self._get(f"/discover/{media_type}", params=params)

    def get_tv_changes(self, start_date=None, page=1):
        """Fetches a list of TV IDs that have changed since start_date."""
        params = {"page": page}
        if start_date:
            params["start_date"] = start_date
        return self._get("/tv/changes", params=params)
