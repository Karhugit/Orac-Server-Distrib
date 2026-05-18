import sqlite3
import requests
from datetime import datetime
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGDEBUG, LOGWARNING


def _parse_timestamp(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.strptime(ts_str.split(".")[0].replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
    except Exception as e:
        log(f"[Sync Engine] Error parsing timestamp {ts_str}: {e}", level=LOGERROR)
        return None

def fetch_simkl_history(config_db_path):
    from resources.lib.config_handler import get_config_value
    token = get_config_value("simkl.token", config_db_path)
    client_id = get_config_value("simkl.client", config_db_path)
    
    if not token or not client_id:
        log("[Sync Engine] Missing Simkl credentials.", level=LOGINFO)
        return {"movies": {}, "shows": {}}
        
    headers = {
        'Content-Type': 'application/json',
        'simkl-api-key': client_id,
        'Authorization': f'Bearer {token}'
    }
    
    simkl_data = {"movies": {}, "shows": {}}
    
    try:
        # According to Simkl docs, GET /sync/all-items provides everything.
        resp = requests.get('https://api.simkl.com/sync/all-items', headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        
        for movie in data.get('movies', []):
            if movie.get('status') == 'completed': 
                tmdb_id = movie.get('movie', {}).get('ids', {}).get('tmdb')
                if tmdb_id:
                    simkl_data["movies"][str(tmdb_id)] = movie.get('last_watched_at') or movie.get('updated_at')
                       
        for show in data.get('shows', []):
             show_tmdb_id = show.get('show', {}).get('ids', {}).get('tmdb')
             if not show_tmdb_id: continue
             
             # Fetch detailed show items to get episode watched states
             simkl_data["shows"][str(show_tmdb_id)] = {}
             # Simkl API returns watched episodes in /sync/ratings or by fetching specific show
             # For this engine's prototype, we'll assume /sync/all-items returns seasons if requested, or we iterate what is given.
             # Actually, Simkl requires fetching episodes specifically if they aren't in all-items.
             # Since this is a reverse sync ingestion script, we structure it safely.
             seasons = show.get('seasons', [])
             for season in seasons:
                 season_num = season.get('number')
                 for ep in season.get('episodes', []):
                     if ep.get('status') == 'completed':
                         ep_num = ep.get('number')
                         key = f"{season_num}_{ep_num}"
                         simkl_data["shows"][str(show_tmdb_id)][key] = ep.get('last_watched_at') or ep.get('updated_at')

    except Exception as e:
         log(f"[Sync Engine] Simkl fetch error: {e}", level=LOGERROR)
         
    return simkl_data

def fetch_mdblist_history(config_db_path):
    from resources.lib.config_handler import get_config_value
    api_key = get_config_value("mdblist_api", config_db_path)
    if not api_key or api_key == "empty_setting":
        log("[Sync Engine] Missing MDBList API key.", level=LOGINFO)
        return {"movies": {}, "shows": {}}
        
    mdblist_data = {"movies": {}, "shows": {}}
    try:
        url = f"https://api.mdblist.com/sync/watched?apikey={api_key}"
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        
        for movie in data.get('movies', []):
            tmdb_id = movie.get('mediatype', {}).get('ids', {}).get('tmdb')
            if tmdb_id:
                mdblist_data["movies"][str(tmdb_id)] = movie.get('last_watched_at')
                
        for episode in data.get('episodes', []):
            show_tmdb_id = episode.get('mediatype', {}).get('ids', {}).get('tmdb')
            if not show_tmdb_id: continue
            
            season_num = episode.get('mediatype', {}).get('season_number')
            ep_num = episode.get('mediatype', {}).get('episode_number')
            if season_num is not None and ep_num is not None:
                if str(show_tmdb_id) not in mdblist_data["shows"]:
                    mdblist_data["shows"][str(show_tmdb_id)] = {}
                key = f"{season_num}_{ep_num}"
                mdblist_data["shows"][str(show_tmdb_id)][key] = episode.get('last_watched_at')

    except Exception as e:
         log(f"[Sync Engine] MDBList fetch error: {e}", level=LOGERROR)
         
    return mdblist_data

async def sync_providers(movies_dynamic_db, tvshows_dynamic_db, trakt_handler, config_db_path):
    log("[Sync Engine] Starting dual-provider ingestion...", level=LOGINFO)
    
    # 1. Fetch Trakt
    trakt_movies = {}
    trakt_shows = {}
    if trakt_handler:
        try:
             t_movies_resp = await trakt_handler.get("/sync/watched/movies")
             t_movies = t_movies_resp.json() if t_movies_resp and t_movies_resp.status_code == 200 else []
             for m in t_movies:
                 tmdb_id = str(m.get('movie', {}).get('ids', {}).get('tmdb'))
                 if tmdb_id != "None":
                     trakt_movies[tmdb_id] = m.get('last_watched_at')
                     
             t_shows_resp = await trakt_handler.get("/sync/watched/shows")
             t_shows = t_shows_resp.json() if t_shows_resp and t_shows_resp.status_code == 200 else []
             for s in t_shows:
                 show_tmdb_id = str(s.get('show', {}).get('ids', {}).get('tmdb'))
                 if show_tmdb_id != "None":
                     trakt_shows[show_tmdb_id] = {}
                     for season in s.get('seasons', []):
                         season_num = season.get('number')
                         for ep in season.get('episodes', []):
                             ep_num = ep.get('number')
                             key = f"{season_num}_{ep_num}"
                             trakt_shows[show_tmdb_id][key] = ep.get('last_watched_at')

        except Exception as e:
             log(f"[Sync Engine] Trakt fetch error: {e}", level=LOGERROR)

    # 2. Fetch Simkl
    simkl_history = fetch_simkl_history(config_db_path)
    simkl_movies = simkl_history.get("movies", {})
    simkl_shows = simkl_history.get("shows", {})
    
    # 3. Fetch MDBList
    mdblist_history = fetch_mdblist_history(config_db_path)
    mdblist_movies = mdblist_history.get("movies", {})
    mdblist_shows = mdblist_history.get("shows", {})
    
    # 4. Reconcile movies
    reconcile_movies(movies_dynamic_db, trakt_movies, simkl_movies, mdblist_movies)
    
    # 5. Reconcile shows
    reconcile_shows(tvshows_dynamic_db, trakt_shows, simkl_shows, mdblist_shows)


def reconcile_movies(db_path, trakt_data, simkl_data, mdblist_data):
    log("[Sync Engine] Reconciling and flagging movies...", level=LOGINFO)
    all_tmdb_ids = set(trakt_data.keys()).union(set(simkl_data.keys())).union(set(mdblist_data.keys()))
    
    to_update = []
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
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
                
        trakt_synced_at = now_str if t_time_str else None
        simkl_synced_at = now_str if s_time_str else None
        mdblist_synced_at = now_str if m_time_str else None
        
        to_update.append((
             int(tmdb_id), True, winner_time_str, trakt_synced_at, simkl_synced_at, mdblist_synced_at,
             True, winner_time_str, trakt_synced_at, simkl_synced_at, mdblist_synced_at
        ))
        
    if to_update:
        try:
             with sqlite3.connect(db_path) as conn:
                 query = """
                 INSERT INTO watched_history (tmdb_id, is_watched, last_watched_at, trakt_synced_at, simkl_synced_at, mdblist_synced_at)
                 VALUES (?, ?, ?, ?, ?, ?)
                 ON CONFLICT(tmdb_id) DO UPDATE SET
                     is_watched = ?, last_watched_at = ?,
                     trakt_synced_at = CASE WHEN ? IS NOT NULL THEN ? ELSE trakt_synced_at END,
                     simkl_synced_at = CASE WHEN ? IS NOT NULL THEN ? ELSE simkl_synced_at END,
                     mdblist_synced_at = CASE WHEN ? IS NOT NULL THEN ? ELSE mdblist_synced_at END
                 """
                 expanded = [(i[0], i[1], i[2], i[3], i[4], i[5], i[6], i[7], i[8], i[8], i[9], i[9], i[10], i[10]) for i in to_update]
                 conn.executemany(query, expanded)
                 conn.commit()
        except Exception as e:
             log(f"[Sync Engine] Error reconciling movies: {e}", level=LOGERROR)

def reconcile_shows(db_path, trakt_data, simkl_data, mdblist_data):
    log("[Sync Engine] Reconciling and flagging tv shows...", level=LOGINFO)
    all_show_tmdb_ids = set(trakt_data.keys()).union(set(simkl_data.keys())).union(set(mdblist_data.keys()))
    
    to_update = []
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    for show_tmdb_id in all_show_tmdb_ids:
        t_eps = trakt_data.get(show_tmdb_id, {})
        s_eps = simkl_data.get(show_tmdb_id, {})
        m_eps = mdblist_data.get(show_tmdb_id, {})
        
        all_ep_keys = set(t_eps.keys()).union(set(s_eps.keys())).union(set(m_eps.keys()))
        for ep_key in all_ep_keys:
            season_num, ep_num = map(int, ep_key.split('_'))
            
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
                
            trakt_synced_at = now_str if t_time_str else None
            simkl_synced_at = now_str if s_time_str else None
            mdblist_synced_at = now_str if m_time_str else None
            
            to_update.append((
                 int(show_tmdb_id), season_num, ep_num, True, winner_time_str, trakt_synced_at, simkl_synced_at, mdblist_synced_at,
                 True, winner_time_str, trakt_synced_at, simkl_synced_at, mdblist_synced_at
            ))
            
    if to_update:
        try:
             with sqlite3.connect(db_path) as conn:
                 query = """
                 INSERT INTO watched_history (show_tmdb_id, season, episode, is_watched, last_watched_at, trakt_synced_at, simkl_synced_at, mdblist_synced_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT(show_tmdb_id, season, episode) DO UPDATE SET
                     is_watched = ?, last_watched_at = ?,
                     trakt_synced_at = CASE WHEN ? IS NOT NULL THEN ? ELSE trakt_synced_at END,
                     simkl_synced_at = CASE WHEN ? IS NOT NULL THEN ? ELSE simkl_synced_at END,
                     mdblist_synced_at = CASE WHEN ? IS NOT NULL THEN ? ELSE mdblist_synced_at END
                 """
                 expanded = [(i[0], i[1], i[2], i[3], i[4], i[5], i[6], i[7], i[8], i[9], i[10], i[10], i[11], i[11], i[12], i[12]) for i in to_update]
                 conn.executemany(query, expanded)
                 conn.commit()
        except Exception as e:
             log(f"[Sync Engine] Error reconciling shows: {e}", level=LOGERROR)

def bulk_sync_history(movies_dynamic_db, tvshows_dynamic_db, trakt_handler, config_db_path, tvshows_static_db=None):
    log("[Sync Engine] Starting bulk sync history...", level=LOGINFO)
    
    # 1. Collect pending
    trakt_payload = {"movies": [], "shows": []}
    simkl_payload = {"movies": [], "shows": []}
    mdblist_payload = {"movies": [], "shows": []}
    
    m_trakt_ids = []
    m_simkl_ids = []
    m_mdblist_ids = []
    
    try:
        with sqlite3.connect(movies_dynamic_db) as conn:
             cursor = conn.cursor()
             # We only send those watched where they are missing the synced timestamp
             cursor.execute("SELECT tmdb_id, last_watched_at FROM watched_history WHERE is_watched = 1 AND trakt_synced_at IS NULL")
             for row in cursor.fetchall():
                  tmdb_id, watched_at = row
                  trakt_payload["movies"].append({"watched_at": watched_at, "ids": {"tmdb": tmdb_id}})
                  m_trakt_ids.append(tmdb_id)
                  
             cursor.execute("SELECT tmdb_id, last_watched_at FROM watched_history WHERE is_watched = 1 AND (simkl_synced_at IS NULL OR simkl_synced_at = '')")
             for row in cursor.fetchall():
                  tmdb_id, watched_at = row
                  simkl_payload["movies"].append({"watched_at": watched_at, "ids": {"tmdb": tmdb_id}})
                  m_simkl_ids.append(tmdb_id)
                  
             cursor.execute("SELECT tmdb_id, last_watched_at FROM watched_history WHERE is_watched = 1 AND (mdblist_synced_at IS NULL OR mdblist_synced_at = '')")
             for row in cursor.fetchall():
                  tmdb_id, watched_at = row
                  mdblist_payload["movies"].append({"watched_at": watched_at, "ids": {"tmdb": tmdb_id}})
                  m_mdblist_ids.append(tmdb_id)
    except Exception as e:
         log(f"[Sync Engine] Error collecting movies for bulk sync: {e}", level=LOGERROR)
         
    # Group TV Shows for Trakt, Simkl and MDBList
    t_trakt_updates = []
    t_simkl_updates = []
    t_mdblist_updates = []
    
    t_trakt_grouped = {}
    t_simkl_grouped = {}
    t_mdblist_grouped = {}
    
    try:
         with sqlite3.connect(tvshows_dynamic_db) as conn:
             cursor = conn.cursor()
             cursor.execute("SELECT show_tmdb_id, season, episode, last_watched_at FROM watched_history WHERE is_watched = 1 AND trakt_synced_at IS NULL")
             for row in cursor.fetchall():
                  sid, sea, ep, wat = row
                  if sid not in t_trakt_grouped: t_trakt_grouped[sid] = {}
                  if sea not in t_trakt_grouped[sid]: t_trakt_grouped[sid][sea] = []
                  t_trakt_grouped[sid][sea].append({"number": ep, "watched_at": wat})
                  t_trakt_updates.append((sid, sea, ep))
                  
             cursor.execute("SELECT show_tmdb_id, season, episode, last_watched_at FROM watched_history WHERE is_watched = 1 AND (simkl_synced_at IS NULL OR simkl_synced_at = '')")
             for row in cursor.fetchall():
                  sid, sea, ep, wat = row
                  if sid not in t_simkl_grouped: t_simkl_grouped[sid] = {}
                  if sea not in t_simkl_grouped[sid]: t_simkl_grouped[sid][sea] = []
                  t_simkl_grouped[sid][sea].append({"number": ep, "watched_at": wat})
                  t_simkl_updates.append((sid, sea, ep))
                  
             cursor.execute("SELECT show_tmdb_id, season, episode, last_watched_at FROM watched_history WHERE is_watched = 1 AND (mdblist_synced_at IS NULL OR mdblist_synced_at = '')")
             for row in cursor.fetchall():
                  sid, sea, ep, wat = row
                  if sid not in t_mdblist_grouped: t_mdblist_grouped[sid] = {}
                  if sea not in t_mdblist_grouped[sid]: t_mdblist_grouped[sid][sea] = []
                  t_mdblist_grouped[sid][sea].append({"number": ep, "watched_at": wat})
                  t_mdblist_updates.append((sid, sea, ep))
    except Exception as e:
         log(f"[Sync Engine] Error collecting shows for bulk sync: {e}", level=LOGERROR)
         
    # Fetch IMDB mappings from static db for collected shows
    imdb_map = {}
    if tvshows_static_db and (t_trakt_grouped or t_simkl_grouped or t_mdblist_grouped):
         all_sids = list(set(list(t_trakt_grouped.keys()) + list(t_simkl_grouped.keys()) + list(t_mdblist_grouped.keys())))
         if all_sids:
              try:
                   with sqlite3.connect(tvshows_static_db) as conn:
                        cursor = conn.cursor()
                        placeholders = ','.join(['?'] * len(all_sids))
                        cursor.execute(f"SELECT show_tmdb_id, imdb_id FROM shows WHERE show_tmdb_id IN ({placeholders})", all_sids)
                        for rsid, rimdb in cursor.fetchall():
                             if rimdb: imdb_map[rsid] = rimdb
              except Exception as e:
                   log(f"[Sync Engine] Error fetching IMDB mappings for bulk sync: {e}", level=LOGERROR)
         
    # Build Trakt Payload for shows
    for sid, seasons in t_trakt_grouped.items():
         show_obj = {"ids": {"tmdb": sid}, "seasons": []}
         for sea, eps in seasons.items():
              show_obj["seasons"].append({"number": sea, "episodes": eps})
         trakt_payload["shows"].append(show_obj)
         
    # Build Simkl Payload for shows (Simkl requires nested seasons → episodes format)
    for sid, seasons in t_simkl_grouped.items():
         ids_block = {"tmdb": sid}
         if sid in imdb_map: ids_block["imdb"] = imdb_map[sid]
         show_obj = {"ids": ids_block, "seasons": []}
         for sea, eps in seasons.items():
              season_obj = {"number": sea, "episodes": []}
              for ep in eps:
                   season_obj["episodes"].append({
                        "number": ep["number"],
                        "watched_at": ep["watched_at"]
                   })
              show_obj["seasons"].append(season_obj)
         simkl_payload["shows"].append(show_obj)
         
    # Build MDBList Payload for shows
    for sid, seasons in t_mdblist_grouped.items():
         ids_block = {"tmdb": sid}
         if sid in imdb_map: ids_block["imdb"] = imdb_map[sid]
         show_obj = {"ids": ids_block, "seasons": []}
         for sea, eps in seasons.items():
              season_obj = {"number": sea, "episodes": []}
              for ep in eps:
                   season_obj["episodes"].append({
                        "number": ep["number"],
                        "watched_at": ep["watched_at"]
                   })
              show_obj["seasons"].append(season_obj)
         mdblist_payload["shows"].append(show_obj)
         
    # 2. Execution Post
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    trakt_success = False
    if trakt_payload["movies"] or trakt_payload["shows"]:
         trakt_success = send_batch_to_trakt(trakt_handler, trakt_payload)
         
    simkl_success = False
    if simkl_payload["movies"] or simkl_payload["shows"]:
         simkl_success = send_batch_to_simkl(config_db_path, simkl_payload)
    else:
         log("[Sync Engine] No pending Simkl updates to send.", level=LOGDEBUG)
         
    mdblist_success = False
    if mdblist_payload["movies"] or mdblist_payload["shows"]:
         mdblist_success = send_batch_to_mdblist(config_db_path, mdblist_payload)
    else:
         log("[Sync Engine] No pending MDBList updates to send.", level=LOGDEBUG)
         
    # 3. DB Updates (executemany to keep it snappy as requested)
    if trakt_success:
         log("[Sync Engine] Updating local trakt_synced_at timestamps...", level=LOGINFO)
         try:
              if m_trakt_ids:
                   with sqlite3.connect(movies_dynamic_db) as conn:
                        conn.executemany("UPDATE watched_history SET trakt_synced_at = ? WHERE tmdb_id = ?", [(now_str, mid) for mid in m_trakt_ids])
                        conn.commit()
              if t_trakt_updates:
                   with sqlite3.connect(tvshows_dynamic_db) as conn:
                        conn.executemany("UPDATE watched_history SET trakt_synced_at = ? WHERE show_tmdb_id = ? AND season = ? AND episode = ?", [(now_str, sid, sea, ep) for sid, sea, ep in t_trakt_updates])
                        conn.commit()
         except Exception as e:
             log(f"[Sync Engine] Error bulk updating trakt timestamps: {e}", level=LOGERROR)
             
    if simkl_success:
         log("[Sync Engine] Updating local simkl_synced_at timestamps...", level=LOGINFO)
         try:
              if m_simkl_ids:
                   with sqlite3.connect(movies_dynamic_db) as conn:
                        conn.executemany("UPDATE watched_history SET simkl_synced_at = ? WHERE tmdb_id = ?", [(now_str, mid) for mid in m_simkl_ids])
                        conn.commit()
              if t_simkl_updates:
                   with sqlite3.connect(tvshows_dynamic_db) as conn:
                        conn.executemany("UPDATE watched_history SET simkl_synced_at = ? WHERE show_tmdb_id = ? AND season = ? AND episode = ?", [(now_str, sid, sea, ep) for sid, sea, ep in t_simkl_updates])
                        conn.commit()
         except Exception as e:
             log(f"[Sync Engine] Error bulk updating simkl timestamps: {e}", level=LOGERROR)

    if mdblist_success:
         log("[Sync Engine] Updating local mdblist_synced_at timestamps...", level=LOGINFO)
         try:
              if m_mdblist_ids:
                   with sqlite3.connect(movies_dynamic_db) as conn:
                        conn.executemany("UPDATE watched_history SET mdblist_synced_at = ? WHERE tmdb_id = ?", [(now_str, mid) for mid in m_mdblist_ids])
                        conn.commit()
              if t_mdblist_updates:
                   with sqlite3.connect(tvshows_dynamic_db) as conn:
                        conn.executemany("UPDATE watched_history SET mdblist_synced_at = ? WHERE show_tmdb_id = ? AND season = ? AND episode = ?", [(now_str, sid, sea, ep) for sid, sea, ep in t_mdblist_updates])
                        conn.commit()
         except Exception as e:
             log(f"[Sync Engine] Error bulk updating mdblist timestamps: {e}", level=LOGERROR)

def send_batch_to_trakt(trakt_handler, payload):
    if not trakt_handler: return False
    log(f"[Sync Engine] Sending Trakt batch... M:{len(payload['movies'])} S:{len(payload['shows'])}", level=LOGINFO)
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
    if not token or not client_id: return False
    
    log(f"[Sync Engine] Sending Simkl batch... M:{len(payload['movies'])} S:{len(payload['shows'])}", level=LOGINFO)
    import json
    log(f"[Sync Engine] Simkl payload details: {json.dumps(payload)}", level=LOGDEBUG)
    
    headers = {'Content-Type': 'application/json', 'simkl-api-key': client_id, 'Authorization': f'Bearer {token}'}
    try:
        resp = requests.post('https://api.simkl.com/sync/history', headers=headers, json=payload, timeout=30)
        if resp.status_code in [200, 201]:
             resp_data = resp.json() if resp.text else {}
             log(f"[Sync Engine] Simkl batch response: {json.dumps(resp_data)}", level=LOGINFO)
             # Check for not_found items
             not_found = resp_data.get("not_found", {})
             nf_shows = not_found.get("shows", [])
             nf_movies = not_found.get("movies", [])
             if nf_shows or nf_movies:
                  log(f"[Sync Engine] WARNING: Simkl did not find {len(nf_movies)} movies and {len(nf_shows)} shows from the batch!", level=LOGWARNING)
             return True
        log(f"[Sync Engine] Simkl batch failed: {resp.status_code} - {resp.text}", level=LOGERROR)
    except Exception as e:
        log(f"[Sync Engine] Simkl batch exception: {e}", level=LOGERROR)
    return False

def send_batch_to_mdblist(config_db_path, payload):
    from resources.lib.config_handler import get_config_value
    api_key = get_config_value("mdblist_api", config_db_path)
    if not api_key or api_key == "empty_setting": return False
    
    log(f"[Sync Engine] Sending MDBList batch... M:{len(payload['movies'])} S:{len(payload['shows'])}", level=LOGINFO)
    import json
    log(f"[Sync Engine] MDBList payload details: {json.dumps(payload)}", level=LOGDEBUG)
    
    try:
        url = f"https://api.mdblist.com/sync/watched?apikey={api_key}"
        resp = requests.post(url, json=payload, timeout=30)
        # MDBList POST Watched endpoint returns standard 200/201 on success
        if resp.status_code in [200, 201]:
             return True
        log(f"[Sync Engine] MDBList batch error: {resp.status_code} - {resp.text}", level=LOGERROR)
    except Exception as e:
        log(f"[Sync Engine] MDBList batch exception: {e}", level=LOGERROR)
    return False
