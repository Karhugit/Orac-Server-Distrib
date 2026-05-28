from __future__ import annotations
"""
trakt_maintenance_worker.py
===========================
Background worker that:
  1. Ingests "watched" events from the local update_queue DB and POSTs them to
     POST /sync/history, then records the returned trakt_history_id locally.
  2. Periodically checks whether the Trakt account history has exceeded 90,000
     items and, if so, deletes the oldest ones from Trakt (FIFO) while flagging
     them locally (is_on_trakt = False).

Tier / API standards
--------------------
- All paginated GET requests are capped at limit=250 (June 2026 hard limit).
- If X-Pagination-Page-Count > 1 the worker fetches subsequent pages.
- "Liked Lists" are fetched up to 5,000 items even on free tier.

Rate-limit awareness
--------------------
- After every API call the worker reads X-Ratelimit-Remaining and
  X-Ratelimit-Reset.  If Remaining < 5 it sleeps until Reset + 0.5 s.
- On HTTP 429 it sleeps for exactly Retry-After seconds.

Database
--------
Uses the trakt_history_sync table (created by init_trakt_history_sync_db).
UPSERT logic prevents duplicates.  The table maps:
  local_id  ↔  trakt_history_id  ↔  is_on_trakt

Usage
-----
    worker = TraktMaintenanceWorker(
        trakt_auth=trakt_handler,
        movies_dynamic_db_path=...,
        tvshows_dynamic_db_path=...,
        update_queue_path=...,
        history_sync_db_path=...,
        db_manager=db_manager,
        sync_interval=300,          # seconds between queue drains
        maintenance_interval=3600,  # seconds between 90k checks
        history_check_every=10,     # run 90k check after every N syncs
        history_ceiling=90_000,
    )
    worker.start()
    …
    worker.stop()
"""

import math
import sqlite3
import time
import json
import threading
from datetime import datetime, timezone
from threading import Thread, Event

import requests  # already a dependency via trakt_handler

from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TRAKT_BASE = "https://api.trakt.tv"
PAGE_SIZE = 250          # June 2026 hard max per paginated request
LIKED_LIST_MAX = 5_000   # special ceiling for liked lists (even free tier)
HISTORY_CEILING = 90_000 # max Trakt history items allowed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_rate_limit_headers(headers: dict) -> tuple[int, float]:
    """
    Returns (remaining, reset_at_epoch_float).
    reset_at_epoch is the Unix timestamp at which the window resets.
    Trakt sends X-Ratelimit-Reset as an integer Unix timestamp (seconds).
    """
    try:
        remaining = int(headers.get("X-Ratelimit-Remaining", 10))
    except (ValueError, TypeError):
        remaining = 10

    try:
        reset_at = float(headers.get("X-Ratelimit-Reset", time.time() + 1))
    except (ValueError, TypeError):
        reset_at = time.time() + 1

    return remaining, reset_at


# ---------------------------------------------------------------------------
# TraktMaintenanceWorker
# ---------------------------------------------------------------------------

