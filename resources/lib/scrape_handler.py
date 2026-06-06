import asyncio
import json
import sqlite3
import math
import time
import re
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

async def handle_scrape_request(query, scraper_manager, movies_db, tvshows_db, tmdb_handler=None, results_limit=0, global_loop=None):
    """
    Handles a scrape request, enriching metadata if needed and optionally using racing mode.
    """
    tmdb_id = query.get("tmdb_id", [None])[0]
    item_type = query.get("item_type", ["movie"])[0]
    provider = query.get("provider", [None])[0] or query.get("scraper", [None])[0]
    search_type = query.get("search_type", ["sources"])[0]
    strict_dedupe = query.get("strict_dedupe", ["false"])[0].lower() == 'true'
    orac_scraping_pref = query.get("orac_scraping", ["true"])[0].lower() == 'true'
    use_aiostreams_pref = query.get("use_aiostreams", ["true"])[0].lower() == 'true'

    if not tmdb_id:
        return 400, json.dumps({"success": False, "error": "tmdb_id is required"}), "application/json"

    # 1. Metadata Enrichment
    scrape_data = _flatten_query_params(query)
    
    # If missing title/year/imdb, OR if it's an episode and missing show title or show IMDB, try to fetch from local DB
    is_episode = (item_type == "episode")
    needs_enrichment = not scrape_data.get("title") or not scrape_data.get("year")
    if is_episode and (not scrape_data.get("tvshowtitle") or not scrape_data.get("show_imdb")):
        needs_enrichment = True

    if needs_enrichment:
         # Try local DB first
         db_data = _get_metadata_from_db(tmdb_id, item_type, movies_db, tvshows_db)
         if db_data:
             log(f"[ScrapeHandler] Enriched metadata from DB for TMDB ID {tmdb_id}: {db_data.get('title') or db_data.get('tvshowtitle')}", level=LOGINFO)
             # Update scrape_data with db_data, prioritizing query params if they exist
             for k, v in db_data.items():
                 if not scrape_data.get(k):
                     scrape_data[k] = v
             # Special case for imdb_id: if we found a show IMDB, make sure it's in scrape_data['imdb']
             if db_data.get('imdb_id'):
                 scrape_data['show_imdb'] = db_data['imdb_id']
         
         # Fallback to TMDb API if still missing critical data and we have a handler
         if tmdb_handler:
             still_needs = not scrape_data.get("title") and not scrape_data.get("tvshowtitle")
             if is_episode and not scrape_data.get("show_imdb"): still_needs = True
             
             if still_needs:
                 log(f"[ScrapeHandler] Metadata still missing for {tmdb_id}, trying TMDb API fallback...", level=LOGINFO)
                 tmdb_data = _get_metadata_from_tmdb(tmdb_id, item_type, tmdb_handler)
                 if tmdb_data:
                     log(f"[ScrapeHandler] Enriched metadata from TMDb API for {tmdb_id}.", level=LOGINFO)
                     for k, v in tmdb_data.items():
                         if not scrape_data.get(k):
                             scrape_data[k] = v
                     if is_episode and tmdb_data.get('imdb_id'):
                         scrape_data['show_imdb'] = tmdb_data['imdb_id']

    # Ensure 'imdb' and 'tmdb_id' keys exist for scrapers as strings.
    for key in ["imdb", "imdb_id", "tmdb_id", "episode_imdb", "show_imdb"]:
        val = scrape_data.get(key)
        if isinstance(val, list) and len(val) > 0:
            scrape_data[key] = str(val[0])
        elif val is not None:
            scrape_data[key] = str(val)

    if is_episode:
        # Priority for episode_imdb: scrape_data > query > None
        if not scrape_data.get("episode_imdb"):
            if scrape_data.get("imdb_id") and scrape_data.get("show_imdb") and scrape_data.get("imdb_id") != scrape_data.get("show_imdb"):
                scrape_data["episode_imdb"] = scrape_data["imdb_id"]
            else:
                scrape_data["episode_imdb"] = scrape_data.get("imdb_id") if not scrape_data.get("show_imdb") else None
            
        show_imdb = scrape_data.get("show_imdb") or scrape_data.get("imdb_id")
        scrape_data["show_imdb"] = show_imdb
        # Traditionally, many scrapers expect 'imdb' to be the Show ID for TV searches
        scrape_data["imdb"] = show_imdb
    elif scrape_data.get("imdb_id") and not scrape_data.get("imdb"):
        scrape_data["imdb"] = scrape_data["imdb_id"]

    if not scrape_data.get("title") and not scrape_data.get("tvshowtitle"):
        return 404, json.dumps({"success": False, "error": f"No media data found for tmdb_id {tmdb_id}"}), "application/json"

    # 2. Scraper Execution
    if provider:
        primary_providers = [{"name": provider, "active": 1, "total_scrapes": 0}]
        background_providers = []
    else:
        primary_providers, background_providers = scraper_manager.get_partitioned_providers()
        
        # Apply client-specified scraper preferences
        if not orac_scraping_pref and not use_aiostreams_pref:
            # Both disabled -> return no results
            primary_providers = []
            background_providers = []
        elif not orac_scraping_pref and use_aiostreams_pref:
            # Standalone AIOStreams mode: find AIOStreams, force active, and make it the ONLY primary
            aiostreams_provider = None
            for p in primary_providers + background_providers:
                if p["name"] == "aiostreams":
                    aiostreams_provider = p.copy()
                    break
            if not aiostreams_provider:
                aiostreams_provider = {"name": "aiostreams", "active": 1, "score": 0.0, "total_scrapes": 0}
            
            aiostreams_provider["active"] = 1
            primary_providers = [aiostreams_provider]
            background_providers = []
        elif orac_scraping_pref and not use_aiostreams_pref:
            # Scrapers ON, AIOStreams OFF: Exclude AIOStreams completely
            primary_providers = [p for p in primary_providers if p["name"] != "aiostreams"]
            background_providers = [p for p in background_providers if p["name"] != "aiostreams"]
    
    # Extract names for the stats keys
    primary_names = [p['name'] for p in primary_providers]
    
    # phase 1: Primary (Exploit) - results returned to user
    results = []
    primary_stats = {p: {"results": [], "winner": False, "time_seconds": 1.0} for p in primary_names}
    
    try:
        if results_limit > 0 and len(primary_providers) > 1:
            log(f"[ScrapeHandler] Racing primary providers (limit {results_limit}): {primary_names}", level=LOGINFO)
            await asyncio.wait_for(
                _run_scrapers_racing_shared(scraper_manager, primary_providers, scrape_data, search_type, results_limit, query, results, primary_stats),
                timeout=25.0
            )
        else:
            log(f"[ScrapeHandler] Standard primary providers: {primary_names}", level=LOGINFO)
            await asyncio.wait_for(
                _run_scrapers_standard_shared(scraper_manager, primary_providers, scrape_data, search_type, query, results, primary_stats),
                timeout=25.0
            )
    except asyncio.TimeoutError:
        log(f"[ScrapeHandler] Global scrape timeout reached (25s). Found {len(results)} results so far.", level=LOGWARNING)

    # Update scores for primary providers (if we have any stats)
    if primary_stats:
        _update_scraper_scores(scraper_manager.db, primary_stats, is_primary_batch=True)

    # Phase 2: Background (Explore) - run remaining scrapers for score updates
    if background_providers:
        if not results:
            log(f"[ScrapeHandler] No results from primary. Running background scrapers synchronously: {[p['name'] for p in background_providers]}", level=LOGINFO)
            await _run_exploration(scraper_manager, background_providers, scrape_data, search_type, query, shared_results=results)
        else:
            log(f"[ScrapeHandler] Starting background exploration for: {[p['name'] for p in background_providers]}", level=LOGDEBUG)
            # Fire and forget
            asyncio.create_task(_run_exploration(scraper_manager, background_providers, scrape_data, search_type, query))

    # 3. Post-processing (Deduplicate and Sort)
    final_results = _process_results(results, strict_dedupe)
    
    if results_limit > 0:
        final_results = final_results[:results_limit]

    response_payload = {
        "success": True, 
        "results": final_results, 
        "count": len(final_results),
        "enriched": bool(tmdb_id and not query.get("name"))
    }
    return 200, json.dumps(response_payload), "application/json"

