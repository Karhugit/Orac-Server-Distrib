import sqlite3
import json
import time
from resources.lib.log_utils import log, LOGERROR, LOGDEBUG, LOGINFO
from datetime import datetime
from threading import Thread, Event
from concurrent.futures import ThreadPoolExecutor, as_completed

from resources.lib.trakt_utils import unlike_trakt_list
from resources.lib.config_handler import get_config_value


class UpdateQueueWorker:
    def __init__(self, update_queue_path, tvshows_static_db_path, trakt_auth, tmdb_handler, db_manager, config_db_path, movies_dynamic_db_path=None, tvshows_dynamic_db_path=None, interval=300):
        self.db = update_queue_path
        self.tvshows_static_db = tvshows_static_db_path
        self.trakt_auth = trakt_auth
        self.tmdb_handler = tmdb_handler
        self.db_manager = db_manager
        self.config_db_path = config_db_path
        self.movies_dynamic_db_path = movies_dynamic_db_path
        self.tvshows_dynamic_db_path = tvshows_dynamic_db_path
        self.interval = interval  # in seconds

        self._stop_event = Event()
        self._pause_event = Event()
        self._pause_event.set()  # Start in "running" state
        self._thread = None

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._pause_event.set()
            self._thread = Thread(target=self._run_loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._pause_event.set()  # Unpause if paused, so it can exit
        if self._thread:
            self._thread.join()
            self._thread = None

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    def _run_loop(self):
        while not self._stop_event.is_set():
            self._pause_event.wait()  # Will block here if paused
            self.process_queue_once()
            time.sleep(self.interval)

    def process_queue_once(self):
        if self.movies_dynamic_db_path and self.tvshows_dynamic_db_path:
             from resources.lib.sync_engine import bulk_sync_history
             try:
                 bulk_sync_history(self.movies_dynamic_db_path, self.tvshows_dynamic_db_path, self.trakt_auth, self.config_db_path, self.tvshows_static_db)
             except Exception as e:
                 log(f"[Queue] Error running bulk status sync in processor: {e}", level=LOGERROR)
                 
        conn = self.db_manager.get_connection(self.db)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM update_queue WHERE status = 'pending' OR status = 'retry' ORDER BY priority, created_at ASC")
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            # log("[Queue] No pending updates.")
            return

        def handle_row(row_data):
            row = dict(row_data)
            row['payload'] = json.loads(row['payload']) if row['payload'] else {}

            if row['status'] == 'retry':
                number_of_attempts = row.get('attempts', 0) + 1
                if number_of_attempts > 3:
                    log(f"[Queue] Max retry attempts reached for row {row['id']}. Marking as failed.")
                    self._mark_status(row['id'], 'failed')
                    return
            self._mark_status(row['id'], 'processing')
            try:
                provider = row.get('provider', 'trakt')
                update_type = row['update_type']
                
                if provider == 'tmdb':
                    self.process_tmdb_update(row, update_type)
                else:
                    self.process_trakt_update(row, update_type)

                self._mark_status(row['id'], 'done')
            except Exception as e:
                log(f"[Queue] Error processing row {row['id']}: {e}", level=LOGERROR)
                self._mark_status(row['id'], 'failed')

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(handle_row, row) for row in rows]
            for future in as_completed(futures):
                try:
                    future.result()  # This will re-raise exceptions if any
                except Exception as e:
                    log(f"[Queue] Error in worker thread: {e}", level=LOGERROR)

    def process_trakt_update(self, row, update_type):
        if update_type == 'watched_episode':
            self.mark_watched_episode(row)
        elif update_type == 'watched_movie':
            self.mark_watched_movie(row)
        elif update_type == 'add_to_list':
            result = self.add_to_list_trakt(row)
            if not result:
                self._mark_status(row['id'], 'retry')
                raise Exception("Failed to add to Trakt list")
        elif update_type == 'remove_from_list':
            result = self.remove_from_list_trakt(row)
            if not result:
                self._mark_status(row['id'], 'retry')
                raise Exception("Failed to remove from Trakt list")
        elif update_type == 'unlike_trakt_list':
            result = unlike_trakt_list(
                self.trakt_auth,
                row['payload'].get('user'),
                row['payload'].get('list_name'),
                row['payload'].get('slug')
            )
            if not result:
                self._mark_status(row['id'], 'retry')
                raise Exception("Failed to unlike Trakt list")
        elif update_type == 'drop_show':
            self.drop_show(row)
        else:
            log(f"[Queue] Unknown Trakt update type: {update_type}", level=LOGERROR)
            raise Exception(f"Unknown update type {update_type}")

    def process_tmdb_update(self, row, update_type):
        # Fetch generic TMDB credentials
        # We need session_id and account_id
        session_id = get_config_value("tmdb_session_id", self.config_db_path)
        account_id = get_config_value("tmdb_account_id", self.config_db_path)
        
        if not session_id:
             log(f"[Queue] Missing TMDB session_id. Cannot process TMDB update.", level=LOGERROR)
             raise Exception("Missing TMDB session_id")

        tmdb_id = row['payload'].get('tmdb_id')
        item_type = row['payload'].get('item_type') # 'movie' or 'tvshow'
        slug = row['payload'].get('slug')
        
        # Map internal item_type to TMDB media_type
        media_type = 'movie' if item_type == 'movie' else 'tv'
        
        result = None
        
        if update_type == 'add_to_list':
            if slug == 'watchlist':
                 log(f"[Queue] Adding {media_type} {tmdb_id} to TMDB Watchlist")
                 result = self.tmdb_handler.add_to_watchlist(account_id, session_id, media_type, tmdb_id)
            else:
                 log(f"[Queue] Adding item {tmdb_id} to TMDB List {slug}")
                 # Slug for TMDB lists is expected to be the List ID
                 result = self.tmdb_handler.add_to_list(slug, session_id, tmdb_id)
                 
        elif update_type == 'remove_from_list':
             if slug == 'watchlist':
                 log(f"[Queue] Removing {media_type} {tmdb_id} from TMDB Watchlist")
                 result = self.tmdb_handler.remove_from_watchlist(account_id, session_id, media_type, tmdb_id)
             else:
                 log(f"[Queue] Removing item {tmdb_id} from TMDB List {slug}")
                 result = self.tmdb_handler.remove_from_list(slug, session_id, tmdb_id)
        
        else:
            log(f"[Queue] Unknown TMDB update type: {update_type}", level=LOGERROR)
            raise Exception(f"Unknown TMDB update type {update_type}")

        if not result or (result.get('success') is False and result.get('status_code') not in [1, 12, 13]):
            # 1: Success, 12: Item updated, 13: Item deleted
            log(f"[Queue] TMDB update failed: {result}", level=LOGERROR)
            self._mark_status(row['id'], 'retry')
            raise Exception(f"TMDB API failure: {result}")
        else:
            log(f"[Queue] TMDB update successful: {result}", level=LOGINFO)


    def _mark_status(self, row_id, status):
        with self.db_manager.connection(self.db) as conn:
            conn.execute("UPDATE update_queue SET status = ? WHERE id = ?", (status, row_id))


    def mark_watched_episode(self, row):
        episode_trakt_id = row['payload'].get('episode_trakt_id')
        episode_tmdb_id = row['payload'].get('tmdb_id')
        
        # Build IDs block - prefer Trakt ID if available, only use TMDB ID if it's positive (not a placeholder)
        ids_block = {}
        if episode_trakt_id and episode_trakt_id > 0:
            ids_block["trakt"] = episode_trakt_id
        if episode_tmdb_id and episode_tmdb_id > 0:
            ids_block["tmdb"] = episode_tmdb_id
        
        if not ids_block:
            log(f"[Queue] Cannot mark episode as watched - no valid IDs (trakt={episode_trakt_id}, tmdb={episode_tmdb_id})", level=LOGERROR)
            return
        
        payload = {
            "episodes": [
                {
                    "watched_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "ids": ids_block
                }
            ]
        }

        log(f"[Queue] Marking episode as watched: {payload}")

        response = self.trakt_auth.post(f"/sync/history", json=payload)

        if response.status_code != 201:
            log(f"[Queue] Failed to mark episode as watched: {response.status_code}")
        else:
            log("[Queue] Successfully marked episode as watched")

    def mark_watched_movie(self, row):
        percent_watched = row['payload'].get('percent_watched', 100)
        
        # Trakt does not support partial percent watched in history, 
        # so we only sync if it's 100% or we send it as fully watched 
        # because the internal Orac DB took care of the partial status locally
        
        payload = {
            "movies": [
                {
                    "watched_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "ids": {
                        "trakt": 0,
                        "tmdb": row['payload'].get('tmdb_id')
                    }
                }
            ]
        }

        log(f"[Queue] Marking movie as watched: {payload}")

        response = self.trakt_auth.post(f"/sync/history", json=payload)

        if response.status_code != 201:
            log(f"[Queue] Failed to mark movie as watched: {response.status_code}")
        else:
            log("[Queue] Successfully marked movie as watched")

    def drop_show(self, row):
        # The payload contains {"shows": [{"ids": {"trakt": 12345, "tmdb": 67890}}]}
        # Note: We need the TMDB id for Simkl ideally, but we can try to extract it from the local DB if needed.
        # Let's extract what we have
        shows = row['payload'].get('shows', [])
        if not shows:
            return

        # 1. Update Trakt
        # Trakt endpoint: POST /users/hidden/dropped
        # Payload format is the same: {"shows": [{"ids": {"trakt": id}}]}
        log(f"[Queue] Marking show as dropped on Trakt: {row['payload']}")
        response = self.trakt_auth.post("/users/hidden/dropped", json={"shows": shows})
        if response.status_code not in (200, 201):
            log(f"[Queue] Failed to mark show as dropped on Trakt: {response.status_code}")
            # we do not fail the whole row if Simkl succeeds or fails, just log it.
        else:
            log("[Queue] Successfully marked show as dropped on Trakt")
            
        # 2. Update Simkl
        # To get the tmdb id we might need to query the database since `watched.py` only queued the Trakt ID.
        # But wait, we can just look up the TMDB id if it's not in the payload.
        # We can extract the Trakt ID and look up its TMDB ID in the shows table.
        show_trakt_id = shows[0].get("ids", {}).get("trakt")
        if show_trakt_id:
            show_tmdb_id = None
            with sqlite3.connect(self.tvshows_static_db) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT tmdb_id FROM shows WHERE show_trakt_id = ?", (show_trakt_id,))
                row_db = cursor.fetchone()
                if row_db:
                    show_tmdb_id = row_db[0]
            
            if show_tmdb_id:
                simkl_token = get_config_value("simkl.token", self.config_db_path)
                simkl_client = get_config_value("simkl.client", self.config_db_path)
                if simkl_token and simkl_client:
                    headers = {
                        'Content-Type': 'application/json',
                        'simkl-api-key': simkl_client,
                        'Authorization': f'Bearer {simkl_token}'
                    }
                    simkl_payload = {
                        "shows": [
                            {
                                "ids": {"tmdb": show_tmdb_id},
                                "status": "dropped"
                            }
                        ]
                    }
                    log(f"[Queue] Marking show {show_tmdb_id} as dropped on Simkl")
                    import requests
                    try:
                        resp = requests.post("https://api.simkl.com/sync/add-items", json=simkl_payload, headers=headers, timeout=20)
                        if resp.status_code not in (200, 201):
                            log(f"[Queue] Failed to drop show on Simkl: {resp.status_code} - {resp.text}", level=LOGERROR)
                        else:
                            log("[Queue] Successfully dropped show on Simkl")
                    except Exception as e:
                        log(f"[Queue] Simkl drop request failed: {e}", level=LOGERROR)


    def add_to_list_trakt(self, row):
        list_id = row['payload'].get('list_name')
        tmdb_id = row['payload'].get('tmdb_id')
        media_type = row['payload'].get('item_type', 'movie')  # Default to movie if not specified
        slug = row['payload'].get('slug')


        if not list_id or not tmdb_id:
            log(f"[Queue] Missing list_id or tmdb_id in payload for add_to_list", LOGERROR)
            return
        
        # The payload needs to be structured with a key of "movies" or "shows"
        if media_type == 'movie':
            payload = {
                "movies": [{"ids": {"tmdb": tmdb_id}}]
            }
        elif media_type == 'tvshow':
            payload = {
                "shows": [{"ids": {"tmdb": tmdb_id}}]
            }
        else:
            log(f"[Queue] Unsupported media_type for add_to_list: {media_type}", LOGERROR)
            # Mark as failed because we can't process this
            self._mark_status(row['id'], 'failed')
            return

        # The watchlist has a different endpoint from custom lists.
        if slug == 'watchlist':
            endpoint = "/sync/watchlist"
            log(f"[Queue] Adding {media_type} with TMDb ID {tmdb_id} to watchlist")
        elif slug == 'favorites':
            endpoint = "/sync/favorites"
            log(f"[Queue] Adding {media_type} with TMDb ID {tmdb_id} to favorites")
        else:
            endpoint = f"/users/me/lists/{slug}/items"
            log(f"[Queue] Adding {media_type} with TMDb ID {tmdb_id} to list '{list_id}'")

        response = self.trakt_auth.post(endpoint, json=payload)

        if response.status_code != 201:
            log(f"[Queue] Failed to add item to list: {response.status_code} - {response.text}", LOGERROR)
        else:
            log(f"[Queue] Successfully added item to list {list_id}")

        return response.status_code == 201

    def remove_from_list_trakt(self, row):
        list_id = row['payload'].get('list_name')
        tmdb_id = row['payload'].get('tmdb_id')
        media_type = row['payload'].get('item_type', 'movie')  # Default to movie if not specified
        slug = row['payload'].get('slug')

        if not list_id or not tmdb_id:
            log(f"[Queue] Missing list_id or tmdb_id in payload for remove_from_list", LOGERROR)
            return

        # The payload needs to be structured with a key of "movies" or "shows"
        if media_type == 'movie':
            payload = {
                "movies": [{"ids": {"tmdb": tmdb_id}}]
            }
        elif media_type == 'tvshow':
            payload = {
                "shows": [{"ids": {"tmdb": tmdb_id}}]
            }
        else:
            log(f"[Queue] Unsupported media_type for remove_from_list: {media_type}", LOGERROR)
            # Mark as failed because we can't process this
            self._mark_status(row['id'], 'failed')
            return

        # The watchlist has a different endpoint from custom lists.
        if slug == 'watchlist':
            endpoint = "/sync/watchlist/remove"
            log(f"[Queue] Removing {media_type} with TMDb ID {tmdb_id} from watchlist")
        elif slug == 'favorites':
            endpoint = "/sync/favorites/remove"
            log(f"[Queue] Removing {media_type} with TMDb ID {tmdb_id} from favorites")
        else:
            endpoint = f"/users/me/lists/{slug}/items/remove"
            log(f"[Queue] Removing {media_type} with TMDb ID {tmdb_id} from list '{list_id}'")

        response = self.trakt_auth.post(endpoint, json=payload)

        if response.status_code != 200:
            log(f"[Queue] Failed to remove item from list: {response.status_code} - {response.text}", LOGERROR)
        else:
            log(f"[Queue] Successfully removed item from list {list_id}")

        return response.status_code == 200