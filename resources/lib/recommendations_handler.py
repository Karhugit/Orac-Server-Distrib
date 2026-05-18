import asyncio
import time
import json
import sqlite3
import random
from collections import defaultdict, Counter
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING

class VisualDiscoveryEngine:
    def __init__(self, movies_dynamic_db_path, movies_static_db_path, tmdb_handler):
        self.movies_dynamic_db_path = movies_dynamic_db_path
        self.movies_static_db_path = movies_static_db_path
        self.tmdb_handler = tmdb_handler
        # Ensure tmdb_handler has get_movie_details or uses consistent naming
        if not hasattr(self.tmdb_handler, 'get_movie_details') and hasattr(self.tmdb_handler, 'get_movie_details_from_tmdb'):
            self.tmdb_handler.get_movie_details = self.tmdb_handler.get_movie_details_from_tmdb
        self.cache = {}  # Simple in-memory cache: {user_key: (timestamp, data)}
        self.CACHE_TTL = 86400  # 24 hours in seconds

    def _get_db_connection(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_user_history(self, user):
        """
        Fetches user's watched movie history.
        Returns a list of dicts: [{'tmdb_id': 123, 'rating': 8, 'timestamp': 123456789}]
        """
        history = []
        try:
            with self._get_db_connection(self.movies_dynamic_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT tmdb_id, user_rating, last_updated 
                    FROM movie_status 
                    WHERE watched_status > 0
                """)
                rows = cursor.fetchall()
                for row in rows:
                    history.append({
                        'tmdb_id': row['tmdb_id'],
                        'rating': row['user_rating'] or 0,
                        'timestamp': row['last_updated'] or 0
                    })
        except Exception as e:
            log(f"[DiscoveryEngine] Error fetching history: {e}", level=LOGERROR)
        return history

    def _get_movie_genres(self, tmdb_ids):
        """
        Fetches genres for a list of movie IDs from the static DB.
        Returns a dict: {tmdb_id: [genre1, genre2]}
        """
        movie_genres = defaultdict(list)
        if not tmdb_ids:
            return movie_genres
            
        try:
            with self._get_db_connection(self.movies_static_db_path) as conn:
                cursor = conn.cursor()
                placeholders = ','.join(['?'] * len(tmdb_ids))
                cursor.execute(f"SELECT tmdb_id, genre FROM movie_genres WHERE tmdb_id IN ({placeholders})", tmdb_ids)
                for row in cursor.fetchall():
                    movie_genres[row['tmdb_id']].append(row['genre'])
        except Exception as e:
            log(f"[DiscoveryEngine] Error fetching genres: {e}", level=LOGERROR)
        return movie_genres

    def _calculate_preferences(self, history):
        """
        Builds a genre weight profile and selects seed movies.
        """
        genre_weights = Counter()
        tmdb_ids = [h['tmdb_id'] for h in history]
        movie_genres = self._get_movie_genres(tmdb_ids)
        
        now = time.time()
        thirty_days = 30 * 24 * 3600
        
        seed_candidates = []

        for item in history:
            tmdb_id = item['tmdb_id']
            rating = item['rating']
            timestamp = item['timestamp']
            
            # Simple rating threshold
            if rating > 0 and rating < 4: # Assuming 1-10 scale, skip low rated? Or 1-5?
                 # If rating is 10-based (Trakt), < 7 might be 'meh'.
                 # If rating is 0 (unrated), assume neutral/positive if watched fully?
                 # Let's assume watched items are positive signals unless rated low.
                 pass

            genres = movie_genres.get(tmdb_id, [])
            
            weight = 1.0
            # Recency boost
            try:
                if now - float(timestamp) < thirty_days:
                    weight = 1.5
            except (ValueError, TypeError):
                # If timestamp is invalid, ignore recency boost
                pass
            
            for genre in genres:
                genre_weights[genre] += weight
                
            # Seed candidates: high rated or recently watched
            # We want 5 most recently watched "highly rated" movies.
            # If no rating, treat as high if recent.
            try:
                score = float(timestamp)
            except (ValueError, TypeError):
                score = 0.0

            if rating >= 7: # High rating bonus for seed selection
                score += thirty_days 
            
            seed_candidates.append({'tmdb_id': tmdb_id, 'score': score})
            
        # Select Seeds
        seed_candidates.sort(key=lambda x: x['score'], reverse=True)
        # Use more seeds for hydration/discovery to get a better genre spread
        seeds = [s['tmdb_id'] for s in seed_candidates[:20]]
        
        # Select Top Genres
        top_genres = [g for g, count in genre_weights.most_common()]
        
        # Map genre names to IDs if TMDB needs IDs? 
        # TMDB discover uses genre IDs. We store genre IDs?
        # db_init says: 'genre' in movie_genres is TEXT. Is it Name or ID?
        # Table 'genres' has 'name TEXT PRIMARY KEY'. So we store names.
        # We need a mapping from Name to TMDB Genre ID.
        # Hardcoding common ones or fetching? 
        # Better to fetch from TMDB /genre/movie/list if we don't have it, or mapped manually.
        # For this implementation, let's assume we can get IDs from a helper or fetch parameters.
        # Wait, the prompt says "genre_id: weight". 
        # If our DB stores Names, we need to convert.
        # Let's make an async call to get genre list from TMDB to map names to IDs.
        
        return genre_weights, top_genres, seeds

    async def _get_tmdb_genre_map(self):
        # Helper to get Name -> ID map
        # Ideally cached.
        data = self.tmdb_handler._get("/genre/movie/list")
        if data and 'genres' in data:
            # unique map, slug keys for matching DB format (science-fiction)
            return {g['name'].lower().replace(" ", "-"): g['id'] for g in data['genres']}
        return {}

    async def generate_recommendations(self, user):
        # Check Cache
        if user in self.cache:
            ts, cached_data = self.cache[user]
            if time.time() - ts < self.CACHE_TTL:
                log(f"[DiscoveryEngine] Returning cached recommendations for {user}", level=LOGINFO)
                return cached_data

        log(f"[DiscoveryEngine] Generating new recommendations for {user}", level=LOGINFO)
        
        history = self._get_user_history(user)
        if not history:
            # Cold start? Return generic trending?
            log("[DiscoveryEngine] No history found, returning trending.", level=LOGWARNING)
            trending = self.tmdb_handler.get_trending_movies()
            return {'top_picks': trending.get('results', [])[:10], 'genre_shelves': []}

        genre_weights, top_genres, seeds = self._calculate_preferences(history)
        
        # Map Genre Names to IDs
        genre_map = await self._get_tmdb_genre_map()
        
        # Hydration: If we have seeds but no top_genres (missing local metadata), fetch details for seeds
        if not top_genres and seeds:
            log("[DiscoveryEngine] Missing local metadata for genres. Hydrating from seeds...", level=LOGINFO)
            hydration_tasks = []
            for tmdb_id in seeds:
                hydration_tasks.append(self._fetch_wrapper(self.tmdb_handler.get_movie_details_from_tmdb, tmdb_id))
            
            hydration_results = await asyncio.gather(*hydration_tasks)
            
            hydrated_genre_counts = Counter()
            for res in hydration_results:
                if not res or not res.get('data'): continue
                success, movie_details = res.get('data')
                if not success or not movie_details: continue
                
                for g in movie_details.get('genres', []):
                    # g is {'id': 12, 'name': 'Adventure'}
                    slug = g['name'].lower().replace(" ", "-")
                    hydrated_genre_counts[slug] += 1
                    # Ensure genre_map has this genre
                    if slug not in genre_map:
                        genre_map[slug] = g['id']
            
            top_genres = [g for g, count in hydrated_genre_counts.most_common(3)]
            log(f"[DiscoveryEngine] Hydrated top genres: {top_genres}", level=LOGINFO)

        top_genre_ids = [genre_map.get(g.lower()) for g in top_genres if g.lower() in genre_map]
        
        log(f"[DiscoveryEngine] Top Genres: {top_genres} -> IDs: {top_genre_ids}", level=LOGINFO)
        log(f"[DiscoveryEngine] Seeds (TMDB IDs): {seeds}", level=LOGINFO)

        tasks = []
        
        # 1. Broad Discovery (Top 3 Genres)
        for gid in top_genre_ids:
            params = {
                'vote_count.gte': 150,
                'include_adult': 'true',
                'with_genres': gid,
                'sort_by': 'popularity.desc'
            }
            log(f"[DiscoveryEngine] Discovering for Genre ID {gid} with params: {params}", level=LOGINFO)
            tasks.append(self._fetch_wrapper(self.tmdb_handler.discover_media, 'movie', params, context={'type': 'genre', 'id': gid}))
            
        # 2. Deep Similarity (Seeds)
        for tmdb_id in seeds:
            tasks.append(self._fetch_wrapper(self.tmdb_handler.get_recommendations, 'movie', tmdb_id, context={'type': 'seed', 'id': tmdb_id}))

        results = await asyncio.gather(*tasks)
        
        # Processing & Scoring
        pool = {} # {tmdb_id: {'score': 0, 'data': movie_data}}
        watched_ids = {h['tmdb_id'] for h in history}
        
        for res in results:
            if not res: continue
            data = res.get('data', {}).get('results', [])
            context = res.get('context')
            
            for movie in data:
                tmdb_id = movie.get('id')
                if not tmdb_id: continue
                # Strict Filter: Watched
                if tmdb_id in watched_ids:
                    continue
                
                if tmdb_id not in pool:
                    pool[tmdb_id] = {'score': 0, 'data': movie, 'sources': []}
                
                # Base Logic
                pool[tmdb_id]['score'] += 1.0 # Base existence
                pool[tmdb_id]['sources'].append(context)
                
                # Occurrence Bonus
                # If already in pool (checked above, but technically we iterate), add bonus?
                # Actually we just added it or updated it. 
                # If len(sources) > 1, it means it appeared multiple times.
                
        # Calculate final scores
        final_list = []
        for tmdb_id, item in pool.items():
            score = item['score']
            movie = item['data']
            
            # Occurrence Bonus
            if len(item['sources']) > 1:
                score += 2.0 * (len(item['sources']) - 1)
            
            # Genre Alignment
            movie_genre_ids = movie.get('genre_ids', [])
            for mgid in movie_genre_ids:
                # Find name
                # Reverse lookup or just check if it matches top preference?
                pass
                # Simplified: if matches Top Genres, bonus
                if mgid in top_genre_ids:
                     score += 1.5

            item['final_score'] = score
            final_list.append(item)
            
        # Sort by Score
        final_list.sort(key=lambda x: x['final_score'], reverse=True)
        final_list = final_list[:200]
        
        # Hydrate runtime from local database
        local_runtimes = {}
        try:
            with self._get_db_connection(self.movies_static_db_path) as conn:
                cursor = conn.cursor()
                pool_ids = [int(item['data'].get('id')) for item in final_list if item['data'].get('id')]
                if pool_ids:
                    placeholders = ','.join(['?'] * len(pool_ids))
                    cursor.execute(f"SELECT tmdb_id, runtime FROM movies WHERE tmdb_id IN ({placeholders})", pool_ids)
                    for row in cursor.fetchall():
                        local_runtimes[int(row['tmdb_id'])] = row['runtime']
        except Exception as e:
            log(f"[DiscoveryEngine] Error fetching local runtimes: {e}", level=LOGERROR)
            
        for item in final_list:
            movie = item['data']
            tmdb_id = movie.get('id')
            if tmdb_id and int(tmdb_id) in local_runtimes and local_runtimes[int(tmdb_id)]:
                movie['runtime'] = local_runtimes[int(tmdb_id)]
        
        top_picks = [x['data'] for x in final_list[:100]]
        
        # Genre Shelves
        # "Because you watch [Genre]"
        genre_shelves = []
        for genre_name in top_genres:
            gid = genre_map.get(genre_name)
            if not gid: continue
            
            # Filter pool for this genre
            shelf_items = []
            for item in final_list:
                if gid in item['data'].get('genre_ids', []):
                    shelf_items.append(item['data'])
                    
            if shelf_items:
                # Convert slug to Title Case for display: "science-fiction" -> "Science Fiction"
                display_title = genre_name.replace("-", " ").title()
                genre_shelves.append({
                    'title': display_title,
                    'items': shelf_items[:100]
                })

        # Apply formatting after building shelves so we can access genre_ids properly
        from resources.lib.formatting_utils import format_movie
        if self.tmdb_handler:
            top_picks = [format_movie(movie, self.tmdb_handler) for movie in top_picks]
            for shelf in genre_shelves:
                shelf['items'] = [format_movie(movie, self.tmdb_handler) for movie in shelf['items']]

        output = {
            'top_picks': top_picks,
            'genre_shelves': genre_shelves
        }
        
        # Update Cache
        self.cache[user] = (time.time(), output)
        
        return output

    async def _fetch_wrapper(self, func, *args, context=None, **kwargs):
        # Helper to run blocking requests in executor if needed, but tmdb_handler._get seems synchronous?
        # If tmdb_handler uses `requests` it is blocking.
        # We should run it in loop info executor.
        loop = asyncio.get_running_loop()
        try:
             # func might be bound method, pass *args
             # But run_in_executor needs a callable.
             # functools.partial?
            from functools import partial
            p = partial(func, *args, **kwargs)
            data = await loop.run_in_executor(None, p)
            return {'data': data, 'context': context}
        except Exception as e:
            log(f"[DiscoveryEngine] API Call failed: {e}", level=LOGWARNING)
            return None

def handle_movie_recommendations(user, movies_dynamic_db_path, movies_static_db_path, tmdb_handler):
    # This function is the entry point for http_server
    # Typically we might instantiate the engine globally or per request. 
    # For caching, global is better.
    # Where to store the engine instance? 
    # Can stick it on `http_server` instance if passed, or use a singleton pattern.
    pass
    # For now, let's create a singleton-like instance at module level (simple) or pass it around.
    # Since http_server imports this module, we can have a global instance here.
    
_engine_instance = None

async def get_recommendations_async(user, movies_dynamic_db_path, movies_static_db_path, tmdb_handler):
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = VisualDiscoveryEngine(movies_dynamic_db_path, movies_static_db_path, tmdb_handler)
    
    return await _engine_instance.generate_recommendations(user)
    
    return await _engine_instance.generate_recommendations(user)

def clear_user_cache(user):
    global _engine_instance
    if _engine_instance and user in _engine_instance.cache:
        del _engine_instance.cache[user]
        log(f"[DiscoveryEngine] Cleared recommendations cache for {user}", level=LOGINFO)
