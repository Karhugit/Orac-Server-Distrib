import os
import json
import sqlite3
from resources.lib.db_utils import db_connect
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

DEFAULT_TRAKT_CLIENT_ID = "EfwA4IGG5EG2CsbH-uKDWCKAkRZ2HNxtPpbC1ExQZGM"
OLD_TRAKT_CLIENT_ID = "f986871799a140dc20a166adfa637c98c8fa474dc80757aabad8668b99e184de"
DEFAULT_TRAKT_REDIRECT_URI = "http://localhost:5555/api/web/platforms/trakt/callback"

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
        with db_connect(config_db_path) as conn:
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
    cid = get_config_value('client_id', config_db_path)
    if not cid or cid in ('empty_setting', '', OLD_TRAKT_CLIENT_ID):
        return DEFAULT_TRAKT_CLIENT_ID
    return cid

def migrate_trakt_credentials(config_db_path, config_file_path=None):
    """
    Migrates legacy Trakt credentials to the new PKCE client ID and clears
    invalidated tokens, flagging that re-authorization is required.
    """
    if not config_db_path:
        return False
    current_id = get_config_value('client_id', config_db_path)
    migrated = False
    if not current_id or current_id in ('empty_setting', '', OLD_TRAKT_CLIENT_ID):
        log(f"[Trakt] Migrating Trakt credentials to new PKCE app (Client ID: {DEFAULT_TRAKT_CLIENT_ID[:6]}...)", level=LOGINFO)
        existing_token = get_config_value('trakt_token', config_db_path)
        had_auth = bool(existing_token and existing_token not in ('empty_setting', ''))

        updates = {
            'client_id': DEFAULT_TRAKT_CLIENT_ID,
            'client_secret': '',
            'trakt_client': DEFAULT_TRAKT_CLIENT_ID,
            'trakt.client': DEFAULT_TRAKT_CLIENT_ID,
        }
        if had_auth:
            updates.update({
                'trakt_token': 'empty_setting',
                'trakt.token': 'empty_setting',
                'trakt_refresh': 'empty_setting',
                'trakt.refresh': 'empty_setting',
                'trakt_expires': 'empty_setting',
                'trakt_reauth_required': 'true'
            })
            log("[Trakt] Invalidation of old Trakt OAuth token complete. User re-auth required.", level=LOGINFO)

        update_config_values(updates, config_db_path)
        migrated = True

    if config_file_path and os.path.exists(config_file_path):
        try:
            with open(config_file_path, 'r', encoding='utf-8') as cf:
                cdata = json.load(cf)
            trakt_cfg = cdata.get('TRAKT', {})
            if trakt_cfg.get('client_id') == OLD_TRAKT_CLIENT_ID or not trakt_cfg.get('client_id'):
                trakt_cfg['client_id'] = DEFAULT_TRAKT_CLIENT_ID
                trakt_cfg['client_secret'] = ''
                cdata['TRAKT'] = trakt_cfg
                with open(config_file_path, 'w', encoding='utf-8') as cf:
                    json.dump(cdata, cf, indent=4)
                log(f"[Trakt] Updated {config_file_path} with new Client ID", level=LOGINFO)
        except Exception as ce:
            log(f"[Trakt] Warning: Failed to update {config_file_path}: {ce}", level=LOGWARNING)

    return migrated

def get_trakt_client_secret(config_db_path):
    return get_config_value('client_secret', config_db_path)


def get_tmdb_user(config_db_path):
    return get_config_value('tmdb_user', config_db_path)


def get_tmdb_session_id(config_db_path):
    return get_config_value('tmdb_session_id', config_db_path)


def get_authorized_watched_providers(config_db_path):
    """
    Returns a list of authorized provider names that support watched items tracking.
    Supported watched-tracking providers: 'trakt', 'simkl', 'mdblist'.
    Note: TMDB does not support watched items tracking.
    """
    providers = []
    if not config_db_path:
        return providers

    # Check Trakt
    trakt_token = get_trakt_access_token(config_db_path)
    trakt_client = get_trakt_client_id(config_db_path)
    if trakt_token and trakt_client and trakt_token != "empty_setting" and trakt_client != "empty_setting":
        providers.append('trakt')

    # Check Simkl
    simkl_token = get_config_value("simkl.token", config_db_path)
    from resources.lib.simkl_api import get_effective_simkl_client_id
    simkl_client = get_effective_simkl_client_id(config_db_path)
    if simkl_token and simkl_client and simkl_token != "empty_setting" and simkl_client != "empty_setting":
        providers.append('simkl')

    # Check MDBList
    mdblist_api = get_config_value("mdblist_api", config_db_path)
    if mdblist_api and mdblist_api != "empty_setting":
        providers.append('mdblist')

    # Check PunchPlay
    punchplay_token = get_config_value("punchplay.token", config_db_path)
    if punchplay_token and punchplay_token != "empty_setting":
        providers.append('punchplay')

    return providers




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
        with db_connect(config_db_path) as conn:
            cursor = conn.cursor()
            placeholders = ','.join('?' for _ in keys_to_delete)
            cursor.execute(f"DELETE FROM config WHERE key IN ({placeholders})", keys_to_delete)
            conn.commit()
            log(f"Cleared Trakt config keys: {keys_to_delete}", LOGINFO)
    except Exception as e:
        log(f"Error clearing Trakt config from DB: {e}", LOGERROR)

def update_config_values(params, config_db_path):
    try:
        with db_connect(config_db_path) as conn:
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


def get_all_config(config_db_path):
    """Fetches all key-value pairs from the config database as a dict."""
    try:
        with db_connect(config_db_path) as conn:
            _init_config_db(conn)
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM config")
            return {row[0]: row[1] for row in cursor.fetchall()}
    except Exception as e:
        log(f"Error fetching all config: {e}", LOGERROR)
        return {}
