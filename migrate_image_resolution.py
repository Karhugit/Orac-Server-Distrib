"""
migrate_image_resolution.py
---------------------------
One-off migration: upgrades all stored TMDb image URLs in the Orac databases
to higher-resolution equivalents, using SQL REPLACE() — no API calls needed.

Size mapping applied:
  Posters (poster_path):           /w500/ -> /w780/
  Fanart/backdrop (fanart_path):   /w780/ -> /w1280/
  Landscape (landscape_path):      /w780/ -> /w1280/
  Thumbnails (thumbnail_path):     /w300/ -> /w780/  and  /w185/ -> /w780/
  Clear logos (clearlogo_path):    /w300/ -> /w500/
  Episode stills (episode_thumbnail_path): /w300/ -> /w780/
  Episode fanart/landscape:        /w780/ -> /w1280/
  Episode poster:                  /w500/ -> /w780/
"""

import os
import sys
import sqlite3

# ── locate databases via config ───────────────────────────────────────────────
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)

from resources.lib.config_loader import ConfigLoader
config_loader = ConfigLoader()
db_paths = config_loader.db_paths

MOVIES_STATIC  = db_paths["movies_static"]
TV_STATIC      = db_paths["tvshows_static"]

# ── helpers ───────────────────────────────────────────────────────────────────

def replace_col(cur, table, column, old_size, new_size):
    """Replace /old_size/ with /new_size/ in a single column, returns row count."""
    cur.execute(f"""
        UPDATE {table}
        SET {column} = REPLACE({column}, '/{old_size}/', '/{new_size}/')
        WHERE {column} LIKE '%/{old_size}/%'
    """)
    return cur.rowcount


def migrate_db(db_path, label, migrations):
    """
    migrations: list of (table, column, old_size, new_size)
    """
    print(f"\n{'='*60}")
    print(f"Database: {label}")
    print(f"Path:     {db_path}")
    print(f"{'='*60}")

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        total = 0
        for table, column, old_size, new_size in migrations:
            count = replace_col(cur, table, column, old_size, new_size)
            if count:
                print(f"  [OK] {table}.{column}: /{old_size}/ -> /{new_size}/  ({count} rows updated)")
            else:
                print(f"  [--] {table}.{column}: /{old_size}/ -> /{new_size}/  (no rows matched)")
            total += count
        conn.commit()

    print(f"\n  Total rows updated: {total}")
    return total


# ── migration definitions ─────────────────────────────────────────────────────

MOVIES_MIGRATIONS = [
    # (table, column, old_size, new_size)
    ("movies", "poster_path",    "w500",  "w780"),
    ("movies", "fanart_path",    "w780",  "w1280"),
    ("movies", "landscape_path", "w780",  "w1280"),
    ("movies", "thumbnail_path", "w300",  "w780"),
    ("movies", "thumbnail_path", "w185",  "w780"),  # poster fallback
    ("movies", "clearlogo_path", "w300",  "w500"),
]

TV_MIGRATIONS = [
    # Shows
    ("shows", "poster_path",    "w500",  "w780"),
    ("shows", "fanart_path",    "w780",  "w1280"),
    ("shows", "landscape_path", "w780",  "w1280"),
    ("shows", "thumbnail_path", "w300",  "w780"),
    ("shows", "clearlogo_path", "w300",  "w500"),

    # Seasons
    ("seasons", "poster_path",    "w500",  "w780"),
    ("seasons", "poster_path",    "w300",  "w780"),  # old thumb size used for seasons
    ("seasons", "fanart_path",    "w780",  "w1280"),
    ("seasons", "landscape_path", "w780",  "w1280"),
    ("seasons", "thumbnail_path", "w300",  "w780"),

    # Episodes
    ("episodes", "episode_poster_path",    "w500",  "w780"),
    ("episodes", "episode_fanart_path",    "w780",  "w1280"),
    ("episodes", "episode_landscape_path", "w780",  "w1280"),
    ("episodes", "episode_thumbnail_path", "w300",  "w780"),
]

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("Orac Image Resolution Migration")
    print("Upgrading stored TMDb image URLs to higher resolution...")

    grand_total = 0
    grand_total += migrate_db(MOVIES_STATIC, "Movies Static Cache", MOVIES_MIGRATIONS)
    grand_total += migrate_db(TV_STATIC,     "TV Shows Static Cache", TV_MIGRATIONS)

    print(f"\n{'='*60}")
    print(f"Migration complete. Grand total rows updated: {grand_total}")
    print(f"{'='*60}")
    print("\nImages will appear at higher resolution on next Kodi refresh.")
    print("No API calls were made — all changes are local string replacements.")


if __name__ == "__main__":
    main()