async def _run_scrapers_standard_shared(scraper_manager, providers, data, search_type, query, shared_results, shared_stats):
    tasks = []
    for p_data in providers:
        name = p_data['name']
        is_active = p_data.get('active', 1) == 1
        total_scrapes = p_data.get('total_scrapes', 0)


        async def timed_scrape(n):
            start = time.time()
            try:
                log(f"[ScrapeHandler] Actually starting network request for '{n}'", level=LOGDEBUG)
                # Per-scraper timeout of 5 seconds
                res = await asyncio.wait_for(
                    scraper_manager.scrape_async(
                        n, data, search_type=search_type,
                        total_seasons=query.get('total_seasons', [None])[0],
                        bypass_filter=query.get('bypass_filter', ['false'])[0].lower() == 'true'
                    ),
                    timeout=5.0
                )
                duration = time.time() - start
                if res:
                    shared_results.extend(res)
                shared_stats[n]["results"] = res
                shared_stats[n]["time_seconds"] = duration
                return True
            except asyncio.TimeoutError:
                log(f"[ScrapeHandler] Scraper '{n}' timed out after 5s", level=LOGWARNING)
                shared_stats[n]["time_seconds"] = 5.0
                return False
            except Exception as e:
                log(f"[ScrapeHandler] Scraper '{n}' error: {e}", level=LOGERROR)
                shared_stats[n]["time_seconds"] = time.time() - start
                return False

        shared_stats[name] = {"results": [], "winner": False, "time_seconds": 1.0}
        tasks.append(asyncio.create_task(timed_scrape(name)))
    
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

