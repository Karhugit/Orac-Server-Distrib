import sqlite3
import threading
import requests
import asyncio
from datetime import datetime
from resources.lib.db_utils import db_connect
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGDEBUG, LOGWARNING
from resources.lib.simkl_api import get_simkl_params, get_simkl_headers, simkl_get, simkl_post

_sync_engine_lock = threading.Lock()


def _parse_timestamp(ts_str):
    if not ts_str:
        return None
    try:
        clean = ts_str.split(".")[0].replace("Z", "")
        return datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
    except Exception as e:
        log(f"[Sync Engine] Error parsing timestamp {ts_str}: {e}", level=LOGDEBUG)
        return None


def fetch_simkl_history(config_db_path, tvshows_static_db=None, force=False):
    """
    Fetches watched movies and episodes from Simkl.
    Uses /sync/activities and date_from to perform continuous incremental sync per
    https://api.simkl.org/guides/sync#phase-2-continuous-sync.
    """
    from resources.lib.config_handler import get_config_value
    from resources.lib.simkl_api import ensure_valid_simkl_token, get_effective_simkl_client_id
    token = ensure_valid_simkl_token(config_db_path) or get_config_value("simkl.token", config_db_path)
    client_id = get_effective_simkl_client_id(config_db_path)
    
    if not token or not client_id or token == "empty_setting" or client_id == "empty_setting":
        log("[Sync Engine] Missing Simkl credentials.", level=LOGINFO)
        return {"movies": {}, "shows": {}, "activities": {}}
        
    headers = get_simkl_headers(token=token, client_id=client_id)
    params = get_simkl_params(client_id)
    
    simkl_data = {"movies": {}, "shows": {}, "activities": {}}
    fetch_movies = True
    fetch_shows = True
    
    stored_last_sync = get_config_value("simkl_last_sync_at", config_db_path, "")
    if not stored_last_sync:
        stored_last_sync = get_config_value("simkl_tv_watching_synced_at", config_db_path, "")

    # 1. Activity check
    try:
        act_resp = simkl_get('https://api.simkl.com/sync/activities', params=params, headers=headers, timeout=15)
        if act_resp.status_code == 200:
            act_data = act_resp.json()
            simkl_data["activities"] = act_data
            act_all = act_data.get("all")
            
            if not force and config_db_path and stored_last_sync:
                if act_all and act_all == stored_last_sync:
                    log(f"[Sync Engine] Simkl watched history unchanged (watermark {stored_last_sync}) — skipping fetch.", level=LOGINFO)
                    return simkl_data

                stored_tv_watch = get_config_value("simkl_tv_watching_synced_at", config_db_path, "")
                stored_tv_comp = get_config_value("simkl_tv_completed_synced_at", config_db_path, "")
                stored_mov_comp = get_config_value("simkl_movies_completed_synced_at", config_db_path, "")
                
                curr_tv_watch = act_data.get("tv_shows", {}).get("watching", "") or ""
                curr_tv_comp = act_data.get("tv_shows", {}).get("completed", "") or ""
                curr_mov_comp = act_data.get("movies", {}).get("completed", "") or ""
                
                if curr_tv_watch and curr_tv_comp and curr_tv_watch <= stored_tv_watch and curr_tv_comp <= stored_tv_comp:
                    log("[Sync Engine] Simkl TV shows watched history unchanged — skipping fetch.", level=LOGINFO)
                    fetch_shows = False
                    
                if curr_mov_comp and curr_mov_comp <= stored_mov_comp:
                    log("[Sync Engine] Simkl movies watched history unchanged — skipping fetch.", level=LOGINFO)
                    fetch_movies = False
                    
                if not fetch_shows and not fetch_movies:
                    log(f"[Sync Engine] Simkl watched history unchanged — skipping fetch.", level=LOGINFO)
                    return simkl_data
    except Exception as e:
        log(f"[Sync Engine] Simkl activities check failed: {e}", level=LOGWARNING)
        
    # 2. Items fetch: Phase 1 Initial Sync vs Phase 2 Continuous Delta Sync
    try:
        if stored_last_sync and not force:
            extra_params = {
                "date_from": stored_last_sync,
                "extended": "full",
                "episode_watched_at": "yes"
            }
            log(f"[Sync Engine] Simkl continuous sync: fetching delta since {stored_last_sync}...", level=LOGINFO)
        else:
            extra_params = {
                "extended": "full",
                "episode_watched_at": "yes",
                "include_all_episodes": "yes"
            }
            log("[Sync Engine] Simkl initial/forced sync: fetching full library baseline...", level=LOGINFO)

        full_params = get_simkl_params(client_id, extra_params)
        resp = simkl_get('https://api.simkl.com/sync/all-items', params=full_params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        
        if fetch_movies:
            for movie in data.get('movies', []):
                if movie.get('status') == 'completed':
                    tmdb_id = movie.get('movie', {}).get('ids', {}).get('tmdb')
                    if tmdb_id:
                        watched_at = movie.get('last_watched_at') or movie.get('user_rated_at') or movie.get('added_to_watchlist_at') or now_iso
                        simkl_data["movies"][str(tmdb_id)] = watched_at
                        
        if fetch_shows:
            for show in data.get('shows', []):
                show_tmdb_id = show.get('show', {}).get('ids', {}).get('tmdb')
                if not show_tmdb_id:
                    continue
                show_tmdb_id_str = str(show_tmdb_id)
                show_status = show.get('status')
                show_last_watched = show.get('last_watched_at') or show.get('user_rated_at') or show.get('added_to_watchlist_at') or now_iso
                
                seasons = show.get('seasons', [])
                if seasons:
                    if show_tmdb_id_str not in simkl_data["shows"]:
                        simkl_data["shows"][show_tmdb_id_str] = {}
                    for season in seasons:
                        season_num = season.get('number')
                        if season_num is None:
                            continue
                        for ep in season.get('episodes', []):
                            ep_num = ep.get('number')
                            if ep_num is None:
                                continue
                            ep_time = ep.get('watched_at') or ep.get('last_watched_at') or show_last_watched
                            key = f"{season_num}_{ep_num}"
                            simkl_data["shows"][show_tmdb_id_str][key] = ep_time
                elif show_status == 'completed' and tvshows_static_db:
                    # Completed show where Simkl omitted individual seasons
                    try:
                        with db_connect(tvshows_static_db) as sconn:
                            scursor = sconn.cursor()
                            scursor.execute("SELECT season, episode_number FROM episodes WHERE show_id = ? AND season > 0", (int(show_tmdb_id),))
                            static_eps = scursor.fetchall()
                            if static_eps:
                                if show_tmdb_id_str not in simkl_data["shows"]:
                                    simkl_data["shows"][show_tmdb_id_str] = {}
                                for s_num, e_num in static_eps:
                                    key = f"{s_num}_{e_num}"
                                    simkl_data["shows"][show_tmdb_id_str][key] = show_last_watched
                    except Exception as e_stat:
                        log(f"[Sync Engine] Error expanding completed Simkl show {show_tmdb_id}: {e_stat}", level=LOGWARNING)

        log(f"[Sync Engine] Simkl fetched: {len(simkl_data['movies'])} movies, {len(simkl_data['shows'])} shows with episodes.", level=LOGINFO)

    except Exception as e:
        log(f"[Sync Engine] Simkl fetch error: {e}", level=LOGERROR)
         
    return simkl_data


def fetch_mdblist_history(config_db_path):
    """
    Fetches watched movies and episodes from MDBList.
    """
    from resources.lib.config_handler import get_config_value
    api_key = get_config_value("mdblist_api", config_db_path)
    if not api_key or api_key == "empty_setting":
        log("[Sync Engine] Missing MDBList API key.", level=LOGINFO)
        return {"movies": {}, "shows": {}}
        
    mdblist_data = {"movies": {}, "shows": {}}
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        url = f"https://api.mdblist.com/sync/watched?apikey={api_key}"
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        
        for item in data.get('movies', []):
            tmdb_id = item.get('movie', {}).get('ids', {}).get('tmdb')
            if tmdb_id:
                mdblist_data["movies"][str(tmdb_id)] = item.get('last_watched_at') or now_iso
                
        for item in data.get('episodes', []):
            ep_obj = item.get('episode', {})
            show_obj = ep_obj.get('show', {})
            show_tmdb_id = show_obj.get('ids', {}).get('tmdb')
            if not show_tmdb_id:
                continue
            
            season_num = ep_obj.get('season')
            ep_num = ep_obj.get('number')
            if season_num is not None and ep_num is not None:
                sid_str = str(show_tmdb_id)
                if sid_str not in mdblist_data["shows"]:
                    mdblist_data["shows"][sid_str] = {}
                key = f"{season_num}_{ep_num}"
                mdblist_data["shows"][sid_str][key] = item.get('last_watched_at') or now_iso

        log(f"[Sync Engine] MDBList fetched: {len(mdblist_data['movies'])} movies, {len(mdblist_data['shows'])} shows with episodes.", level=LOGINFO)

    except Exception as e:
        log(f"[Sync Engine] MDBList fetch error: {e}", level=LOGERROR)
         
    return mdblist_data

def reconcile_movies(db_path, trakt_data, simkl_data, mdblist_data, config_db_path=None):
    """
    Reconciles movies from all authorized providers into watched_history and movie_status.
    """
    log("[Sync Engine] Reconciling and flagging movies...", level=LOGINFO)
    from resources.lib.config_handler import get_authorized_watched_providers
    authed_providers = get_authorized_watched_providers(config_db_path) if config_db_path else ['trakt', 'simkl', 'mdblist']
    
    all_tmdb_ids = set(trakt_data.keys()).union(set(simkl_data.keys())).union(set(mdblist_data.keys()))
    if not all_tmdb_ids:
        log("[Sync Engine] No movies to reconcile.", level=LOGDEBUG)
        return
        
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to_update = []
    
    for tmdb_id in all_tmdb_ids:
        t_time_str = trakt_data.get(tmdb_id)
        s_time_str = simkl_data.get(tmdb_id)
        m_time_str = mdblist_data.get(tmdb_id)
        
        t_time = _parse_timestamp(t_time_str)
        s_time = _parse_timestamp(s_time_str)
        m_time = _parse_timestamp(m_time_str)
        
        winner_time_str = t_time_str
        winner_time = t_time
        
        if s_time and (not winner_time or s_time > winner_time):
            winner_time_str = s_time_str
            winner_time = s_time
            
        if m_time and (not winner_time or m_time > winner_time):
            winner_time_str = m_time_str
            winner_time = m_time
            
        if not winner_time_str:
            winner_time_str = now_str
                
        trakt_synced_at = now_str if t_time_str else (None if 'trakt' in authed_providers else 'unauthorized')
        simkl_synced_at = now_str if s_time_str else (None if 'simkl' in authed_providers else 'unauthorized')
        mdblist_synced_at = now_str if m_time_str else (None if 'mdblist' in authed_providers else 'unauthorized')
        
        to_update.append((
             int(tmdb_id), winner_time_str, trakt_synced_at, simkl_synced_at, mdblist_synced_at
        ))
        
    if to_update:
        try:
            with db_connect(db_path) as conn:
                query = """
                INSERT INTO watched_history (tmdb_id, is_watched, last_watched_at, trakt_synced_at, simkl_synced_at, mdblist_synced_at)
                VALUES (?, 1, ?, ?, ?, ?)
                ON CONFLICT(tmdb_id) DO UPDATE SET
                    is_watched = 1,
                    last_watched_at = CASE WHEN excluded.last_watched_at > watched_history.last_watched_at THEN excluded.last_watched_at ELSE watched_history.last_watched_at END,
                    trakt_synced_at = COALESCE(excluded.trakt_synced_at, watched_history.trakt_synced_at),
                    simkl_synced_at = COALESCE(excluded.simkl_synced_at, watched_history.simkl_synced_at),
                    mdblist_synced_at = COALESCE(excluded.mdblist_synced_at, watched_history.mdblist_synced_at)
                """
                conn.executemany(query, to_update)
                
                # Update movie_status in dynamic cache so local UI reflects watched status immediately
                movie_status_data = [(item[0], item[0], item[1]) for item in to_update]
                conn.executemany("""
                    INSERT OR REPLACE INTO movie_status (tmdb_id, trakt_id, watched, user_rating, last_updated, watched_status)
                    VALUES (?, (SELECT trakt_id FROM movie_status WHERE tmdb_id = ?), 100, NULL, ?, 2)
                """, movie_status_data)
                
                conn.commit()
                log(f"[Sync Engine] Successfully reconciled {len(to_update)} movies.", level=LOGINFO)
        except Exception as e:
            log(f"[Sync Engine] Error reconciling movies: {e}", level=LOGERROR)


def reconcile_shows(db_path, trakt_data, simkl_data, mdblist_data, config_db_path=None, tvshows_static_db=None):
    """
    Reconciles TV show episodes from all authorized providers into watched_history and watched_episodes.
    """
    log("[Sync Engine] Reconciling and flagging tv shows...", level=LOGINFO)
    from resources.lib.config_handler import get_authorized_watched_providers, get_config_value, get_trakt_user
    authed_providers = get_authorized_watched_providers(config_db_path) if config_db_path else ['trakt', 'simkl', 'mdblist']
    
    all_show_tmdb_ids = set(trakt_data.keys()).union(set(simkl_data.keys())).union(set(mdblist_data.keys()))
    if not all_show_tmdb_ids:
        log("[Sync Engine] No tv shows to reconcile.", level=LOGDEBUG)
        return
        
    to_update = []
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    for show_tmdb_id in all_show_tmdb_ids:
        t_eps = trakt_data.get(show_tmdb_id, {})
        s_eps = simkl_data.get(show_tmdb_id, {})
        m_eps = mdblist_data.get(show_tmdb_id, {})
        
        all_ep_keys = set(t_eps.keys()).union(set(s_eps.keys())).union(set(m_eps.keys()))
        for ep_key in all_ep_keys:
            try:
                season_num, ep_num = map(int, ep_key.split('_'))
            except (ValueError, TypeError):
                continue
            
            t_time_str = t_eps.get(ep_key)
            s_time_str = s_eps.get(ep_key)
            m_time_str = m_eps.get(ep_key)
            
            t_time = _parse_timestamp(t_time_str)
            s_time = _parse_timestamp(s_time_str)
            m_time = _parse_timestamp(m_time_str)
            
            winner_time_str = t_time_str
            winner_time = t_time
            
            if s_time and (not winner_time or s_time > winner_time):
                winner_time_str = s_time_str
                winner_time = s_time
                
            if m_time and (not winner_time or m_time > winner_time):
                winner_time_str = m_time_str
                winner_time = m_time
                
            if not winner_time_str:
                winner_time_str = now_str
                
            trakt_synced_at = now_str if t_time_str else (None if 'trakt' in authed_providers else 'unauthorized')
            simkl_synced_at = now_str if s_time_str else (None if 'simkl' in authed_providers else 'unauthorized')
            mdblist_synced_at = now_str if m_time_str else (None if 'mdblist' in authed_providers else 'unauthorized')
            
            to_update.append((
                 int(show_tmdb_id), season_num, ep_num, winner_time_str, trakt_synced_at, simkl_synced_at, mdblist_synced_at
            ))
            
    if to_update:
        try:
            with db_connect(db_path) as conn:
                query = """
                INSERT INTO watched_history (show_tmdb_id, season, episode, is_watched, last_watched_at, trakt_synced_at, simkl_synced_at, mdblist_synced_at)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(show_tmdb_id, season, episode) DO UPDATE SET
                    is_watched = 1,
                    last_watched_at = CASE WHEN excluded.last_watched_at > watched_history.last_watched_at THEN excluded.last_watched_at ELSE watched_history.last_watched_at END,
                    trakt_synced_at = COALESCE(excluded.trakt_synced_at, watched_history.trakt_synced_at),
                    simkl_synced_at = COALESCE(excluded.simkl_synced_at, watched_history.simkl_synced_at),
                    mdblist_synced_at = COALESCE(excluded.mdblist_synced_at, watched_history.mdblist_synced_at)
                """
                conn.executemany(query, to_update)
                
                # Update watched_episodes in tvshows_dynamic_cache.db for local user
                username = (get_trakt_user(config_db_path) or get_config_value('username', config_db_path) or 'default').lower()
                
                # Look up static episode IDs
                ep_id_lookup = {}
                if tvshows_static_db:
                    try:
                        with db_connect(tvshows_static_db) as sconn:
                            scursor = sconn.cursor()
                            distinct_shows = list(set([u[0] for u in to_update]))
                            placeholders = ','.join(['?'] * len(distinct_shows))
                            scursor.execute(
                                f"SELECT show_id, season, episode_number, episode_trakt_id, tmdb_id FROM episodes WHERE show_id IN ({placeholders})",
                                distinct_shows
                            )
                            for r_sid, r_sea, r_ep, r_trid, r_tmid in scursor.fetchall():
                                ep_id_lookup[(r_sid, r_sea, r_ep)] = (r_trid, r_tmid)
                    except Exception as e_lookup:
                        log(f"[Sync Engine] Warning: could not lookup static episode IDs: {e_lookup}", level=LOGWARNING)
                
                watched_episodes_data = []
                for u in to_update:
                    sid, sea, ep, wat, _, _, _ = u
                    tr_id, tm_id = ep_id_lookup.get((sid, sea, ep), (None, None))
                    watched_episodes_data.append((username, tr_id, tm_id, sea, ep, wat, 100, 2))
                    
                conn.executemany("""
                    INSERT OR REPLACE INTO watched_episodes (user, episode_trakt_id, tmdb_id, season, episode, watched_at, percent_watched, watched_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, watched_episodes_data)
                
                # Update parent show watched status in user_show_sync if static DB is available
                if tvshows_static_db:
                    try:
                        from resources.lib.watched import _update_show_watched_status
                        with db_connect(tvshows_static_db) as sconn:
                            scursor = sconn.cursor()
                            dcursor = conn.cursor()
                            for sid in set([u[0] for u in to_update]):
                                _update_show_watched_status(dcursor, scursor, username, sid)
                    except Exception as e_status:
                        log(f"[Sync Engine] Warning: could not update parent show statuses: {e_status}", level=LOGWARNING)
                
                conn.commit()
                log(f"[Sync Engine] Successfully reconciled {len(to_update)} tv show episodes.", level=LOGINFO)
        except Exception as e:
            log(f"[Sync Engine] Error reconciling shows: {e}", level=LOGERROR)


def sync_providers_sync(movies_dynamic_db, tvshows_dynamic_db, trakt_handler, config_db_path, tvshows_static_db=None, force=False):
    """
    Synchronous implementation of multi-provider watch history ingestion.
    Safe to call from background threads like UpdateQueueWorker.
    """
    if not _sync_engine_lock.acquire(blocking=False):
        log("[Sync Engine] Provider sync already in progress — skipping duplicate trigger.", level=LOGDEBUG)
        return
        
    try:
        log("[Sync Engine] Starting multi-provider watch history ingestion...", level=LOGINFO)
        
        # 1. Fetch Trakt
        PAGE_SIZE = 250
        trakt_movies = {}
        trakt_shows = {}
        _movies_watched_at = None
        _episodes_watched_at = None

        from resources.lib.config_handler import get_trakt_access_token, get_trakt_client_id, get_config_value, update_config_values
        trakt_token = get_trakt_access_token(config_db_path) if config_db_path else None
        trakt_client = get_trakt_client_id(config_db_path) if config_db_path else None
        has_trakt = bool(trakt_handler and trakt_token and trakt_client and trakt_token != "empty_setting")

        if has_trakt:
            try:
                fetch_movies = True
                fetch_shows = True

                if not force and config_db_path:
                    # Synchronous Trakt get using _get
                    la_resp = trakt_handler._get("/sync/last_activities", authenticated=True)
                    if la_resp and la_resp.status_code == 200:
                        la_data = la_resp.json()
                        _movies_watched_at   = la_data.get("movies",   {}).get("watched_at", "")
                        _episodes_watched_at = la_data.get("episodes", {}).get("watched_at", "")

                        local_movies_at   = get_config_value("trakt_movies_watched_synced_at",   config_db_path, "")
                        local_episodes_at = get_config_value("trakt_episodes_watched_synced_at", config_db_path, "")

                        if _movies_watched_at and local_movies_at and _movies_watched_at <= local_movies_at:
                            log("[Sync Engine] Trakt movie watched history unchanged — skipping fetch.", level=LOGINFO)
                            fetch_movies = False

                        if _episodes_watched_at and local_episodes_at and _episodes_watched_at <= local_episodes_at:
                            log("[Sync Engine] Trakt episode watched history unchanged — skipping fetch.", level=LOGINFO)
                            fetch_shows = False
                    else:
                        log("[Sync Engine] Could not fetch Trakt last_activities; proceeding with full sync.", level=LOGWARNING)

                # Fetch watched movies
                if fetch_movies:
                    page = 1
                    while True:
                        t_movies_resp = trakt_handler._get(
                            f"/sync/watched/movies?limit={PAGE_SIZE}&page={page}", authenticated=True
                        )
                        if not t_movies_resp or t_movies_resp.status_code != 200:
                            log(f"[Sync Engine] Trakt watched/movies page {page} failed: "
                                f"{t_movies_resp.status_code if t_movies_resp else 'no response'}",
                                level=LOGWARNING)
                            break
                        t_movies = t_movies_resp.json()
                        for m in t_movies:
                            tmdb_id = str(m.get('movie', {}).get('ids', {}).get('tmdb'))
                            if tmdb_id != "None":
                                trakt_movies[tmdb_id] = m.get('last_watched_at')
                        total_pages = int(t_movies_resp.headers.get("X-Pagination-Page-Count", 1))
                        if page >= total_pages:
                            break
                        page += 1

                # Fetch watched shows
                if fetch_shows:
                    page = 1
                    while True:
                        t_shows_resp = trakt_handler._get(
                            f"/sync/watched/shows?extended=full,progress&limit={PAGE_SIZE}&page={page}", authenticated=True
                        )
                        if not t_shows_resp or t_shows_resp.status_code != 200:
                            log(f"[Sync Engine] Trakt watched/shows page {page} failed: "
                                f"{t_shows_resp.status_code if t_shows_resp else 'no response'}",
                                level=LOGWARNING)
                            break
                        t_shows = t_shows_resp.json()
                        for s in t_shows:
                            show_tmdb_id = str(s.get('show', {}).get('ids', {}).get('tmdb'))
                            if show_tmdb_id != "None":
                                if show_tmdb_id not in trakt_shows:
                                    trakt_shows[show_tmdb_id] = {}
                                for season in s.get('seasons', []):
                                    season_num = season.get('number')
                                    for ep in season.get('episodes', []):
                                        ep_num = ep.get('number')
                                        key = f"{season_num}_{ep_num}"
                                        trakt_shows[show_tmdb_id][key] = ep.get('last_watched_at')
                        total_pages = int(t_shows_resp.headers.get("X-Pagination-Page-Count", 1))
                        if page >= total_pages:
                            break
                        page += 1

                log(f"[Sync Engine] Trakt fetched: {len(trakt_movies)} movies, {len(trakt_shows)} shows with episodes.", level=LOGINFO)

            except Exception as e:
                log(f"[Sync Engine] Trakt fetch error: {e}", level=LOGERROR)
        else:
            log("[Sync Engine] Missing Trakt credentials — skipping Trakt watched history fetch.", level=LOGDEBUG)

        # 2. Fetch Simkl
        simkl_history = fetch_simkl_history(config_db_path, tvshows_static_db=tvshows_static_db, force=force)
        simkl_movies = simkl_history.get("movies", {})
        simkl_shows = simkl_history.get("shows", {})
        simkl_act = simkl_history.get("activities", {})
        
        # 3. Fetch MDBList
        mdblist_history = fetch_mdblist_history(config_db_path)
        mdblist_movies = mdblist_history.get("movies", {})
        mdblist_shows = mdblist_history.get("shows", {})
        
        # 4. Reconcile movies
        if movies_dynamic_db:
            reconcile_movies(movies_dynamic_db, trakt_movies, simkl_movies, mdblist_movies, config_db_path)
        
        # 5. Reconcile shows
        if tvshows_dynamic_db:
            reconcile_shows(tvshows_dynamic_db, trakt_shows, simkl_shows, mdblist_shows, config_db_path, tvshows_static_db)

        # 6. Persist activities timestamps
        if config_db_path:
            try:
                updates = {}
                if _movies_watched_at:
                    updates["trakt_movies_watched_synced_at"] = _movies_watched_at
                if _episodes_watched_at:
                    updates["trakt_episodes_watched_synced_at"] = _episodes_watched_at
                    
                if simkl_act:
                    s_all = simkl_act.get("all")
                    if s_all:
                        updates["simkl_last_sync_at"] = s_all
                    s_tv_watch = simkl_act.get("tv_shows", {}).get("watching")
                    s_tv_comp = simkl_act.get("tv_shows", {}).get("completed")
                    s_mov_comp = simkl_act.get("movies", {}).get("completed")
                    if s_tv_watch:
                        updates["simkl_tv_watching_synced_at"] = s_tv_watch
                    if s_tv_comp:
                        updates["simkl_tv_completed_synced_at"] = s_tv_comp
                    if s_mov_comp:
                        updates["simkl_movies_completed_synced_at"] = s_mov_comp
                        
                if updates:
                    update_config_values(updates, config_db_path)
                    log(f"[Sync Engine] Persisted provider activities sync timestamps: {list(updates.keys())}", level=LOGDEBUG)
            except Exception as e:
                log(f"[Sync Engine] Failed to persist activities timestamps: {e}", level=LOGWARNING)

        # 7. Fetch dropped shows from each authorized provider
        if tvshows_static_db:
            try:
                from resources.lib.config_handler import get_authorized_watched_providers
                authed_providers = get_authorized_watched_providers(config_db_path) if config_db_path else []
                log("[Sync Engine] Checking for dropped show changes across providers...", level=LOGINFO)
                trakt_dropped = fetch_trakt_dropped(trakt_handler, config_db_path, force=force) if has_trakt else set()
                simkl_dropped = fetch_simkl_dropped(config_db_path, force=force) if 'simkl' in authed_providers else set()
                mdblist_dropped = fetch_mdblist_dropped(config_db_path) if 'mdblist' in authed_providers else set()

                reconcile_dropped_shows(tvshows_static_db, trakt_dropped, simkl_dropped, mdblist_dropped, config_db_path)
                bulk_sync_dropped(tvshows_static_db, trakt_handler, config_db_path)
            except Exception as e:
                log(f"[Sync Engine] Error in dropped show sync cycle: {e}", level=LOGERROR)

    finally:
        _sync_engine_lock.release()


async def sync_providers(movies_dynamic_db, tvshows_dynamic_db, trakt_handler, config_db_path, tvshows_static_db=None, force=False):
    """
    Async wrapper for sync_providers_sync, offloading to worker thread.
    """
    await asyncio.to_thread(
        sync_providers_sync,
        movies_dynamic_db,
        tvshows_dynamic_db,
        trakt_handler,
        config_db_path,
        tvshows_static_db,
        force
    )

def _chunk_list(lst, chunk_size):
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i + chunk_size]


def bulk_sync_history(movies_dynamic_db, tvshows_dynamic_db, trakt_handler, config_db_path, tvshows_static_db=None):
    """
    Pushes watched movies and TV episodes pending sync to Trakt, Simkl, and MDBList.
    Uses chunked batches (up to 100 movies / 20 shows per payload).
    """
    log("[Sync Engine] Checking for pending bulk sync history...", level=LOGINFO)
    
    from resources.lib.config_handler import get_authorized_watched_providers
    authed_providers = get_authorized_watched_providers(config_db_path) if config_db_path else []
    has_trakt = 'trakt' in authed_providers and bool(trakt_handler)
    has_simkl = 'simkl' in authed_providers
    has_mdblist = 'mdblist' in authed_providers

    if not (has_trakt or has_simkl or has_mdblist):
        log("[Sync Engine] No authorized watched providers for bulk sync.", level=LOGDEBUG)
        return

    # 1. Collect pending movies
    m_trakt_rows = []
    m_simkl_rows = []
    m_mdblist_rows = []
    
    try:
        with db_connect(movies_dynamic_db) as conn:
            cursor = conn.cursor()
            if has_trakt:
                cursor.execute("SELECT tmdb_id, last_watched_at FROM watched_history WHERE is_watched = 1 AND (trakt_synced_at IS NULL OR trakt_synced_at = '')")
                m_trakt_rows = cursor.fetchall()
            if has_simkl:
                cursor.execute("SELECT tmdb_id, last_watched_at FROM watched_history WHERE is_watched = 1 AND (simkl_synced_at IS NULL OR simkl_synced_at = '')")
                m_simkl_rows = cursor.fetchall()
            if has_mdblist:
                cursor.execute("SELECT tmdb_id, last_watched_at FROM watched_history WHERE is_watched = 1 AND (mdblist_synced_at IS NULL OR mdblist_synced_at = '')")
                m_mdblist_rows = cursor.fetchall()
    except Exception as e:
        log(f"[Sync Engine] Error collecting movies for bulk sync: {e}", level=LOGERROR)
         
    # 2. Collect pending TV shows
    t_trakt_rows = []
    t_simkl_rows = []
    t_mdblist_rows = []
    
    try:
        with db_connect(tvshows_dynamic_db) as conn:
            cursor = conn.cursor()
            if has_trakt:
                cursor.execute("SELECT show_tmdb_id, season, episode, last_watched_at FROM watched_history WHERE is_watched = 1 AND (trakt_synced_at IS NULL OR trakt_synced_at = '')")
                t_trakt_rows = cursor.fetchall()
            if has_simkl:
                cursor.execute("SELECT show_tmdb_id, season, episode, last_watched_at FROM watched_history WHERE is_watched = 1 AND (simkl_synced_at IS NULL OR simkl_synced_at = '')")
                t_simkl_rows = cursor.fetchall()
            if has_mdblist:
                cursor.execute("SELECT show_tmdb_id, season, episode, last_watched_at FROM watched_history WHERE is_watched = 1 AND (mdblist_synced_at IS NULL OR mdblist_synced_at = '')")
                t_mdblist_rows = cursor.fetchall()
    except Exception as e:
        log(f"[Sync Engine] Error collecting shows for bulk sync: {e}", level=LOGERROR)

    total_pending = len(m_trakt_rows) + len(m_simkl_rows) + len(m_mdblist_rows) + len(t_trakt_rows) + len(t_simkl_rows) + len(t_mdblist_rows)
    if total_pending == 0:
        log("[Sync Engine] No pending watched history items to sync.", level=LOGINFO)
        return

    log(f"[Sync Engine] Pending sync items — Trakt: M:{len(m_trakt_rows)} E:{len(t_trakt_rows)} | Simkl: M:{len(m_simkl_rows)} E:{len(t_simkl_rows)} | MDBList: M:{len(m_mdblist_rows)} E:{len(t_mdblist_rows)}", level=LOGINFO)

    # Fetch IMDB mappings from static db for collected shows
    all_sids = list(set([r[0] for r in (t_trakt_rows + t_simkl_rows + t_mdblist_rows)]))
    imdb_map = {}
    if tvshows_static_db and all_sids:
        try:
            with db_connect(tvshows_static_db) as conn:
                cursor = conn.cursor()
                for sid_chunk in _chunk_list(all_sids, 500):
                    placeholders = ','.join(['?'] * len(sid_chunk))
                    cursor.execute(f"SELECT show_tmdb_id, imdb_id FROM shows WHERE show_tmdb_id IN ({placeholders})", sid_chunk)
                    for rsid, rimdb in cursor.fetchall():
                        if rimdb:
                            imdb_map[rsid] = rimdb
        except Exception as e:
            log(f"[Sync Engine] Error fetching IMDB mappings for bulk sync: {e}", level=LOGERROR)

    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Helper to group show rows by show and season
    def _group_show_rows(rows, use_imdb=False):
        grouped = {}
        for sid, sea, ep, wat in rows:
            if sid not in grouped:
                grouped[sid] = {}
            if sea not in grouped[sid]:
                grouped[sid][sea] = []
            grouped[sid][sea].append({"number": ep, "watched_at": wat})
            
        payload_shows = []
        for sid, seasons in grouped.items():
            ids_block = {"tmdb": sid}
            if use_imdb and sid in imdb_map:
                ids_block["imdb"] = imdb_map[sid]
            show_obj = {"ids": ids_block, "seasons": []}
            for sea, eps in seasons.items():
                show_obj["seasons"].append({"number": sea, "episodes": eps})
            payload_shows.append(show_obj)
        return payload_shows

    # --- Push to Trakt ---
    if has_trakt and (m_trakt_rows or t_trakt_rows):
        t_shows_payload = _group_show_rows(t_trakt_rows)
        # Chunk movies
        for m_chunk in _chunk_list(m_trakt_rows, 100):
            p = {"movies": [{"watched_at": wat, "ids": {"tmdb": mid}} for mid, wat in m_chunk]}
            if send_batch_to_trakt(trakt_handler, p):
                chunk_mids = [mid for mid, _ in m_chunk]
                with db_connect(movies_dynamic_db) as conn:
                    conn.executemany("UPDATE watched_history SET trakt_synced_at = ? WHERE tmdb_id = ?", [(now_str, mid) for mid in chunk_mids])
                    conn.commit()
                    
        # Chunk shows
        for s_chunk in _chunk_list(t_shows_payload, 25):
            p = {"shows": s_chunk}
            if send_batch_to_trakt(trakt_handler, p):
                chunk_tuples = []
                for s_obj in s_chunk:
                    sid = s_obj["ids"]["tmdb"]
                    for sea_obj in s_obj["seasons"]:
                        sea = sea_obj["number"]
                        for ep_obj in sea_obj["episodes"]:
                            chunk_tuples.append((sid, sea, ep_obj["number"]))
                with db_connect(tvshows_dynamic_db) as conn:
                    conn.executemany(
                        "UPDATE watched_history SET trakt_synced_at = ? WHERE show_tmdb_id = ? AND season = ? AND episode = ?",
                        [(now_str, sid, sea, ep) for sid, sea, ep in chunk_tuples]
                    )
                    conn.commit()

    # --- Push to Simkl ---
    if has_simkl and (m_simkl_rows or t_simkl_rows):
        s_shows_payload = _group_show_rows(t_simkl_rows, use_imdb=True)
        # Chunk movies
        for m_chunk in _chunk_list(m_simkl_rows, 100):
            p = {"movies": [{"watched_at": wat, "ids": {"tmdb": mid}} for mid, wat in m_chunk]}
            if send_batch_to_simkl(config_db_path, p):
                chunk_mids = [mid for mid, _ in m_chunk]
                with db_connect(movies_dynamic_db) as conn:
                    conn.executemany("UPDATE watched_history SET simkl_synced_at = ? WHERE tmdb_id = ?", [(now_str, mid) for mid in chunk_mids])
                    conn.commit()
                    
        # Chunk shows
        for s_chunk in _chunk_list(s_shows_payload, 25):
            p = {"shows": s_chunk}
            if send_batch_to_simkl(config_db_path, p):
                chunk_tuples = []
                for s_obj in s_chunk:
                    sid = s_obj["ids"]["tmdb"]
                    for sea_obj in s_obj["seasons"]:
                        sea = sea_obj["number"]
                        for ep_obj in sea_obj["episodes"]:
                            chunk_tuples.append((sid, sea, ep_obj["number"]))
                with db_connect(tvshows_dynamic_db) as conn:
                    conn.executemany(
                        "UPDATE watched_history SET simkl_synced_at = ? WHERE show_tmdb_id = ? AND season = ? AND episode = ?",
                        [(now_str, sid, sea, ep) for sid, sea, ep in chunk_tuples]
                    )
                    conn.commit()

    # --- Push to MDBList ---
    if has_mdblist and (m_mdblist_rows or t_mdblist_rows):
        mdb_shows_payload = _group_show_rows(t_mdblist_rows, use_imdb=True)
        # Chunk movies
        for m_chunk in _chunk_list(m_mdblist_rows, 100):
            p = {"movies": [{"watched_at": wat, "ids": {"tmdb": mid}} for mid, wat in m_chunk]}
            if send_batch_to_mdblist(config_db_path, p):
                chunk_mids = [mid for mid, _ in m_chunk]
                with db_connect(movies_dynamic_db) as conn:
                    conn.executemany("UPDATE watched_history SET mdblist_synced_at = ? WHERE tmdb_id = ?", [(now_str, mid) for mid in chunk_mids])
                    conn.commit()
                    
        # Chunk shows
        for s_chunk in _chunk_list(mdb_shows_payload, 25):
            p = {"shows": s_chunk}
            if send_batch_to_mdblist(config_db_path, p):
                chunk_tuples = []
                for s_obj in s_chunk:
                    sid = s_obj["ids"]["tmdb"]
                    for sea_obj in s_obj["seasons"]:
                        sea = sea_obj["number"]
                        for ep_obj in sea_obj["episodes"]:
                            chunk_tuples.append((sid, sea, ep_obj["number"]))
                with db_connect(tvshows_dynamic_db) as conn:
                    conn.executemany(
                        "UPDATE watched_history SET mdblist_synced_at = ? WHERE show_tmdb_id = ? AND season = ? AND episode = ?",
                        [(now_str, sid, sea, ep) for sid, sea, ep in chunk_tuples]
                    )
                    conn.commit()

    log("[Sync Engine] Bulk sync history cycle completed.", level=LOGINFO)


def send_batch_to_trakt(trakt_handler, payload):
    if not trakt_handler:
        return False
    m_count = len(payload.get('movies', []))
    s_count = len(payload.get('shows', []))
    log(f"[Sync Engine] Sending Trakt batch... Movies:{m_count} Shows:{s_count}", level=LOGINFO)
    try:
        resp = trakt_handler.post("/sync/history", json=payload)
        if resp.status_code in [200, 201]:
            return True
        log(f"[Sync Engine] Trakt batch failed: {resp.status_code} - {resp.text}", level=LOGERROR)
    except Exception as e:
        log(f"[Sync Engine] Trakt batch exception: {e}", level=LOGERROR)
    return False


def send_batch_to_simkl(config_db_path, payload):
    from resources.lib.config_handler import get_config_value
    token = get_config_value("simkl.token", config_db_path)
    client_id = get_config_value("simkl.client", config_db_path)
    if not token or not client_id:
        return False
    
    m_count = len(payload.get('movies', []))
    s_count = len(payload.get('shows', []))
    log(f"[Sync Engine] Sending Simkl batch... Movies:{m_count} Shows:{s_count}", level=LOGINFO)
    
    headers = get_simkl_headers(token=token, client_id=client_id)
    params = get_simkl_params(client_id)
    try:
        resp = simkl_post('https://api.simkl.com/sync/history', params=params, headers=headers, json=payload, timeout=30)
        if resp.status_code in [200, 201]:
            resp_data = resp.json() if resp.text else {}
            not_found = resp_data.get("not_found", {})
            nf_shows = not_found.get("shows", [])
            nf_movies = not_found.get("movies", [])
            if nf_shows or nf_movies:
                log(f"[Sync Engine] WARNING: Simkl did not find {len(nf_movies)} movies and {len(nf_shows)} shows from batch.", level=LOGWARNING)
            return True
        log(f"[Sync Engine] Simkl batch failed: {resp.status_code} - {resp.text}", level=LOGERROR)
    except Exception as e:
        log(f"[Sync Engine] Simkl batch exception: {e}", level=LOGERROR)
    return False


def send_batch_to_mdblist(config_db_path, payload):
    from resources.lib.config_handler import get_config_value
    api_key = get_config_value("mdblist_api", config_db_path)
    if not api_key or api_key == "empty_setting":
        return False
    
    m_count = len(payload.get('movies', []))
    s_count = len(payload.get('shows', []))
    log(f"[Sync Engine] Sending MDBList batch... Movies:{m_count} Shows:{s_count}", level=LOGINFO)
    
    try:
        url = f"https://api.mdblist.com/sync/watched?apikey={api_key}"
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code in [200, 201]:
            return True
        log(f"[Sync Engine] MDBList batch error: {resp.status_code} - {resp.text}", level=LOGERROR)
    except Exception as e:
        log(f"[Sync Engine] MDBList batch exception: {e}", level=LOGERROR)
    return False


# ---------------------------------------------------------------------------
# Dropped-show sync helpers
# ---------------------------------------------------------------------------

def _ensure_dropped_sync_columns(tvshows_static_db):
    """Add per-provider dropped-sync timestamp columns if they don't exist."""
    cols = [
        "trakt_dropped_synced_at",
        "simkl_dropped_synced_at",
        "mdblist_dropped_synced_at",
    ]
    try:
        with db_connect(tvshows_static_db) as conn:
            for col in cols:
                try:
                    conn.execute(f"ALTER TABLE shows ADD COLUMN {col} TEXT")
                except Exception:
                    pass  # Column already exists
            conn.commit()
    except Exception as e:
        log(f"[Sync Engine] Could not ensure dropped sync columns: {e}", level=LOGWARNING)


def fetch_trakt_dropped(trakt_handler, config_db_path, force=False):
    """
    Fetch shows the user has marked as dropped on Trakt.
    Returns a set of TMDB IDs (integers).
    """
    from resources.lib.config_handler import get_trakt_access_token, get_trakt_client_id, get_config_value, update_config_values
    if not trakt_handler:
        return set()
    token = get_trakt_access_token(config_db_path) if config_db_path else None
    client = get_trakt_client_id(config_db_path) if config_db_path else None
    if not token or not client or token == "empty_setting":
        return set()

    dropped_tmdb_ids = set()
    PAGE_SIZE = 250
    try:
        # Activity-based change detection
        if not force and config_db_path:
            la_resp = trakt_handler._get("/sync/last_activities", authenticated=True)
            if la_resp and la_resp.status_code == 200:
                la_data = la_resp.json()
                remote_dropped_at = la_data.get("shows", {}).get("hidden_at", "")
                local_dropped_at = get_config_value("trakt_shows_dropped_synced_at", config_db_path, "")
                if remote_dropped_at and local_dropped_at and remote_dropped_at <= local_dropped_at:
                    log("[Sync Engine] Trakt dropped shows unchanged — skipping fetch.", level=LOGINFO)
                    return set()

        page = 1
        while True:
            resp = trakt_handler._get(
                f"/users/hidden/dropped?type=shows&limit={PAGE_SIZE}&page={page}",
                authenticated=True
            )
            if not resp or resp.status_code != 200:
                log(f"[Sync Engine] Trakt dropped shows page {page} failed: "
                    f"{resp.status_code if resp else 'no response'}", level=LOGWARNING)
                break
            items = resp.json()
            for item in items:
                tmdb_id = item.get("show", {}).get("ids", {}).get("tmdb")
                if tmdb_id:
                    dropped_tmdb_ids.add(int(tmdb_id))
            total_pages = int(resp.headers.get("X-Pagination-Page-Count", 1))
            if page >= total_pages:
                break
            page += 1

        log(f"[Sync Engine] Trakt dropped shows fetched: {len(dropped_tmdb_ids)}", level=LOGINFO)
    except Exception as e:
        log(f"[Sync Engine] Error fetching Trakt dropped shows: {e}", level=LOGERROR)
    return dropped_tmdb_ids


def fetch_simkl_dropped(config_db_path, force=False):
    """
    Fetch shows the user has marked as dropped on Simkl.
    Returns a set of TMDB IDs (integers).
    """
    from resources.lib.config_handler import get_config_value
    from resources.lib.simkl_api import ensure_valid_simkl_token, get_effective_simkl_client_id
    token = ensure_valid_simkl_token(config_db_path) or get_config_value("simkl.token", config_db_path)
    client_id = get_effective_simkl_client_id(config_db_path)
    if not token or not client_id or token == "empty_setting" or client_id == "empty_setting":
        return set()

    headers = get_simkl_headers(token=token, client_id=client_id)
    params = get_simkl_params(client_id)

    # Activity-based change detection
    if not force and config_db_path:
        try:
            act_resp = simkl_get('https://api.simkl.com/sync/activities', params=params, headers=headers, timeout=15)
            if act_resp.status_code == 200:
                act_data = act_resp.json()
                remote_dropped_at = act_data.get("tv_shows", {}).get("dropped", "") or ""
                local_dropped_at = get_config_value("simkl_tv_dropped_synced_at", config_db_path, "")
                if not remote_dropped_at or (local_dropped_at and remote_dropped_at <= local_dropped_at):
                    log("[Sync Engine] Simkl dropped shows unchanged — skipping fetch.", level=LOGINFO)
                    return set()
        except Exception as e:
            log(f"[Sync Engine] Simkl activities check for dropped failed: {e}", level=LOGWARNING)

    dropped_tmdb_ids = set()
    try:
        dropped_params = get_simkl_params(client_id, {"extended": "ids_only"})
        resp = simkl_get('https://api.simkl.com/sync/all-items/shows/dropped', params=dropped_params, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json() if resp.text else {}
        for show in data.get('shows', []):
            tmdb_id = show.get('show', {}).get('ids', {}).get('tmdb')
            if tmdb_id:
                dropped_tmdb_ids.add(int(tmdb_id))
        log(f"[Sync Engine] Simkl dropped shows fetched: {len(dropped_tmdb_ids)}", level=LOGINFO)
        if config_db_path and remote_dropped_at:
            from resources.lib.config_handler import update_config_values
            update_config_values({"simkl_tv_dropped_synced_at": remote_dropped_at}, config_db_path)
    except Exception as e:
        log(f"[Sync Engine] Error fetching Simkl dropped shows: {e}", level=LOGERROR)
    return dropped_tmdb_ids


def fetch_mdblist_dropped(config_db_path):
    """
    Fetch shows the user has marked as dropped on MDBList.
    Returns a set of TMDB IDs (integers).
    """
    from resources.lib.config_handler import get_config_value
    api_key = get_config_value("mdblist_api", config_db_path)
    if not api_key or api_key == "empty_setting":
        return set()

    dropped_tmdb_ids = set()
    try:
        url = f"https://api.mdblist.com/sync/dropped?apikey={api_key}"
        resp = requests.get(url, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get('shows', []) if isinstance(data, dict) else (data if isinstance(data, list) else []):
                # MDBList may return list of items or dict with 'shows' key
                show_obj = item.get('show', item) if isinstance(item, dict) else {}
                tmdb_id = show_obj.get('ids', {}).get('tmdb') if show_obj else None
                if tmdb_id:
                    dropped_tmdb_ids.add(int(tmdb_id))
            log(f"[Sync Engine] MDBList dropped shows fetched: {len(dropped_tmdb_ids)}", level=LOGINFO)
        elif resp.status_code == 404:
            log("[Sync Engine] MDBList dropped endpoint not found (beta) — skipping.", level=LOGDEBUG)
        else:
            log(f"[Sync Engine] MDBList dropped fetch failed: {resp.status_code}", level=LOGWARNING)
    except Exception as e:
        log(f"[Sync Engine] Error fetching MDBList dropped shows: {e}", level=LOGERROR)
    return dropped_tmdb_ids


def reconcile_dropped_shows(tvshows_static_db, trakt_dropped, simkl_dropped, mdblist_dropped, config_db_path=None):
    """
    Reconciles dropped show status from all providers into the local static DB.
    - Sets dropped=1 for any show that is dropped on any authorized provider
    - Clears dropped sync timestamps so bulk_sync_dropped will push to remaining providers
    Returns the set of show_tmdb_ids newly marked as dropped (for immediate push).
    """
    from resources.lib.config_handler import get_authorized_watched_providers
    authed = get_authorized_watched_providers(config_db_path) if config_db_path else []

    all_provider_dropped = set()
    if 'trakt' in authed:
        all_provider_dropped |= trakt_dropped
    if 'simkl' in authed:
        all_provider_dropped |= simkl_dropped
    if 'mdblist' in authed:
        all_provider_dropped |= mdblist_dropped

    if not all_provider_dropped:
        log("[Sync Engine] No dropped shows from providers to reconcile.", level=LOGDEBUG)
        return set()

    newly_dropped = set()
    try:
        _ensure_dropped_sync_columns(tvshows_static_db)
        with db_connect(tvshows_static_db) as conn:
            cursor = conn.cursor()
            # Fetch current dropped state for shows that are provider-dropped
            placeholders = ','.join(['?'] * len(all_provider_dropped))
            cursor.execute(
                f"SELECT show_tmdb_id, dropped, trakt_dropped_synced_at, simkl_dropped_synced_at, mdblist_dropped_synced_at "
                f"FROM shows WHERE show_tmdb_id IN ({placeholders})",
                list(all_provider_dropped)
            )
            existing = {r[0]: r for r in cursor.fetchall()}

            now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

            for tmdb_id in all_provider_dropped:
                row = existing.get(tmdb_id)
                if row is None:
                    # Show not in local DB yet — skip (will be reconciled when it's added)
                    continue
                was_dropped = row[1]
                if not was_dropped:
                    # Newly dropped from a provider — mark locally
                    newly_dropped.add(tmdb_id)
                    # Set dropped=1 and clear sync timestamps for providers that didn't report the drop
                    # so bulk_sync_dropped will push to them
                    trakt_ts = now_str if tmdb_id in trakt_dropped else None
                    simkl_ts = now_str if tmdb_id in simkl_dropped else None
                    mdblist_ts = now_str if tmdb_id in mdblist_dropped else None
                    conn.execute(
                        "UPDATE shows SET dropped = 1, trakt_dropped_synced_at = ?, simkl_dropped_synced_at = ?, mdblist_dropped_synced_at = ? "
                        "WHERE show_tmdb_id = ?",
                        (trakt_ts, simkl_ts, mdblist_ts, tmdb_id)
                    )
                    log(f"[Sync Engine] Show {tmdb_id} marked as dropped from provider (trakt={tmdb_id in trakt_dropped}, simkl={tmdb_id in simkl_dropped}, mdblist={tmdb_id in mdblist_dropped})", level=LOGINFO)

            conn.commit()

        log(f"[Sync Engine] Dropped show reconciliation complete: {len(newly_dropped)} newly dropped.", level=LOGINFO)
    except Exception as e:
        log(f"[Sync Engine] Error reconciling dropped shows: {e}", level=LOGERROR)
    return newly_dropped


def bulk_sync_dropped(tvshows_static_db, trakt_handler, config_db_path):
    """
    Pushes any locally dropped shows to providers that haven't been synced yet.
    Reads shows where dropped=1 and *_dropped_synced_at IS NULL for each provider.
    """
    from resources.lib.config_handler import get_authorized_watched_providers, update_config_values
    authed = get_authorized_watched_providers(config_db_path) if config_db_path else []
    has_trakt = 'trakt' in authed and bool(trakt_handler)
    has_simkl = 'simkl' in authed
    has_mdblist = 'mdblist' in authed

    if not (has_trakt or has_simkl or has_mdblist):
        return

    _ensure_dropped_sync_columns(tvshows_static_db)

    try:
        with db_connect(tvshows_static_db) as conn:
            cursor = conn.cursor()
            now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

            # --- Trakt ---
            if has_trakt:
                cursor.execute(
                    "SELECT show_tmdb_id, show_trakt_id FROM shows WHERE dropped = 1 AND (trakt_dropped_synced_at IS NULL OR trakt_dropped_synced_at = '')"
                )
                trakt_rows = cursor.fetchall()
                if trakt_rows:
                    shows_payload = [{"ids": {"tmdb": r[0], "trakt": r[1]}} for r in trakt_rows if r[1]]
                    no_trakt_id = [r[0] for r in trakt_rows if not r[1]]
                    if no_trakt_id:
                        log(f"[Sync Engine] {len(no_trakt_id)} dropped shows have no Trakt ID, will try TMDB-only", level=LOGDEBUG)
                        shows_payload += [{"ids": {"tmdb": sid}} for sid in no_trakt_id]
                    if shows_payload and send_drop_to_trakt(trakt_handler, shows_payload):
                        synced_ids = [r[0] for r in trakt_rows]
                        conn.executemany(
                            "UPDATE shows SET trakt_dropped_synced_at = ? WHERE show_tmdb_id = ?",
                            [(now_str, sid) for sid in synced_ids]
                        )
                        log(f"[Sync Engine] Pushed {len(synced_ids)} dropped shows to Trakt.", level=LOGINFO)

            # --- Simkl ---
            if has_simkl:
                cursor.execute(
                    "SELECT show_tmdb_id FROM shows WHERE dropped = 1 AND (simkl_dropped_synced_at IS NULL OR simkl_dropped_synced_at = '')"
                )
                simkl_rows = cursor.fetchall()
                if simkl_rows:
                    shows_payload = [{"ids": {"tmdb": r[0]}, "status": "dropped"} for r in simkl_rows]
                    if send_drop_to_simkl(config_db_path, shows_payload):
                        conn.executemany(
                            "UPDATE shows SET simkl_dropped_synced_at = ? WHERE show_tmdb_id = ?",
                            [(now_str, r[0]) for r in simkl_rows]
                        )
                        log(f"[Sync Engine] Pushed {len(simkl_rows)} dropped shows to Simkl.", level=LOGINFO)

            # --- MDBList ---
            if has_mdblist:
                cursor.execute(
                    "SELECT show_tmdb_id FROM shows WHERE dropped = 1 AND (mdblist_dropped_synced_at IS NULL OR mdblist_dropped_synced_at = '')"
                )
                mdblist_rows = cursor.fetchall()
                if mdblist_rows:
                    shows_payload = [{"ids": {"tmdb": r[0]}} for r in mdblist_rows]
                    if send_drop_to_mdblist_dropped(config_db_path, shows_payload):
                        conn.executemany(
                            "UPDATE shows SET mdblist_dropped_synced_at = ? WHERE show_tmdb_id = ?",
                            [(now_str, r[0]) for r in mdblist_rows]
                        )
                        log(f"[Sync Engine] Pushed {len(mdblist_rows)} dropped shows to MDBList.", level=LOGINFO)

            conn.commit()

    except Exception as e:
        log(f"[Sync Engine] Error in bulk_sync_dropped: {e}", level=LOGERROR)


def send_drop_to_trakt(trakt_handler, show_items):
    """
    Push a list of shows as dropped to Trakt.
    show_items: list of {"ids": {"tmdb": ..., "trakt": ...}} dicts.
    Returns True on success.
    """
    if not trakt_handler or not show_items:
        return False
    payload = {"shows": show_items}
    log(f"[Sync Engine] Pushing {len(show_items)} dropped show(s) to Trakt.", level=LOGINFO)
    try:
        resp = trakt_handler.post("/users/hidden/dropped", json=payload)
        if resp and resp.status_code in (200, 201, 204):
            return True
        log(f"[Sync Engine] Trakt drop push failed: {resp.status_code if resp else 'no response'} - {resp.text if resp else ''}", level=LOGERROR)
    except Exception as e:
        log(f"[Sync Engine] Trakt drop push exception: {e}", level=LOGERROR)
    return False


def send_drop_to_simkl(config_db_path, show_items):
    """
    Push a list of shows as dropped to Simkl via POST /sync/add-items.
    show_items: list of {"ids": {"tmdb": ...}, "status": "dropped"} dicts.
    Returns True on success.
    """
    from resources.lib.config_handler import get_config_value
    token = get_config_value("simkl.token", config_db_path)
    client_id = get_config_value("simkl.client", config_db_path)
    if not token or not client_id or not show_items:
        return False
    headers = get_simkl_headers(token=token, client_id=client_id)
    params = get_simkl_params(client_id)
    payload = {"shows": show_items}
    log(f"[Sync Engine] Pushing {len(show_items)} dropped show(s) to Simkl.", level=LOGINFO)
    try:
        resp = simkl_post('https://api.simkl.com/sync/add-items', params=params, headers=headers, json=payload, timeout=20)
        if resp.status_code in (200, 201, 204):
            return True
        log(f"[Sync Engine] Simkl drop push failed: {resp.status_code} - {resp.text}", level=LOGERROR)
    except Exception as e:
        log(f"[Sync Engine] Simkl drop push exception: {e}", level=LOGERROR)
    return False


def send_drop_to_mdblist_dropped(config_db_path, show_items):
    """
    Push a list of shows as dropped to MDBList via POST /sync/dropped (beta).
    show_items: list of {"ids": {"tmdb": ...}} dicts.
    Returns True on success.
    """
    from resources.lib.config_handler import get_config_value
    api_key = get_config_value("mdblist_api", config_db_path)
    if not api_key or api_key == "empty_setting" or not show_items:
        return False
    payload = {"shows": show_items}
    log(f"[Sync Engine] Pushing {len(show_items)} dropped show(s) to MDBList.", level=LOGINFO)
    try:
        url = f"https://api.mdblist.com/sync/dropped?apikey={api_key}"
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code in (200, 201, 204):
            return True
        if resp.status_code == 404:
            log("[Sync Engine] MDBList dropped endpoint not found (beta feature) — skipping.", level=LOGDEBUG)
            return True  # Don't block sync if endpoint not available
        log(f"[Sync Engine] MDBList drop push failed: {resp.status_code} - {resp.text}", level=LOGERROR)
    except Exception as e:
        log(f"[Sync Engine] MDBList drop push exception: {e}", level=LOGERROR)
    return False