class TraktMaintenanceWorker:
    """
    Tier-aware, rate-limit-aware Trakt history sync and 90k maintenance worker.
    """

    def __init__(
        self,
        trakt_auth,                 # TraktAuth instance (from trakt_handler.py)
        movies_dynamic_db_path: str,
        tvshows_dynamic_db_path: str,
        update_queue_path: str,
        history_sync_db_path: str,  # dedicated DB for trakt_history_sync table
        db_manager,                 # DatabaseManager singleton
        sync_interval: int = 300,
        maintenance_interval: int = 3600,
        history_check_every: int = 10,
        history_ceiling: int = HISTORY_CEILING,
    ):
        self.trakt_auth = trakt_auth
        self.movies_dynamic_db = movies_dynamic_db_path
        self.tvshows_dynamic_db = tvshows_dynamic_db_path
        self.update_queue_db = update_queue_path
        self.history_sync_db = history_sync_db_path
        self.db_manager = db_manager

        self.sync_interval = sync_interval
        self.maintenance_interval = maintenance_interval
        self.history_check_every = history_check_every
        self.history_ceiling = history_ceiling

        self._stop_event = Event()
        self._pause_event = Event()
        self._pause_event.set()  # Start in running state

        self._sync_thread: Thread | None = None
        self._maintenance_thread: Thread | None = None

        # Mutable state shared only within the sync thread
        self._sync_count_since_last_check = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if self._sync_thread is None or not self._sync_thread.is_alive():
            self._stop_event.clear()
            self._pause_event.set()
            self._sync_thread = Thread(
                target=self._sync_loop, daemon=True, name="TraktSyncWorker"
            )
            self._sync_thread.start()
            log("[TraktMW] Sync thread started.", level=LOGINFO)

        if self._maintenance_thread is None or not self._maintenance_thread.is_alive():
            self._maintenance_thread = Thread(
                target=self._maintenance_loop,
                daemon=True,
                name="TraktMaintenanceWorker",
            )
            self._maintenance_thread.start()
            log("[TraktMW] Maintenance thread started.", level=LOGINFO)

    def stop(self):
        self._stop_event.set()
        self._pause_event.set()  # unblock any wait()
        for t in (self._sync_thread, self._maintenance_thread):
            if t and t.is_alive():
                t.join(timeout=10)
        log("[TraktMW] Worker stopped.", level=LOGINFO)

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    # ------------------------------------------------------------------
    # Loop controllers
    # ------------------------------------------------------------------

    def _sync_loop(self):
        while not self._stop_event.is_set():
            self._pause_event.wait()
            try:
                self.process_queue()
            except Exception as exc:
                log(f"[TraktMW] Unhandled error in sync loop: {exc}", level=LOGERROR)
            self._stop_event.wait(timeout=self.sync_interval)

    def _maintenance_loop(self):
        while not self._stop_event.is_set():
            self._pause_event.wait()
            try:
                self.check_history_limit()
            except Exception as exc:
                log(f"[TraktMW] Unhandled error in maintenance loop: {exc}", level=LOGERROR)
            self._stop_event.wait(timeout=self.maintenance_interval)

    # ------------------------------------------------------------------
    # Public API (can also be called directly for testing)
    # ------------------------------------------------------------------

    def process_queue(self):
        """
        Drain all pending 'watched_movie' and 'watched_episode' rows from the
        update_queue, POST each to Trakt's /sync/history, and record the
        returned trakt_history_id in the local trakt_history_sync table.
        """
        try:
            conn = self.db_manager.get_connection(self.update_queue_db)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, update_type, payload, media_type
                FROM update_queue
                WHERE (status = 'pending' OR status = 'retry')
                  AND update_type IN ('watched_movie', 'watched_episode')
                ORDER BY priority, created_at ASC
                """
            )
            rows = cursor.fetchall()
            conn.close()
        except Exception as exc:
            log(f"[TraktMW] Failed to read update_queue: {exc}", level=LOGERROR)
            return

        if not rows:
            return

        log(f"[TraktMW] Processing {len(rows)} watched event(s) from queue.", level=LOGINFO)

        for row in rows:
            if self._stop_event.is_set():
                break
            try:
                self._process_watched_row(dict(row))
                self._sync_count_since_last_check += 1

                # Trigger 90k maintenance check after every N successful syncs
                if self._sync_count_since_last_check >= self.history_check_every:
                    self._sync_count_since_last_check = 0
                    self.check_history_limit()

            except Exception as exc:
                log(f"[TraktMW] Error processing queue row {row['id']}: {exc}", level=LOGERROR)
                self._set_queue_status(row["id"], "failed")

    def check_history_limit(self):
        """
        Checks the total Trakt history item count.  If it exceeds
        self.history_ceiling, deletes the oldest items (FIFO) from Trakt and
        flags them locally as is_on_trakt = False.
        """
        log("[TraktMW] Checking Trakt history count…", level=LOGINFO)

        total = self._get_history_count()
        if total is None:
            log("[TraktMW] Could not determine history count.", level=LOGWARNING)
            return

        log(f"[TraktMW] Trakt history count: {total}/{self.history_ceiling}", level=LOGINFO)

        if total <= self.history_ceiling:
            return

        overflow = total - self.history_ceiling
        log(f"[TraktMW] Overflow: {overflow} items need to be pruned.", level=LOGWARNING)

        # Fetch oldest items (last page)
        oldest_items = self._fetch_oldest_history_items(total, overflow)
        if not oldest_items:
            log("[TraktMW] No oldest items found to prune.", level=LOGWARNING)
            return

        # Verify against local DB and build a list of history_ids to delete
        verified_ids = self._verify_items_exist_locally(oldest_items)
        if not verified_ids:
            log("[TraktMW] None of the oldest items verified locally; skipping purge.", level=LOGWARNING)
            return

        log(f"[TraktMW] Purging {len(verified_ids)} items from Trakt history.", level=LOGINFO)
        self._purge_from_trakt(verified_ids)

    def handle_rate_limits(self, response: requests.Response):
        """
        Inspects rate-limit headers on a response and sleeps if necessary.
        Call this after *every* API request.
        """
        if response is None:
            return

        # Handle hard rate-limit (429)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", 5))
            log(
                f"[TraktMW] 429 Too Many Requests — sleeping {retry_after}s.",
                level=LOGWARNING,
            )
            time.sleep(retry_after)
            return

        remaining, reset_at = _parse_rate_limit_headers(response.headers)
        if remaining < 5:
            sleep_for = max(0.0, reset_at - time.time()) + 0.5
            log(
                f"[TraktMW] Rate limit low (remaining={remaining}). "
                f"Sleeping {sleep_for:.1f}s until window reset.",
                level=LOGWARNING,
            )
            time.sleep(sleep_for)

    # ------------------------------------------------------------------
    # Internal: queue processing
    # ------------------------------------------------------------------

    def _process_watched_row(self, row: dict):
        """Builds the Trakt payload for one watched event and POSTs it."""
        payload_data = row["payload"]
        if isinstance(payload_data, str):
            payload_data = json.loads(payload_data)

        update_type = row["update_type"]
        row_id = row["id"]
        media_type = row.get("media_type", "movie")

        self._set_queue_status(row_id, "processing")

        # Build the /sync/history payload
        watched_at = _utcnow_str()

        if update_type == "watched_movie":
            tmdb_id = payload_data.get("tmdb_id")
            trakt_id = payload_data.get("trakt_id")
            if not tmdb_id and not trakt_id:
                log(f"[TraktMW] Row {row_id}: no valid IDs for movie — skipping.", level=LOGWARNING)
                self._set_queue_status(row_id, "failed")
                return
            ids_block = {}
            if trakt_id and int(trakt_id) > 0:
                ids_block["trakt"] = int(trakt_id)
            if tmdb_id and int(tmdb_id) > 0:
                ids_block["tmdb"] = int(tmdb_id)
            sync_payload = {
                "movies": [{"watched_at": watched_at, "ids": ids_block}]
            }
            local_tmdb_id = int(tmdb_id) if tmdb_id else None

        elif update_type == "watched_episode":
            episode_trakt_id = payload_data.get("episode_trakt_id")
            episode_tmdb_id = payload_data.get("tmdb_id")
            ids_block = {}
            if episode_trakt_id and int(episode_trakt_id) > 0:
                ids_block["trakt"] = int(episode_trakt_id)
            if episode_tmdb_id and int(episode_tmdb_id) > 0:
                ids_block["tmdb"] = int(episode_tmdb_id)
            if not ids_block:
                log(f"[TraktMW] Row {row_id}: no valid IDs for episode — skipping.", level=LOGWARNING)
                self._set_queue_status(row_id, "failed")
                return
            sync_payload = {
                "episodes": [{"watched_at": watched_at, "ids": ids_block}]
            }
            local_tmdb_id = int(episode_tmdb_id) if episode_tmdb_id else None
        else:
            log(f"[TraktMW] Row {row_id}: unknown update_type '{update_type}'.", level=LOGWARNING)
            self._set_queue_status(row_id, "failed")
            return

        # POST to Trakt
        response = self._api_post("/sync/history", sync_payload)
        self.handle_rate_limits(response)

        if response is None or response.status_code != 201:
            status_code = response.status_code if response else "N/A"
            log(
                f"[TraktMW] Failed to sync row {row_id} to Trakt (HTTP {status_code}).",
                level=LOGERROR,
            )
            self._set_queue_status(row_id, "retry")
            return

        # Extract the trakt_history_id from the response
        trakt_history_id = self._extract_history_id(response, update_type)

        # Persist to local trakt_history_sync table (UPSERT)
        media_kind = "movie" if update_type == "watched_movie" else "episode"
        self._upsert_history_record(
            local_id=row_id,
            tmdb_id=local_tmdb_id,
            media_type=media_kind,
            trakt_history_id=trakt_history_id,
            watched_at=watched_at,
        )

        self._set_queue_status(row_id, "done")
        log(
            f"[TraktMW] Row {row_id} synced to Trakt (history_id={trakt_history_id}).",
            level=LOGINFO,
        )

    def _extract_history_id(self, response: requests.Response, update_type: str) -> int | None:
        """
        Trakt's POST /sync/history response body:
        { "added": {"movies": 1, "episodes": 1}, "ids": [12345678] }
        where "ids" lists the newly created history IDs.
        """
        try:
            data = response.json()
            ids = data.get("ids") or data.get("added_ids")
            if ids and len(ids) > 0:
                return int(ids[0])
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Internal: 90k maintenance
    # ------------------------------------------------------------------

    def _get_history_count(self) -> int | None:
        """
        GET /users/me/history?limit=1  and read X-Pagination-Item-Count header.
        Returns the total history item count, or None on failure.
        """
        response = self._api_get("/users/me/history", params={"limit": 1})
        self.handle_rate_limits(response)

        if response is None or response.status_code != 200:
            return None

        try:
            total = int(response.headers.get("X-Pagination-Item-Count", 0))
            return total
        except (ValueError, TypeError):
            return None

    def _fetch_oldest_history_items(self, total: int, needed: int) -> list[dict]:
        """
        Fetches the oldest 'needed' items from Trakt history by targeting the
        last paginated page(s) and working backwards.

        Returns a list of dicts: {history_id, type, tmdb_id, trakt_id}
        """
        items: list[dict] = []
        total_pages = math.ceil(total / PAGE_SIZE)

        log(
            f"[TraktMW] Fetching oldest items from history "
            f"(total={total}, pages={total_pages}, need={needed}).",
            level=LOGDEBUG,
        )

        page = total_pages
        while page >= 1 and len(items) < needed:
            if self._stop_event.is_set():
                break

            response = self._api_get(
                "/users/me/history",
                params={"limit": PAGE_SIZE, "page": page},
            )
            self.handle_rate_limits(response)

            if response is None or response.status_code != 200:
                log(f"[TraktMW] Failed to fetch history page {page}.", level=LOGWARNING)
                break

            data = response.json()
            if not data:
                break

            # We are reading oldest-first by going backwards through pages;
            # the last page contains the oldest items.
            items.extend(self._parse_history_items(data))
            page -= 1

        return items[:needed]

    def _parse_history_items(self, data: list) -> list[dict]:
        """Convert raw Trakt history list to simplified dicts."""
        parsed = []
        for entry in data:
            history_id = entry.get("id")
            item_type = entry.get("type")  # "movie" or "episode"
            media = entry.get(item_type) or {}
            ids = media.get("ids", {})
            parsed.append(
                {
                    "history_id": history_id,
                    "type": item_type,
                    "trakt_id": ids.get("trakt"),
                    "tmdb_id": ids.get("tmdb"),
                    "imdb_id": ids.get("imdb"),
                }
            )
        return parsed

    def _verify_items_exist_locally(self, items: list[dict]) -> list[int]:
        """
        Cross-reference the items (from Trakt history) against the local
        trakt_history_sync table.  Returns a list of trakt_history_ids that
        are known locally and marked is_on_trakt = True.

        Items that are NOT found locally are still included so we don't
        orphan them on Trakt — we just flag that fact in the log.
        """
        history_ids = [item["history_id"] for item in items if item.get("history_id")]
        if not history_ids:
            return []

        locally_known: set[int] = set()
        try:
            with self.db_manager.connection(self.history_sync_db) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                placeholders = ",".join("?" * len(history_ids))
                cursor.execute(
                    f"""
                    SELECT trakt_history_id
                    FROM trakt_history_sync
                    WHERE trakt_history_id IN ({placeholders})
                      AND is_on_trakt = 1
                    """,
                    history_ids,
                )
                locally_known = {row[0] for row in cursor.fetchall()}
        except Exception as exc:
            log(f"[TraktMW] Error verifying items locally: {exc}", level=LOGERROR)

        unknown_count = len(history_ids) - len(locally_known)
        if unknown_count > 0:
            log(
                f"[TraktMW] {unknown_count} history items not found locally "
                f"(will still be purged from Trakt).",
                level=LOGWARNING,
            )

        return history_ids  # purge all fetched items regardless

    def _purge_from_trakt(self, history_ids: list[int]):
        """
        POST /sync/history/remove  with chunks of history IDs, then marks
        them as is_on_trakt = False in the local DB.
        Trakt removes by id when you pass {"ids": [...]} (list of ints).
        """
        # Trakt recommends batching; use chunks of 100 to be safe
        CHUNK = 100
        for i in range(0, len(history_ids), CHUNK):
            if self._stop_event.is_set():
                break

            chunk = history_ids[i : i + CHUNK]
            payload = {"ids": chunk}

            response = self._api_post("/sync/history/remove", payload)
            self.handle_rate_limits(response)

            if response is None or response.status_code not in (200, 201):
                status = response.status_code if response else "N/A"
                log(
                    f"[TraktMW] Failed to purge history chunk (HTTP {status}). "
                    f"IDs: {chunk[:5]}…",
                    level=LOGERROR,
                )
                continue

            # Flag locally
            self._flag_not_on_trakt(chunk)
            log(
                f"[TraktMW] Purged {len(chunk)} history items from Trakt.",
                level=LOGINFO,
            )

    # ------------------------------------------------------------------
    # Internal: database helpers
    # ------------------------------------------------------------------

    def _upsert_history_record(
        self,
        local_id: int,
        tmdb_id: int | None,
        media_type: str,
        trakt_history_id: int | None,
        watched_at: str,
    ):
        """UPSERT a record in trakt_history_sync."""
        try:
            with self.db_manager.connection(self.history_sync_db) as conn:
                conn.execute(
                    """
                    INSERT INTO trakt_history_sync
                        (local_id, tmdb_id, media_type, trakt_history_id, watched_at, is_on_trakt, synced_at)
                    VALUES (?, ?, ?, ?, ?, 1, strftime('%s','now'))
                    ON CONFLICT(local_id) DO UPDATE SET
                        trakt_history_id = excluded.trakt_history_id,
                        is_on_trakt      = 1,
                        synced_at        = excluded.synced_at
                    """,
                    (local_id, tmdb_id, media_type, trakt_history_id, watched_at),
                )
        except Exception as exc:
            log(f"[TraktMW] Failed to upsert history record: {exc}", level=LOGERROR)

    def _flag_not_on_trakt(self, history_ids: list[int]):
        """Mark rows is_on_trakt = False without deleting the local record."""
        if not history_ids:
            return
        try:
            placeholders = ",".join("?" * len(history_ids))
            with self.db_manager.connection(self.history_sync_db) as conn:
                conn.execute(
                    f"""
                    UPDATE trakt_history_sync
                    SET is_on_trakt = 0,
                        purged_at   = strftime('%s','now')
                    WHERE trakt_history_id IN ({placeholders})
                    """,
                    history_ids,
                )
        except Exception as exc:
            log(f"[TraktMW] Failed to flag items as not-on-trakt: {exc}", level=LOGERROR)

    def _set_queue_status(self, row_id: int, status: str):
        try:
            with self.db_manager.connection(self.update_queue_db) as conn:
                conn.execute(
                    "UPDATE update_queue SET status = ?, updated_at = strftime('%s','now') WHERE id = ?",
                    (status, row_id),
                )
        except Exception as exc:
            log(f"[TraktMW] Failed to update queue status for row {row_id}: {exc}", level=LOGERROR)

    # ------------------------------------------------------------------
    # Internal: HTTP wrappers (synchronous, blocking — runs in daemon thread)
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict:
        from resources.lib.config_handler import get_trakt_access_token, get_trakt_client_id
        access_token = get_trakt_access_token(self.trakt_auth.config_db_path)
        client_id = self.trakt_auth.client_id or get_trakt_client_id(self.trakt_auth.config_db_path)
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": client_id or "",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _api_get(self, endpoint: str, params: dict | None = None) -> requests.Response | None:
        """Authenticated GET with automatic 401 retry after token refresh."""
        self.trakt_auth._ensure_token_fresh()
        url = f"{TRAKT_BASE}{endpoint}"
        try:
            resp = requests.get(url, headers=self._build_headers(), params=params, timeout=30)
            if resp.status_code == 401:
                log("[TraktMW] 401 on GET — refreshing token and retrying.", level=LOGWARNING)
                self.trakt_auth.refresh_token()
                resp = requests.get(url, headers=self._build_headers(), params=params, timeout=30)
            return resp
        except requests.RequestException as exc:
            log(f"[TraktMW] GET {endpoint} failed: {exc}", level=LOGERROR)
            return None

    def _api_post(self, endpoint: str, payload: dict) -> requests.Response | None:
        """Authenticated POST with automatic 401 retry after token refresh."""
        self.trakt_auth._ensure_token_fresh()
        url = f"{TRAKT_BASE}{endpoint}"
        try:
            resp = requests.post(url, headers=self._build_headers(), json=payload, timeout=30)
            if resp.status_code == 401:
                log("[TraktMW] 401 on POST — refreshing token and retrying.", level=LOGWARNING)
                self.trakt_auth.refresh_token()
                resp = requests.post(url, headers=self._build_headers(), json=payload, timeout=30)
            return resp
        except requests.RequestException as exc:
            log(f"[TraktMW] POST {endpoint} failed: {exc}", level=LOGERROR)
            return None

    # ------------------------------------------------------------------
    # Optional: paginate liked lists (up to LIKED_LIST_MAX items)
    # ------------------------------------------------------------------

    def fetch_liked_lists(self) -> list[dict]:
        """
        Fetches all liked lists for the authenticated user, respecting the
        5,000-item ceiling even on free-tier accounts.
        Returns a list of raw list objects from the Trakt API.
        """
        endpoint = "/users/me/likes/lists"
        all_items: list[dict] = []
        page = 1

        while len(all_items) < LIKED_LIST_MAX:
            if self._stop_event.is_set():
                break

            response = self._api_get(
                endpoint,
                params={"limit": PAGE_SIZE, "page": page},
            )
            self.handle_rate_limits(response)

            if response is None or response.status_code != 200:
                break

            data = response.json()
            if not data:
                break

            all_items.extend(data)

            total_pages = int(response.headers.get("X-Pagination-Page-Count", 1))
            if page >= total_pages:
                break

            page += 1

        return all_items[:LIKED_LIST_MAX]
