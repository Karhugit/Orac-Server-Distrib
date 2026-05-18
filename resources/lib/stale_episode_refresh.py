"""
stale_episode_refresh.py
========================
Background worker that periodically identifies episodes in the local DB that
have already aired but are missing key metadata (runtime, rating, overview,
thumbnail, imdb_id, tvdb_id, etc.), then re-fetches fresh data from TMDB to
fill in the gaps.

Why this is needed
------------------
When a show is first synced, future episodes are often added to TMDB/Trakt
before they air. At that point many fields (runtime, rating, votes, overview,
still image) are simply not available yet. The initial INSERT writes zeros/
nulls for those fields and there is no mechanism to go back and update them
once the episode airs and TMDB populates the data.

This worker is self-healing: it runs on a configurable cycle (default 24 h)
and quietly patches every aired episode whose metadata looks incomplete.

Staleness criteria (any one of these triggers a refresh)
---------------------------------------------------------
  - runtime IS NULL OR runtime = 0
  - rating  IS NULL OR rating  = 0.0
  - votes   IS NULL OR votes   = 0
  - episode_overview IS NULL OR episode_overview = ''
  - episode_thumbnail_path IS NULL
  - imdb_id IS NULL
  - tvdb_id IS NULL

Only episodes where DATE(air_date) <= DATE('now') are considered — there is
no point refreshing future episodes as the data won't be there yet.

Fields updated on each refresh
-------------------------------
  runtime, rating, votes, episode_overview, episode_title, original_title,
  imdb_id, tvdb_id, episode_thumbnail_path

Trakt IDs (episode_trakt_id) are updated too when the episode has a valid
Trakt entry in the lookup.

Usage
-----
    worker = StaleEpisodeRefreshWorker(
        tmdb_handler=tmdb_handler,
        tvshows_static_db_path=...,
        refresh_interval=86400,   # seconds; default 24 h
        batch_size=100,           # episodes refreshed per cycle
        startup_delay=120,        # seconds to wait before first run
    )
    worker.start()
    …
    worker.stop()
"""

import sqlite3
import time
from threading import Thread, Event

import requests