async def _run_scrapers_racing_shared(scraper_manager, providers, data, search_type, limit, query, shared_results, shared_stats):
    tasks = {}
    for p_data in providers:
        name = p_data['name']
        is_active = p_data.get('active', 1) == 1
        total_scrapes = p_data.get('total_scrapes', 0)


        async def timed_scrape(n):
            start = time.time()
            try:
                log(f"[ScrapeHandler] Actually starting network request for '{n}' (racing)", level=LOGDEBUG)
                res = await asyncio.wait_for(
                    scraper_manager.scrape_async(
                        n, data, search_type=search_type,
                        total_seasons=query.get('total_seasons', [None])[0],
                        bypass_filter=query.get('bypass_filter', ['false'])[0].lower() == 'true'
                    ),
                    timeout=5.0
                )
                return n, res, time.time() - start
            except asyncio.TimeoutError:
                return n, [], 5.0
            except Exception as e:
                return n, [], time.time() - start

        shared_stats[name] = {"results": [], "winner": False, "time_seconds": 1.0}
        tasks[name] = asyncio.create_task(timed_scrape(name))
    
    winner_found = False
    for task_future in asyncio.as_completed(tasks.values()):
        try:
            name, res, duration = await task_future
            shared_stats[name]["results"] = res
            shared_stats[name]["time_seconds"] = duration

            if res:
                shared_results.extend(res)
                if not winner_found:
                    shared_stats[name]["winner"] = True
                    winner_found = True

                if len(shared_results) >= limit:
                    log(f"[ScrapeHandler] Racing: reached limit of {limit} results.", level=LOGINFO)
                    for t in tasks.values():
                        if not t.done(): t.cancel()
                    break
        except Exception as e:
            log(f"[ScrapeHandler] Racing task error: {e}", level=LOGERROR)

