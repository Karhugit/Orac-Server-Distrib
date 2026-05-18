# -*- coding: utf-8 -*-
"""
providers_handler.py — TMDB Watch Provider catalogue management.

Responsibilities:
  1. sync_watch_providers()  — fetches movie+TV providers from TMDB for the
     configured region and upserts them into the watch_providers table in the
     config DB.
  2. get_watch_providers()   — reads providers from the DB and returns a sorted
     list ready for use by Liberator's /providers endpoint.
  3. init_watch_providers_db() — safe schema migration, called on startup.
"""
import sqlite3
import time
from resources.lib.log_utils import log, LOGERROR, LOGDEBUG, LOGINFO


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

def init_watch_providers_db(db_path: str) -> bool:
    """
    Idempotent schema migration — adds the watch_providers table to the
    config DB if it does not already exist.
    """
    try:
        with sqlite3.connect(db_path, timeout=15) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watch_providers (
                    provider_id   INTEGER PRIMARY KEY,
                    name          TEXT    NOT NULL,
                    logo_path     TEXT,
                    for_movie     INTEGER DEFAULT 0,
                    for_tv        INTEGER DEFAULT 0,
                    display_order INTEGER DEFAULT 9999,
                    last_synced   INTEGER DEFAULT 0
                )
            """)
            conn.commit()
        log("[Providers] watch_providers table ready.", level=LOGDEBUG)
        return True
    except Exception as exc:
        log(f"[Providers] Schema migration failed: {exc}", level=LOGERROR)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Sync from TMDB
# ─────────────────────────────────────────────────────────────────────────────

def sync_watch_providers(tmdb_handler, config_db_path: str, region: str | None = None) -> bool:
    """
    Fetches the watch-provider catalogue from TMDB (globally if region is None,
    or scoped to a specific ISO-3166-1 region) and upserts every provider into
    the watch_providers table.

    Called once on Orac startup (and daily thereafter via the sync loop).
    Uses the existing tmdb_handler so no extra API-key plumbing is required.
    """
    if not tmdb_handler:
        log("[Providers] No TMDB handler — skipping provider sync.", level=LOGDEBUG)
        return False

    now = int(time.time())
    counts = {"movie": 0, "tv": 0}

    try:
        rows: dict[int, dict] = {}

        for media_type in ("movie", "tv"):
            data = tmdb_handler.get_watch_providers(media_type, region=region)
            if not data:
                log(f"[Providers] TMDB returned no providers for {media_type}.", level=LOGDEBUG)
                continue

            results = data.get("results", [])
            for p in results:
                pid = p.get("provider_id")
                if not pid:
                    continue
                # Use regional display priority if a region was specified; else 9999
                if region:
                    order = p.get("display_priorities", {}).get(region, 9999)
                else:
                    order = 9999

                if pid not in rows:
                    rows[pid] = {
                        "provider_id":   pid,
                        "name":          p.get("provider_name", ""),
                        "logo_path":     p.get("logo_path", ""),
                        "for_movie":     0,
                        "for_tv":        0,
                        "display_order": order,
                        "last_synced":   now,
                    }
                rows[pid][f"for_{media_type}"] = 1
                # Keep the lowest (most prominent) display order
                if order < rows[pid]["display_order"]:
                    rows[pid]["display_order"] = order

            counts[media_type] = len(results)

        if not rows:
            log("[Providers] No provider data returned from TMDB — aborting upsert.", level=LOGDEBUG)
            return False

        with sqlite3.connect(config_db_path, timeout=15) as conn:
            conn.executemany(
                """
                INSERT INTO watch_providers
                    (provider_id, name, logo_path, for_movie, for_tv, display_order, last_synced)
                VALUES
                    (:provider_id, :name, :logo_path, :for_movie, :for_tv, :display_order, :last_synced)
                ON CONFLICT(provider_id) DO UPDATE SET
                    name          = excluded.name,
                    logo_path     = excluded.logo_path,
                    for_movie     = excluded.for_movie,
                    for_tv        = excluded.for_tv,
                    display_order = excluded.display_order,
                    last_synced   = excluded.last_synced
                """,
                list(rows.values()),
            )
            conn.commit()

        region_label = region if region else "global"
        log(
            f"[Providers] Synced {counts['movie']} movie + {counts['tv']} TV providers "
            f"({len(rows)} unique) region={region_label}.",
            level=LOGINFO,
        )
        return True

    except Exception as exc:
        log(f"[Providers] sync_watch_providers failed: {exc}", level=LOGERROR)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Read from DB
# ─────────────────────────────────────────────────────────────────────────────

def get_watch_providers(config_db_path: str, media_type: str | None = None) -> list[dict]:
    """
    Returns providers sorted by display_order then name.

    media_type  — 'movie', 'tv', or None / 'all' for both.
    Each item:  {id, name, logo_path, for_movie, for_tv}
    """
    try:
        with sqlite3.connect(config_db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if media_type in ("movie",):
                cursor.execute(
                    "SELECT * FROM watch_providers WHERE for_movie = 1 "
                    "ORDER BY display_order, name",
                )
            elif media_type in ("tv", "tvshow"):
                cursor.execute(
                    "SELECT * FROM watch_providers WHERE for_tv = 1 "
                    "ORDER BY display_order, name",
                )
            else:
                cursor.execute(
                    "SELECT * FROM watch_providers ORDER BY display_order, name"
                )

            rows = cursor.fetchall()
            return [
                {
                    "id":        r["provider_id"],
                    "name":      r["name"],
                    "logo_path": r["logo_path"] or "",
                    "for_movie": bool(r["for_movie"]),
                    "for_tv":    bool(r["for_tv"]),
                }
                for r in rows
            ]

    except Exception as exc:
        log(f"[Providers] get_watch_providers failed: {exc}", level=LOGERROR)
        return []