from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGDEBUG, LOGWARNING


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_stale(row: dict) -> bool:
    """
    Returns True if the episode row is missing any important metadata field.
    A value of 0 / 0.0 / '' / None all count as "missing".
    """
    if not row.get("runtime"):
        return True
    if not row.get("rating"):
        return True
    if not row.get("votes"):
        return True
    if not row.get("episode_overview"):
        return True
    if not row.get("episode_thumbnail_path"):
        return True
    if not row.get("imdb_id"):
        return True
    if not row.get("tvdb_id"):
        return True
    return False


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class StaleEpisodeRefreshWorker:
    """
    Daemon thread that refreshes stale episode metadata from TMDB.
    """

    def __init__(
        self,
        tmdb_handler,
        tvshows_static_db_path: str,
        trakt_handler=None,
        refresh_interval: int = 86400,   # 24 hours
        batch_size: int = 100,
        startup_delay: int = 120,        # give server time to fully start
    ):
        self.tmdb = tmdb_handler
        self.trakt = trakt_handler   # optional; used as fallback for runtime etc.
        self.db_path = tvshows_static_db_path
        self.refresh_interval = refresh_interval
        self.batch_size = batch_size
        self.startup_delay = startup_delay

        self._stop_event = Event()
        self._thread: Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._ensure_schema()
            self._stop_event.clear()
            self._thread = Thread(
                target=self._run_loop,
                daemon=True,
                name="StaleEpisodeRefreshWorker",
            )
            self._thread.start()
            log("[StaleEpisodeRefresh] Worker started.", level=LOGINFO)

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=15)
        log("[StaleEpisodeRefresh] Worker stopped.", level=LOGINFO)

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def _run_loop(self):
        # Wait a short period after server start before the first pass so
        # we don't compete with the initial sync.
        if self._stop_event.wait(timeout=self.startup_delay):
            return  # stop was requested during delay

        while not self._stop_event.is_set():
            try:
                self.run_refresh_pass()
            except Exception as exc:
                log(f"[StaleEpisodeRefresh] Unhandled error in refresh pass: {exc}", level=LOGERROR)
            # Sleep for the configured interval, but wake up immediately if stopped.
            self._stop_event.wait(timeout=self.refresh_interval)

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def run_refresh_pass(self):
        """
        Single end-to-end refresh pass:
          1. Find stale aired episodes (up to batch_size).
          2. For each, call TMDB /tv/{show_tmdb_id}/season/{s}/episode/{e}.
          3. Write back any newly-available fields.
        """
        log("[StaleEpisodeRefresh] Starting stale episode refresh pass.", level=LOGINFO)
        start_time = time.time()

        stale_episodes = self._find_stale_episodes()
        if not stale_episodes:
            log("[StaleEpisodeRefresh] No stale episodes found.", level=LOGINFO)
            return

        log(f"[StaleEpisodeRefresh] Found {len(stale_episodes)} stale episode(s) to refresh.", level=LOGINFO)

        refreshed = 0
        skipped = 0
        failed = 0

        for ep in stale_episodes:
            if self._stop_event.is_set():
                log("[StaleEpisodeRefresh] Stop requested mid-pass; aborting.", level=LOGINFO)
                break

            show_tmdb_id  = ep["show_tmdb_id"]
            season        = ep["season"]
            episode_num   = ep["episode_number"]
            ep_tmdb_id    = ep["tmdb_id"]
            show_slug     = ep.get("slug")
            show_trakt_id = ep.get("show_trakt_id")

            try:
                tmdb_data  = self._fetch_tmdb_episode(show_tmdb_id, season, episode_num)
                trakt_data = self._fetch_trakt_episode(show_slug, show_trakt_id, season, episode_num)

                if not tmdb_data and not trakt_data:
                    skipped += 1
                    continue

                if self._update_episode(ep_tmdb_id, ep, tmdb_data or {}, trakt_data or {}, show_tmdb_id):
                    refreshed += 1
                else:
                    failed += 1

                # Small throttle so we don't hammer APIs if the batch is large.
                time.sleep(0.25)

            except Exception as exc:
                log(
                    f"[StaleEpisodeRefresh] Error refreshing S{season}E{episode_num} "
                    f"(show_tmdb={show_tmdb_id}): {exc}",
                    level=LOGERROR,
                )
                failed += 1

        elapsed = time.time() - start_time
        log(
            f"[StaleEpisodeRefresh] Pass complete in {elapsed:.1f}s — "
            f"refreshed={refreshed}, skipped={skipped}, failed={failed}.",
            level=LOGINFO,
        )

    # ------------------------------------------------------------------
    # Schema migration
    # ------------------------------------------------------------------

    def _ensure_schema(self):
        """
        Safely adds the metadata_refreshed_at column to the episodes table if
        it doesn't already exist.  This is an additive migration — safe to run
        on an existing database.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                conn.execute(
                    "ALTER TABLE episodes ADD COLUMN metadata_refreshed_at INTEGER DEFAULT 0"
                )
                conn.commit()
                log("[StaleEpisodeRefresh] Added metadata_refreshed_at column to episodes.", level=LOGINFO)
        except sqlite3.OperationalError:
            # Column already exists — this is expected after first run.
            pass
        except Exception as exc:
            log(f"[StaleEpisodeRefresh] Schema migration error: {exc}", level=LOGERROR)

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _find_stale_episodes(self) -> list[dict]:
        """
        Query the DB for aired episodes that have at least one missing/default
        metadata field AND have not been refresh-attempted in the last 7 days.
        Returns up to self.batch_size rows as dicts.
        """
        cooldown = 7 * 86400   # 7 days in seconds
        try:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT
                        e.tmdb_id,
                        s.show_tmdb_id,
                        s.show_trakt_id,
                        s.slug,
                        e.season,
                        e.episode_number,
                        e.runtime,
                        e.rating,
                        e.votes,
                        e.episode_overview,
                        e.episode_thumbnail_path,
                        e.imdb_id,
                        e.tvdb_id,
                        e.episode_title,
                        e.episode_type,
                        e.air_date
                    FROM episodes e
                    JOIN shows s ON e.show_id = s.show_tmdb_id
                    WHERE DATE(e.air_date) <= DATE('now')
                      AND (
                            e.runtime               IS NULL OR e.runtime  = 0
                         OR e.rating                IS NULL OR e.rating   = 0.0
                         OR e.votes                 IS NULL OR e.votes    = 0
                         OR e.episode_overview      IS NULL OR e.episode_overview = ''
                         OR e.episode_thumbnail_path IS NULL
                         OR e.imdb_id               IS NULL
                         OR e.tvdb_id               IS NULL
                      )
                      AND (
                            e.metadata_refreshed_at IS NULL
                         OR e.metadata_refreshed_at = 0
                         OR e.metadata_refreshed_at < (unixepoch() - ?)
                      )
                    ORDER BY e.air_date DESC
                    LIMIT ?
                    """,
                    (cooldown, self.batch_size),
                )
                rows = cursor.fetchall()
                return [dict(r) for r in rows]
        except Exception as exc:
            log(f"[StaleEpisodeRefresh] Error querying stale episodes: {exc}", level=LOGERROR)
            return []

    def _fetch_tmdb_episode(self, show_tmdb_id: int, season: int, episode_number: int) -> dict | None:
        """
        Calls TMDB /tv/{show_id}/season/{s}/episode/{e}?append_to_response=external_ids
        and returns the raw response dict, or None on failure.
        """
        try:
            data = self.tmdb._get(
                f"/tv/{show_tmdb_id}/season/{season}/episode/{episode_number}",
                params={"append_to_response": "external_ids"},
            )
            if not data:
                log(
                    f"[StaleEpisodeRefresh] TMDB returned empty for show={show_tmdb_id} "
                    f"S{season}E{episode_number}.",
                    level=LOGDEBUG,
                )
            return data
        except Exception as exc:
            log(
                f"[StaleEpisodeRefresh] TMDB fetch error show={show_tmdb_id} "
                f"S{season}E{episode_number}: {exc}",
                level=LOGERROR,
            )
            return None

    def _fetch_trakt_episode(self, slug: str | None, show_trakt_id: int | None, season: int, episode_number: int) -> dict | None:
        """
        Calls Trakt /shows/{slug}/seasons/{s}/episodes/{e}?extended=full
        using the synchronous _get helper (public endpoint — only needs client_id).
        Falls back to using the numeric Trakt show ID if no slug is available.
        Returns the raw response dict, or None on failure / no handler.
        """
        if not self.trakt:
            return None

        # Prefer slug (human-readable, stable); fall back to numeric ID
        show_ref = slug or show_trakt_id
        if not show_ref:
            return None

        try:
            resp = self.trakt._get(
                f"/shows/{show_ref}/seasons/{season}/episodes/{episode_number}",
                extended="full",
            )
            if resp is None:
                return None
            if resp.status_code == 200:
                data = resp.json()
                runtime = data.get("runtime")
                rating  = data.get("rating")
                log(
                    f"[StaleEpisodeRefresh] Trakt S{season}E{episode_number} ({show_ref}): "
                    f"runtime={runtime}, rating={rating}, "
                    f"imdb={data.get('ids', {}).get('imdb')}, tvdb={data.get('ids', {}).get('tvdb')}.",
                    level=LOGDEBUG,
                )
                return data
            log(
                f"[StaleEpisodeRefresh] Trakt returned HTTP {resp.status_code} for "
                f"{show_ref} S{season}E{episode_number}.",
                level=LOGDEBUG,
            )
            return None
        except Exception as exc:
            log(
                f"[StaleEpisodeRefresh] Trakt fetch error {show_ref} "
                f"S{season}E{episode_number}: {exc}",
                level=LOGERROR,
            )
            return None

    def _update_episode(
        self,
        ep_tmdb_id: int,
        old: dict,
        tmdb_data: dict,
        trakt_data: dict,
        show_tmdb_id: int,
    ) -> bool:
        """
        Merges TMDB + Trakt data into the existing episode row.
        Priority: TMDB → Trakt → existing DB value.
        Only writes fields that were previously missing/default AND are now
        available from one of the sources so we never overwrite real data with nulls.

        Returns True on success.
        """
        def _pick(primary, fallback, old_val, empty_check=lambda v: not v):
            """Return the first non-empty value from primary, fallback, then old_val."""
            if not empty_check(primary):
                return primary
            if not empty_check(fallback):
                return fallback
            return old_val

        # Runtime: TMDB first, Trakt fallback (Trakt stores it as 'runtime' too)
        new_runtime  = _pick(tmdb_data.get("runtime"),      trakt_data.get("runtime"),    old.get("runtime"),      lambda v: not v)
        # Rating: TMDB vote_average, Trakt rating
        new_rating   = _pick(tmdb_data.get("vote_average"), trakt_data.get("rating"),     old.get("rating"),       lambda v: not v)
        # Votes: TMDB vote_count, Trakt votes
        new_votes    = _pick(tmdb_data.get("vote_count"),   trakt_data.get("votes"),      old.get("votes"),        lambda v: not v)
        # Overview: TMDB overview, Trakt overview
        new_overview = _pick(tmdb_data.get("overview"),     trakt_data.get("overview"),   old.get("episode_overview"), lambda v: not v or not str(v).strip())
        # Title: TMDB name, Trakt title
        new_title    = _pick(tmdb_data.get("name"),         trakt_data.get("title"),      old.get("episode_title"),    lambda v: not v or not str(v).strip())

        # External IDs: TMDB external_ids sub-object, then Trakt ids sub-object
        tmdb_ext_ids  = tmdb_data.get("external_ids") or {}
        trakt_ids     = trakt_data.get("ids") or {}
        new_imdb_id   = _pick(tmdb_ext_ids.get("imdb_id"), trakt_ids.get("imdb"), old.get("imdb_id"), lambda v: not v)
        new_tvdb_id   = _pick(tmdb_ext_ids.get("tvdb_id"), trakt_ids.get("tvdb"), old.get("tvdb_id"), lambda v: not v)

        # Episode still image (thumbnail) — TMDB only
        still_path   = tmdb_data.get("still_path")
        new_thumb    = (
            f"https://image.tmdb.org/t/p/w300{still_path}"
            if still_path
            else old.get("episode_thumbnail_path")
        )

        # If absolutely nothing changed, skip the full UPDATE but still stamp
        # the cooldown so this episode isn't re-queried for 7 days.
        unchanged = (
            new_runtime  == (old.get("runtime") or 0)
            and new_rating   == (old.get("rating")  or 0.0)
            and new_votes    == (old.get("votes")   or 0)
            and new_overview == (old.get("episode_overview") or "")
            and new_title    == (old.get("episode_title")    or "")
            and new_imdb_id  == old.get("imdb_id")
            and new_tvdb_id  == old.get("tvdb_id")
            and new_thumb    == old.get("episode_thumbnail_path")
        )
        if unchanged:
            log(
                f"[StaleEpisodeRefresh] No new data from TMDB or Trakt for "
                f"tmdb_id={ep_tmdb_id}; stamping cooldown.",
                level=LOGDEBUG,
            )
            try:
                with sqlite3.connect(self.db_path, timeout=15) as conn:
                    conn.execute(
                        "UPDATE episodes SET metadata_refreshed_at = unixepoch() WHERE tmdb_id = ?",
                        (ep_tmdb_id,),
                    )
                    conn.commit()
            except Exception as exc:
                log(f"[StaleEpisodeRefresh] Failed to stamp cooldown for tmdb_id={ep_tmdb_id}: {exc}", level=LOGERROR)
            return True  # not an error — sources just don't have it yet

        try:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                conn.execute(
                    """
                    UPDATE episodes
                    SET
                        runtime                = ?,
                        rating                 = ?,
                        votes                  = ?,
                        episode_overview       = ?,
                        episode_title          = ?,
                        original_title         = ?,
                        imdb_id                = ?,
                        tvdb_id                = ?,
                        episode_thumbnail_path = ?,
                        metadata_refreshed_at  = unixepoch()
                    WHERE tmdb_id = ?
                    """,
                    (
                        new_runtime,
                        new_rating,
                        new_votes,
                        new_overview,
                        new_title,
                        new_title,      # original_title mirrors title for episodes
                        new_imdb_id,
                        new_tvdb_id,
                        new_thumb,
                        ep_tmdb_id,
                    ),
                )
                conn.commit()

            log(
                f"[StaleEpisodeRefresh] Updated tmdb_id={ep_tmdb_id} "
                f"(show={show_tmdb_id} S{old['season']}E{old['episode_number']}) — "
                f"runtime={new_runtime}, rating={new_rating}, votes={new_votes}, "
                f"thumb={'yes' if new_thumb else 'no'}, "
                f"imdb_id={new_imdb_id}, tvdb_id={new_tvdb_id}.",
                level=LOGDEBUG,
            )
            return True

        except Exception as exc:
            log(
                f"[StaleEpisodeRefresh] DB write failed for tmdb_id={ep_tmdb_id}: {exc}",
                level=LOGERROR,
            )
            return False