def _process_results(results, strict_dedupe=False):
    """Deduplicates and sorts results."""
    unique_sources = []
    seen_hashes = set()
    
    # Strict Dedupe Setup
    canonical_seen = set()
    
    for source in results:
        # Initial Hash-based dedupe (always on)
        if isinstance(source, dict) and 'hash' in source:
            if source['hash'] in seen_hashes:
                continue
            seen_hashes.add(source['hash'])
            
            # Strict Dedupe Logic
            if strict_dedupe and 'name' in source:
                try:
                    # 1. Lowercase
                    name = source['name'].lower()
                    
                    # 2. Strip suffixes
                    suffixes = ['.eztv', '.rarbg', '.jff', '.flux', '.rawr', '.fenix', '.mkv', '.mp4', '.avi']
                    for suffix in suffixes:
                        if name.endswith(suffix):
                            name = name[:-len(suffix)]
                            
                    # 3. Normalize punctuation
                    # Replace multiple dots with single dot
                    name = re.sub(r'\.+', '.', name)
                    # Replace . - or .- with -
                    name = name.replace('. -', '-').replace('.-', '-')
                    # Replace _ with .
                    name = name.replace('_', '.')
                    
                    # 4. Identify release group (last token after hyphen)
                    if '-' in name:
                        parts = name.rsplit('-', 1)
                        base_name = parts[0]
                        group = parts[1]
                    else:
                        base_name = name
                        group = "unknown"
                        
                    # 5. Construct canonical key
                    # canonical = f"{show}.{season_episode}.{quality}.{source}.{codec}-{group}"
                    # We accept that different trackers might have the same release. We want to dedupe identical releases.
                    # Actually, if we want to dedupe the SAME release from DIFFERENT sources/trackers, we need a key that describes the RELEASE itself.
                    # The user's requested key format: canonical = f"{show}.{season_episode}.{quality}.{source}.{codec}-{group}"
                    # BUT we don't have parsed fields for all of these reliably in 'source' dict at this stage without re-parsing.
                    # The user's instructions say:
                    # "Step 5 — Extract the “base name”. The base name is everything up to the release group: the.curse.of.oak.island.s13e11.1080p.web.h264"
                    # "Step 6 — Construct a canonical key: canonical = f"{show}.{season_episode}.{quality}.{source}.{codec}-{group}""
                    # Wait, if step 5 extracts the base name, then step 6 seems to IMPLY that the base name CONTAINS those parts.
                    # So essentially, Key = BaseName + Group.
                    
                    canonical_key = f"{base_name}-{group}-{source.get('quality', 'unknown')}"
                    
                    if canonical_key in canonical_seen:
                        continue # Skip duplicate
                    
                    canonical_seen.add(canonical_key)
                    
                except Exception as e:
                    # Logic failure shouldn't stop scraping
                    pass

        unique_sources.append(source)

    quality_order = {'4K': 0, '1080p': 1, '720p': 2, 'SD': 3, 'SCR': 4, 'CAM': 5}
    sorted_sources = sorted(
        unique_sources,
        key=lambda s: (
            quality_order.get(s.get('quality'), 99), 
            -s.get('seeders', 0)
        ) if isinstance(s, dict) else (99, 0)
    )
    return sorted_sources

