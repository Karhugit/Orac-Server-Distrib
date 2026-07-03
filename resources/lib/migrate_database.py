import sqlite3
from resources.lib.log_utils import log, LOGINFO, LOGERROR
from resources.lib.watched import _update_show_watched_status

def migration_1_recalculate_specials(static_db_path, dynamic_db_path):
    """
    Migration v1: Recalculates the watched_status for all shows in user_show_sync,
    excluding Specials (Season 0).
    """
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


# Map version numbers to migration functions.
# New migrations should be appended here with incremented version numbers (2, 3, etc.)
MIGRATIONS = {
    1: migration_1_recalculate_specials,
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
