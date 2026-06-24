import sqlite3
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

def _init_config_db(conn):
    """Ensures the config table exists."""
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key VARCHAR(255) PRIMARY KEY,
                value TEXT,
                user_id TEXT,
                is_encrypted BOOLEAN DEFAULT false
            )
        """)
        conn.commit()
    except Exception as e:
        log(f"[Orac] Failed to initialize config database table: {e}", level=LOGERROR)


def get_config_value(key, config_db_path, default=None):
    """Fetches a single value from the config database by key."""
    try:
        with sqlite3.connect(config_db_path) as conn:
            # Ensure the table exists before trying to read from it
            _init_config_db(conn)
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else default
    except Exception as e:
        log(f"Error fetching '{key}' from config DB: {e}", LOGERROR)
    return default


def get_trakt_user(config_db_path):
    return get_config_value('trakt_user', config_db_path)


def get_trakt_access_token(config_db_path):
    return get_config_value('trakt_token', config_db_path)


def get_trakt_refresh_token(config_db_path):
    return get_config_value('trakt_refresh', config_db_path)


def get_trakt_client_id(config_db_path):
    return get_config_value('client_id', config_db_path)

def get_trakt_client_secret(config_db_path):
    return get_config_value('client_secret', config_db_path)


def get_tmdb_user(config_db_path):
    return get_config_value('tmdb_user', config_db_path)


def get_tmdb_session_id(config_db_path):
    return get_config_value('tmdb_session_id', config_db_path)



def clear_trakt_config(config_db_path):
    """Deletes Trakt authentication records from the config database."""
    keys_to_delete = [
        'trakt_user',
        'trakt_token',
        'trakt_refresh',
        'trakt_refresh_token',
        'trakt_expires',
        'access_token',
        'refresh_token',
        'expires_in',
        'created_at'
    ]
    try:
        with sqlite3.connect(config_db_path) as conn:
            cursor = conn.cursor()
            placeholders = ','.join('?' for _ in keys_to_delete)
            cursor.execute(f"DELETE FROM config WHERE key IN ({placeholders})", keys_to_delete)
            conn.commit()
            log(f"Cleared Trakt config keys: {keys_to_delete}", LOGINFO)
    except Exception as e:
        log(f"Error clearing Trakt config from DB: {e}", LOGERROR)

def update_config_values(params, config_db_path):
    try:
        with sqlite3.connect(config_db_path) as conn:
            # Ensure the table exists before trying to write to it
            _init_config_db(conn)
            cursor = conn.cursor()

            # Gracefully fetch user_id
            user_id = params.get('trakt_user')
            if not user_id:
                row = cursor.execute("SELECT value FROM config WHERE key = 'user_id'").fetchone()
                user_id = row[0] if row else None

            # Update all provided parameters
            for key, value in params.items():
                # Use INSERT OR REPLACE to handle both new and existing keys
                cursor.execute("INSERT OR REPLACE INTO config (key, value, user_id) VALUES (?, ?, ?)", (key, value, user_id))
            conn.commit()
            return True
    except Exception as e:
        log(f"Error updating config values: {e}", LOGERROR)
        return False


def get_fanart_config(config_db_path=None):
    if config_db_path is None:
        from resources.lib.database_manager import DatabaseManager
        config_db_path = DatabaseManager().get_path('config')
    if not config_db_path:
        from pathlib import Path
        config_db_path = str(Path(__file__).resolve().parent.parent.parent / "config.db")
    
    enabled = get_config_value('fanart_enabled', config_db_path, 'false') == 'true'
    api_key = get_config_value('fanart_api_key', config_db_path, '')
    storage_mode = get_config_value('fanart_storage_mode', config_db_path, 'URL')
    
    return {
        'fanart_enabled': enabled,
        'fanart_api_key': api_key,
        'fanart_storage_mode': storage_mode
    }