def _get_metadata_from_db(tmdb_id, item_type, movies_db, tvshows_db):
    """Fetches title/year/imdb from local DB."""
    try:
        if item_type == 'movie':
            with sqlite3.connect(movies_db) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT title, year, imdb_id FROM movies WHERE tmdb_id = ?", (tmdb_id,))
                row = cursor.fetchone()
                if row:
                    return {"title": row["title"], "year": str(row["year"]), "imdb_id": row["imdb_id"], "aliases": []}
        elif item_type == 'episode':
            with sqlite3.connect(tvshows_db) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # 1. Try finding as a Show ID first
                cursor.execute("""
                    SELECT s.title as tvshowtitle, s.year, s.imdb_id, s.show_trakt_id
                    FROM shows s
                    WHERE s.show_tmdb_id = ?
                """, (tmdb_id,))
                row = cursor.fetchone()
                
                # 2. If not found, check if it's an Episode ID and get the parent show details
                if not row:
                    cursor.execute("""
                        SELECT s.title as tvshowtitle, s.year, s.imdb_id as show_imdb, s.show_trakt_id, 
                               e.season, e.episode_number, e.episode_title as title, e.imdb_id as episode_imdb
                        FROM episodes e
                        JOIN shows s ON e.show_id = s.show_trakt_id
                        WHERE e.tmdb_id = ?
                    """, (tmdb_id,))
                    row = cursor.fetchone()
                
                if row:
                    data = dict(row)
                    data['aliases'] = []
                    if 'title' not in data: data['title'] = ""
                    if 'show_imdb' not in data and 'imdb_id' in data: data['show_imdb'] = data['imdb_id']
                    if 'episode_imdb' not in data: data['episode_imdb'] = ""
                    # Convert season/episode to strings if they were found via episode lookup
                    if 'season' in data: data['season'] = str(data['season'])
                    if 'episode_number' in data: 
                        data['episode'] = str(data['episode_number'])
                        del data['episode_number']
                    return data
    except Exception as e:
        log(f"[ScrapeHandler] DB metadata fetch failed: {e}", level=LOGERROR)
    return None

def _get_metadata_from_tmdb(tmdb_id, item_type, tmdb_handler):
    """Fetches title/year/imdb from TMDb API."""
    try:
        if item_type == 'movie':
            success, details = tmdb_handler.get_movie_details_from_tmdb(tmdb_id)
            if success:
                return {
                    "title": details.get("title"),
                    "year": str(details.get("year")) if details.get("year") else "",
                    "imdb_id": details.get("imdb_id"),
                    "aliases": []
                }
        elif item_type == 'episode':
            # For episodes, we really need the SHOW details for scraping
            # tmdb_id might be show_id or episode_id
            # Try fetching as show first with external_ids appended
            show_data = tmdb_handler._get(f"/tv/{tmdb_id}", params={"append_to_response": "external_ids"})
            if show_data and "name" in show_data:
                 show_imdb_id = show_data.get("external_ids", {}).get("imdb_id", "")
                 return {
                     "tvshowtitle": show_data.get("name"),
                     "title": "", # Unknown episode title at this stage
                     "year": show_data.get("first_air_date", "")[:4],
                     "imdb_id": show_imdb_id,
                     "show_imdb": show_imdb_id,
                     "aliases": []
                 }
            else:
                # Might be an episode ID, try fetching episode details
                # But TMDb needs show_id/season/episode to get episode details via /tv/{show_id}/season/{s}/episode/{e}
                # If we only have an episode's TMDB ID, we can use /find/{id}?external_source=tmdb_id
                # but /find only works for external IDs like IMDB.
                # For TMDB ID of an episode, we can try /episode/{id} but that's not a standard TMDb route.
                # Actually, there is NO direct way to get show_id from episode_tmdb_id without search or find
                # unless we have it in our DB.
                pass
                
    except Exception as e:
        log(f"[ScrapeHandler] TMDb API metadata fetch failed: {e}", level=LOGERROR)
    return None

