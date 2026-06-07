import requests
import re
import asyncio
from resources.lib.log_utils import log, LOGINFO, LOGERROR, LOGDEBUG, LOGWARNING
import sqlite3
import time
import urllib.parse
from resources.lib.trakt_list_sync import add_movie
from resources.lib.db_utils import add_tvshow
from datetime import datetime

# FlixPatrol URL
FLIXPATROL_URL = "https://flixpatrol.com/"

# Providers to look for (Name mapped to internal slug/name)
# The keys match the case-sensitive headers in HTML
PROVIDERS = {
    "Netflix": "netflix",
    "HBO Max": "hbo-max",
    "Disney+": "disney-plus",
    "Amazon": "amazon-prime",
    "Apple TV": "apple-tv",
    "Paramount+": "paramount-plus",
    "Hulu": "hulu",
    "Peacock": "peacock",
    "Google": "google-play"
}

class FlixPatrolSync:
    def __init__(self, tmdb_handler, lists_db_path, movies_static_db_path, movies_dynamic_db_path, tvshows_static_db_path, tvshows_dynamic_db_path, trakt_update_queue_path):
        self.tmdb_handler = tmdb_handler
        self.lists_db_path = lists_db_path
        self.movies_static_db_path = movies_static_db_path
        self.movies_dynamic_db_path = movies_dynamic_db_path
        self.tvshows_static_db_path = tvshows_static_db_path
        self.tvshows_dynamic_db_path = tvshows_dynamic_db_path
        self.trakt_update_queue_path = trakt_update_queue_path

    async def run_sync(self):
        log("[Orac] Starting FlixPatrol sync...", level=LOGINFO)
        try:
            html = await self._fetch_html()
            if not html:
                log("[Orac] Failed to fetch FlixPatrol HTML. Aborting.", level=LOGERROR)
                return

            extracted_lists = self._parse_html(html)
            
            if not extracted_lists:
                log("[Orac] No lists extracted from FlixPatrol.", level=LOGWARNING)
                return

            log(f"[Orac] Extracted {len(extracted_lists)} lists from FlixPatrol. Starting processing...", level=LOGINFO)
            await self._process_lists(extracted_lists)
            log("[Orac] FlixPatrol sync completed.", level=LOGINFO)

        except Exception as e:
            log(f"[Orac] FlixPatrol sync failed: {e}", level=LOGERROR)

    async def _fetch_html(self):
        try:
            return await asyncio.to_thread(self._fetch_html_blocking)
        except Exception as e:
            log(f"[Orac] Error fetching content: {e}", level=LOGERROR)
            return None

    def _fetch_html_blocking(self):
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(FLIXPATROL_URL, headers=headers, timeout=15)
        response.raise_for_status()
        return response.text

    def _parse_html(self, html):
        """
        Parses HTML to find lists.
        Returns a list of dicts: {'provider': 'netflix', 'type': 'movie', 'items': [titles]}
        """
        lists = []
        
        # Split HTML by "Top Movies" and "Top TV Shows" markers if possible, or just sequential scan.
        # Based on inspection:
        # 1. "Top Movies" -> <h2>Provider</h2> -> items
        # 2. "Top TV Shows" -> <h2>Provider</h2> -> items
        
        # Regex to find blocks:
        # Match 'Top (Movies|TV Shows)' ... '<h2>(Provider)</h2>' ... items
        
        # We'll just iterate through providers and look for their specific blocks using the structure found.
        # Structure: <div ..>Top {Type}</div> <h2 ...>{Provider}</h2> ... <div class="card-body ..."> ... items ... </div>
        
        # We can find all occurrences of the provider header and check strictly what precedes it.
        
        for provider_name, slug in PROVIDERS.items():
            # Find all start indices of the provider header
            # Header pattern: <h2[^>]*>\s*ProviderName\s*</h2>
            header_pattern = re.compile(f'<h2[^>]*>\\s*{re.escape(provider_name)}\\s*</h2>', re.IGNORECASE)
            
            for match in header_pattern.finditer(html):
                header_start = match.start()
                header_end = match.end()
                
                # Check preceding context for "Top Movies" or "Top TV Shows"
                # Look back approx 300 chars
                context_start = max(0, header_start - 300)
                preceding_text = html[context_start:header_start]
                
                list_type = None
                if "Top Movies" in preceding_text:
                    list_type = 'movie'
                elif "Top TV Shows" in preceding_text:
                    list_type = 'tvshow'
                
                if not list_type:
                    continue
                    
                # Extract items following the header
                # Look for the card-body
                # <div class="card-body p-0 group">
                
                # Find the next card-body after header_end
                card_body_start_match = re.search(r'<div class="card-body[^"]*p-0 group">', html[header_end:])
                if not card_body_start_match:
                    continue
                
                start_index = header_end + card_body_start_match.end()
                
                # Extract until next 'card-body' or end of container (heuristic: assume list is reasonable length)
                # We can just extract all links with title attributes until a new h2 or div that looks like a header starts.
                # Or just grab the next 5000 chars and parse items.
                
                chunk = html[start_index:start_index+10000] # Enough for 10 items
                
                # Terminate chunk at next "Top " or "<h2>" to avoid bleeding into next list
                split_match = re.search(r'(<h2|Top (Movies|TV Shows))', chunk)
                if split_match:
                    chunk = chunk[:split_match.start()]
                
                # Extract items
                # We'll match all anchor tags in the chunk, then filter for those with href="/title/..."
                
                # Check for two common orders: href...title or title...href
                p1 = r'<a[^>]+href=["\']/title/([^/\s"\']+)/?["\'][^>]+title=["\']([^"\']+)["\']'
                p2 = r'<a[^>]+title=["\']([^"\']+)["\'][^>]+href=["\']/title/([^/\s"\']+)/?["\']'
                
                items_p1 = re.findall(p1, chunk)
                items_p2 = re.findall(p2, chunk)
                
                raw_items = []
                for item_slug, item_title in items_p1:
                    raw_items.append((item_title, item_slug))
                for item_title, item_slug in items_p2:
                    raw_items.append((item_title, item_slug))
                
                # Distinct and preserve order
                clean_items = []
                seen = set()
                for title, item_slug in raw_items:
                    # HTML unescape just in case
                    title = title.replace('&amp;', '&').replace('&#39;', "'").replace('&apos;', "'")
                    if title not in seen:
                        # Extract year from slug
                        year = None
                        year_match = re.search(r'-(\d{4})$', item_slug)
                        if year_match:
                            year = int(year_match.group(1))
                        clean_items.append({'title': title, 'year': year})
                        seen.add(title)
                        if len(clean_items) >= 10: # Limit to top 10
                            break
                            
                if clean_items:
                    lists.append({
                        'provider': slug,
                        'provider_name': provider_name,
                        'type': list_type,
                        'items': clean_items
                    })

        return lists

    async def _process_lists(self, lists):
        # Open DB connection for lists only (Main Thread)
        lists_conn = sqlite3.connect(self.lists_db_path)
        lists_conn.row_factory = sqlite3.Row
        
        try:
            lists_cursor = lists_conn.cursor()
            timestamp = int(time.time())
            
            for lst in lists:
                provider_slug = lst['provider']
                provider_name = lst['provider_name']
                list_type = lst['type']
                items = lst['items']
                
                # Construct unique list slug
                list_slug = f"flixpatrol-{provider_slug}-{list_type}"
                list_name = f"{provider_name} Top 10 {'Movies' if list_type == 'movie' else 'TV Shows'}"
                
                log(f"[Orac] Processing list: {list_name} ({len(items)} items)", level=LOGDEBUG)
                
                # 1. Create/Update List Entry
                # 1. Create/Update List Entry
                lists_cursor.execute("SELECT list_id, add_to_library FROM lists WHERE slug = ?", (list_slug,))
                row = lists_cursor.fetchone()
                
                # Use strict list_id matching the slug for internal consistency
                list_id = f"web:chart:{list_slug}"

                if row:
                    existing_list_id = row[0]
                    add_to_library = row[1]
                    # Update (using existing ID found in DB, though it should be same as list_slug if we fixed it)
                    # If we have legacy NULL ID rows, we should probably fix them or just update using slug?
                    # Let's enforce the ID update too just in case it was NULL
                    lists_cursor.execute("""
                        UPDATE lists SET 
                        list_id=?, name=?, description=?, item_count_movies=?, item_count_shows=?, source=?, last_checked=?
                        WHERE slug=?
                    """, (
                        list_id,
                        list_name, 
                        f"Top 10 {list_type}s on {provider_name} from FlixPatrol",
                        len(items) if list_type == 'movie' else 0,
                        len(items) if list_type == 'tvshow' else 0,
                        'web',
                        datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        list_slug
                    ))
                else:
                    lists_cursor.execute("""
                        INSERT INTO lists 
                        (list_id, slug, name, user, description, item_count_movies, item_count_shows, source, add_to_library, last_checked)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        list_id,
                        list_slug,
                        list_name,
                        'flixpatrol',
                        f"Top 10 {list_type}s on {provider_name} from FlixPatrol",
                        len(items) if list_type == 'movie' else 0,
                        len(items) if list_type == 'tvshow' else 0,
                        'web',
                        1,
                        datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
                    ))
                    # list_id is already set above
                    
                # 2. Clear old items for this list
                lists_cursor.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
                
                # 3. Resolve and Insert Items
                for item in items:
                    title = item['title']
                    year = item.get('year')
                    tmdb_id, info = await self._resolve_title(title, list_type, year=year)
                    
                    if tmdb_id:
                        # Add to Metadata Database First (Thread-Safe)
                        if list_type == 'movie':
                            movie_item = {
                                "ids": {"trakt": None, "tmdb": tmdb_id},
                                "title": info.get("title"),
                                "year": int(info.get("release_date", "")[:4]) if info.get("release_date") else None
                            }
                            media_id = {"trakt": None, "tmdb": tmdb_id}
                            
                            await asyncio.to_thread(self._add_movie_safe, movie_item, media_id)
                            
                        elif list_type == 'tvshow':
                            show_item = {
                                "ids": {"trakt": None, "tmdb": tmdb_id, "slug": ""},
                                "title": info.get("name"),
                                "year": int(info.get("first_air_date", "")[:4]) if info.get("first_air_date") else None
                            }
                            media_id = {"tmdb": tmdb_id}
                            
                            await asyncio.to_thread(self._add_show_safe, show_item, media_id)

                        # Insert into list items
                        # Use negative TMDB ID as placeholder for Trakt ID to ensure uniqueness
                        # The system convention (seen in db_init.py) handles negative Trakt IDs as TMDB placeholders
                        lists_cursor.execute("""
                            INSERT OR IGNORE INTO list_items (list_id, media_type, tmdb_id, trakt_id)
                            VALUES (?, ?, ?, ?)
                        """, (
                            list_id, 
                            'movie' if list_type == 'movie' else 'show',
                            tmdb_id,
                            -tmdb_id 
                        ))
                    else:
                        log(f"[Orac] Could not resolve '{title}' to a TMDB ID.", level=LOGWARNING)

            lists_conn.commit()

        except Exception as e:
            log(f"[Orac] Error processing FlixPatrol lists: {e}", level=LOGERROR)
            import traceback
            log(traceback.format_exc(), level=LOGERROR)
            
        finally:
            if lists_conn: lists_conn.close()

    def _add_movie_safe(self, movie_item, media_id):
        # Open new connections for this thread
        with sqlite3.connect(self.movies_static_db_path, timeout=10) as s_conn, \
             sqlite3.connect(self.movies_dynamic_db_path, timeout=10) as d_conn:
            add_movie(s_conn.cursor(), d_conn.cursor(), movie_item, media_id, self.tmdb_handler)
            s_conn.commit()
            d_conn.commit()

    def _add_show_safe(self, show_item, media_id):
        # Open new connections for this thread
        with sqlite3.connect(self.tvshows_static_db_path, timeout=10) as s_conn, \
             sqlite3.connect(self.tvshows_dynamic_db_path, timeout=10) as d_conn, \
             sqlite3.connect(self.trakt_update_queue_path, timeout=10) as q_conn:
            add_tvshow(
                s_conn.cursor(), 
                d_conn.cursor(), 
                q_conn.cursor(), 
                media_id, 
                None, # trakt_handler
                self.tmdb_handler, 
                show_item
            )
            s_conn.commit()
            d_conn.commit()
            q_conn.commit()

    async def _resolve_title(self, title, item_type, year=None):
        """
        Searches TMDB for the title and returns the best match ID.
        """
        search_type = 'movie' if item_type == 'movie' else 'tv'
        
        # Use simple caching or just hit the API? 
        # TMDB handler likely has caching if request-based.
        
        try:
            # Call tmdb_handler._get directly
            params = {'query': title, 'language': 'en-US', 'page': 1}
            # Add year? FlixPatrol sometimes has (Year). 
            # Our regex extraction `title="Title"` usually doesn't have year.
            
            # Check if title has year: "Title (2025)"
            year_match = re.search(r'\((\d{4})\)$', title)
            if year_match:
                year = int(year_match.group(1))
                # Remove year from query
                params['query'] = title.replace(f" ({year})", "").strip()
            
            if year:
                params['year'] = str(year)
                if search_type == 'movie':
                    params['primary_release_year'] = str(year)
                else:
                    params['first_air_date_year'] = str(year)
            
            # Wrap blocking call
            response = await asyncio.to_thread(self.tmdb_handler._get, f"/search/{search_type}", params)
            
            if response and response.get('results'):
                # Take first result (Top match)
                first = response['results'][0]
                return first['id'], first
                
        except Exception as e:
            log(f"[Orac] Error searching TMDB for '{title}': {e}", level=LOGERROR)
            
        return None, None

