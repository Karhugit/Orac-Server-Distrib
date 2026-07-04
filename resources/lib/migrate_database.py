import sqlite3
from resources.lib.log_utils import log, LOGINFO, LOGERROR
from resources.lib.watched import _update_show_watched_status

def migration_1_recalculate_specials(static_db_path, dynamic_db_path):
    """
    Migration v1: Recalculates the watched_status for all shows in user_show_sync,
    excluding Specials (Season 0).
    """
    with sqlite3.connect(dynamic_db_path) as dynamic_conn:
        cursor = dynamic_conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_show_sync'")
        if not cursor.fetchone():
            log("[Orac] Table 'user_show_sync' not found. Skipping v1 migration.", level=LOGINFO)
            return

    log("[Orac] Running migration v1: Recalculating TV show watched status (excluding Specials)...", level=LOGINFO)
    with sqlite3.connect(dynamic_db_path) as dynamic_conn, \
         sqlite3.connect(static_db_path) as static_conn:
        dynamic_cursor = dynamic_conn.cursor()
        static_cursor = static_conn.cursor()
        
        dynamic_cursor.execute("SELECT DISTINCT user, show_tmdb_id FROM user_show_sync")
        rows = dynamic_cursor.fetchall()
        
        for user, show_tmdb_id in rows:
            _update_show_watched_status(dynamic_cursor, static_cursor, user, show_tmdb_id)
            
        dynamic_conn.commit()
    log("[Orac] Migration v1 completed successfully.", level=LOGINFO)


def migration_2_refactor_fanart_columns(static_db_path, dynamic_db_path):
    """
    Migration v2: Unifies artwork columns and drops deprecated fanart_ columns.
    If fanart_enabled is True, it copies the fanart paths to the standard columns.
    """
    log(f"[Orac] Running migration v2: Unifying artwork columns in {static_db_path}...", level=LOGINFO)
    
    # 1. Determine media type and table name
    table = None
    with sqlite3.connect(static_db_path) as static_conn:
        cursor = static_conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='movies'")
        if cursor.fetchone():
            table = "movies"
        else:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='shows'")
            if cursor.fetchone():
                table = "shows"
                
    if not table:
        log(f"[Orac] No movies or shows table found in {static_db_path}. Skipping v2 migration.", level=LOGINFO)
        return

    # 2. Check if fanart is enabled in settings
    from resources.lib.config_handler import get_fanart_config
    config = get_fanart_config()
    fanart_enabled = config.get("fanart_enabled", False)

    with sqlite3.connect(static_db_path) as static_conn:
        cursor = static_conn.cursor()
        
        # 3. If fanart_enabled is True, migrate the values
        if fanart_enabled:
            log(f"[Orac] Fanart is enabled. Backfilling fanart paths to standard artwork columns in table '{table}'...", level=LOGINFO)
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [col[1] for col in cursor.fetchall()]
            
            if "fanart_poster_path" in columns and "fanart_fanart_path" in columns and "fanart_clearlogo_path" in columns:
                cursor.execute(f"""
                    UPDATE {table} SET
                        poster_path = COALESCE(fanart_poster_path, poster_path),
                        thumbnail_path = COALESCE(fanart_poster_path, thumbnail_path),
                        fanart_path = COALESCE(fanart_fanart_path, fanart_path),
                        landscape_path = COALESCE(fanart_fanart_path, landscape_path),
                        clearlogo_path = COALESCE(fanart_clearlogo_path, clearlogo_path)
                """)
                static_conn.commit()
                log(f"[Orac] Backfill completed for '{table}' table.", level=LOGINFO)
            else:
                log(f"[Orac] Old fanart columns not present in '{table}' table. Skipping backfill.", level=LOGINFO)

        # 4. Drop the deprecated columns
        log(f"[Orac] Dropping deprecated fanart columns from '{table}' table...", level=LOGINFO)
        for col in ["fanart_poster_path", "fanart_fanart_path", "fanart_clearlogo_path"]:
            try:
                cursor.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
            except sqlite3.OperationalError:
                # Column already dropped or SQLite doesn't support DROP COLUMN
                pass
        static_conn.commit()
        log(f"[Orac] Finished dropping deprecated columns from '{table}' table.", level=LOGINFO)


# Map version numbers to migration functions.
# New migrations should be appended here with incremented version numbers (2, 3, etc.)
MIGRATIONS = {
    1: migration_1_recalculate_specials,
    2: migration_2_refactor_fanart_columns,
}

TARGET_VERSION = max(MIGRATIONS.keys()) if MIGRATIONS else 0


def migrate_database(static_db_path, dynamic_db_path):
    """
    Checks the current database version in sync_metadata and runs pending migrations sequentially.
    """
    try:
        current_version = 0
        with sqlite3.connect(dynamic_db_path) as dynamic_conn:
            cursor = dynamic_conn.cursor()
            cursor.execute("CREATE TABLE IF NOT EXISTS sync_metadata (key TEXT PRIMARY KEY, value TEXT)")
            cursor.execute("SELECT value FROM sync_metadata WHERE key = 'db_version'")
            row = cursor.fetchone()
            if row:
                try:
                    current_version = int(row[0])
                except ValueError:
                    current_version = 0

        if current_version >= TARGET_VERSION:
            return

        log(f"[Orac] Database migration started. Current version: {current_version}, Target version: {TARGET_VERSION}", level=LOGINFO)

        for version in sorted(MIGRATIONS.keys()):
            if version > current_version:
                migration_func = MIGRATIONS[version]
                migration_func(static_db_path, dynamic_db_path)
                
                # Update version in sync_metadata after successful migration step
                with sqlite3.connect(dynamic_db_path) as dynamic_conn:
                    cursor = dynamic_conn.cursor()
                    cursor.execute("INSERT OR REPLACE INTO sync_metadata (key, value) VALUES ('db_version', ?)", (str(version),))
                    dynamic_conn.commit()
                
                current_version = version
                log(f"[Orac] Database successfully migrated to version {version}.", level=LOGINFO)

        log("[Orac] All database migrations completed successfully.", level=LOGINFO)
    except Exception as e:
        log(f"[Orac] Error during database migration: {e}", level=LOGERROR)
        raise
