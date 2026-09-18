import sqlite3
import threading
import requests
import asyncio
from datetime import datetime
from resources.lib.db_utils import db_connect
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGDEBUG, LOGWARNING

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
    Uses /sync/activities to skip unchanged data unless force=True.
    """
    from resources.lib.config_handler import get_config_value
    token = get_config_value("simkl.token", config_db_path)
    client_id = get_config_value("simkl.client", config_db_path)
    
    if not token or not client_id or token == "empty_setting" or client_id == "empty_setting":
        log("[Sync Engine] Missing Simkl credentials.", level=LOGINFO)
        return {"movies": {}, "shows": {}, "activities": {}}
        
    headers = {
        'Content-Type': 'application/json',
        'simkl-api-key': client_id,
        'Authorization': f'Bearer {token}'
    }
    
    simkl_data = {"movies": {}, "shows": {}, "activities": {}}
    fetch_movies = True
    fetch_shows = True
    
    # 1. Activity check
    try:
        act_resp = requests.get('https://api.simkl.com/sync/activities', headers=headers, timeout=15)
        if act_resp.status_code == 200:
            act_data = act_resp.json()
            simkl_data["activities"] = act_data
            
            if not force and config_db_path:
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
                    return simkl_data
    except Exception as e:
        log(f"[Sync Engine] Simkl activities check failed: {e}", level=LOGWARNING)
        
    # 2. Full items fetch
    try:
        resp = requests.get('https://api.simkl.com/sync/all-items?extended=full', headers=headers, timeout=30)
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
    
    headers = {'Content-Type': 'application/json', 'simkl-api-key': client_id, 'Authorization': f'Bearer {token}'}
    try:
        resp = requests.post('https://api.simkl.com/sync/history', headers=headers, json=payload, timeout=30)
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