def _update_scraper_scores(db, stats, is_primary_batch=False):
    """
    Updates scraper scores using the user's weighted formula and moving average.
    Also manages 'active' status based on results.
    """
    for name, data in stats.items():
        time_seconds = data.get("time_seconds", 1.0)
        results = data.get("results", [])
        
        # Aggregate quality counts
        quality_counts = {'4k': 0, '1080': 0, '720': 0, 'sd': 0}
        for r in results:
            if not isinstance(r, dict): continue
            q = r.get("quality", "SD").lower()
            if '4k' in q: quality_counts['4k'] += 1
            elif '1080' in q: quality_counts['1080'] += 1
            elif '720' in q: quality_counts['720'] += 1
            else: quality_counts['sd'] += 1
            
        # 1. Calculate result score
        weights = {'4k': 10, '1080': 5, '720': 3, 'sd': 1}
        result_score = (
            quality_counts['4k'] * weights['4k'] +
            quality_counts['1080'] * weights['1080'] +
            quality_counts['720'] * weights['720'] +
            quality_counts['sd'] * weights['sd']
        )
        
        # 2. Check for zero results - assign zero score regardless of speed
        if len(results) == 0:
            session_score = 0
        else:
            # 3. Logarithmic time score (only for successful scrapes)
            time_score = 100 / math.log(1 + time_seconds)
            
            # 4. Weighted final session score
            session_score = (0.95 * result_score) + (0.05 * time_score)

            # 5. Low result penalty: halve the score if results < 4 (1, 2, or 3)
            if 0 < len(results) < 4:
                session_score *= 0.5
        
        # If it was skipped, increment count only to avoid tanking the score
        if data.get('skipped'):
            db.increment_scrape_count(name)
            log(f"[ScrapeHandler] {name} SKIPPED (Count Incremented)", level=LOGINFO)
            continue

        # Scraper Health Tracking:
        # 1. If any scraper returns results, ensure it's set to active.
        #    NOTE: set_active_status resets stats if transitioning from inactive to active.
        if len(results) > 0:
            db.set_active_status(name, True)

        # Update DB (uses moving average logic internally)
        db.update_score(name, session_score)
        
        # 2. If a primary scraper returns ZERO results, DEACTIVATE it.
        #    BUT only if ANY scraper in this batch found results.
        #    If everyone found nothing, we assume there are no torrents for this content.
        #    AND it wasn't cancelled (winner found elsewhere or global timeout)
        #    AND it wasn't skipped.
        batch_has_any_results = any(len(s.get("results", [])) > 0 for s in stats.values())
        if is_primary_batch and len(results) == 0 and not data.get('skipped') and batch_has_any_results:
            log(f"[ScrapeHandler] {name} returned 0 results (while others found some) as primary. Deactivating.", level=LOGWARNING)
            db.set_active_status(name, False)
        elif is_primary_batch and len(results) == 0 and not batch_has_any_results:
             log(f"[ScrapeHandler] {name} returned 0 results but no other scraper found results either. Keeping active.", level=LOGDEBUG)

        log(f"[ScrapeHandler] {name} Score: {session_score:.2f} (Time: {time_seconds:.2f}s, Results: {len(results)})", level=LOGINFO)

async def _run_exploration(scraper_manager, providers, data, search_type, query, shared_results=None):
    """Background task to update scores for non-primary scrapers. If shared_results is provided, they are enriched."""
    try:
        results = []
        provider_names = [p['name'] for p in providers]
        stats = {name: {"results": [], "winner": False, "time_seconds": 1.0} for name in provider_names}
        await _run_scrapers_standard_shared(scraper_manager, providers, data, search_type, query, results, stats)
        _update_scraper_scores(scraper_manager.db, stats, is_primary_batch=False)
        if shared_results is not None:
            shared_results.extend(results)
    except Exception as e:
        log(f"[ScrapeHandler] Background exploration failed: {e}", level=LOGERROR)

def _flatten_query_params(query):
    """Converts a query dict from parse_qs to a flat dict."""
    flat_params = {}
    # Map common fields
    mapping = {
        "name": "title",
        "tmdb_id": "tmdb_id",
        "item_type": "item_type",
        "season": "season",
        "episode": "episode",
        "imdb_id": "imdb_id",
        "tvshowtitle": "tvshowtitle"
    }
    
    for k, v in query.items():
        if not v: continue
        target_key = mapping.get(k, k)
        if target_key == 'aliases' and v[0]:
            try:
                aliases_raw = json.loads(v[0])
                if isinstance(aliases_raw, list):
                    flat_params[target_key] = [a.get('title', a) if isinstance(a, dict) else a for a in aliases_raw]
                else:
                    flat_params[target_key] = []
            except:
                flat_params[target_key] = []
        else:
            flat_params[target_key] = v[0]
            
    if 'aliases' not in flat_params:
        flat_params['aliases'] = []
    
    if 'title' not in flat_params:
        flat_params['title'] = "" # Guarantee title exists
            
    return flat_params
