import unittest
import tempfile
import sqlite3
import os
import json
import gc
from unittest.mock import patch, MagicMock

from resources.lib.config_handler import (
    get_trakt_client_id, get_config_value, update_config_values,
    migrate_trakt_credentials, DEFAULT_TRAKT_CLIENT_ID, OLD_TRAKT_CLIENT_ID
)
from resources.lib.trakt_handler import TraktAuth


class TestTraktPKCE(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "config.db")
        self.config_json = os.path.join(self.temp_dir.name, "config.json")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE config (
                    key VARCHAR(255) PRIMARY KEY,
                    value TEXT,
                    user_id TEXT,
                    is_encrypted BOOLEAN DEFAULT false
                )
            """)

    def tearDown(self):
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_default_client_id_resolution(self):
        # Empty config returns new default PKCE client ID
        self.assertEqual(get_trakt_client_id(self.db_path), DEFAULT_TRAKT_CLIENT_ID)

        # Legacy client ID resolves to new default PKCE client ID
        update_config_values({'client_id': OLD_TRAKT_CLIENT_ID}, self.db_path)
        self.assertEqual(get_trakt_client_id(self.db_path), DEFAULT_TRAKT_CLIENT_ID)

    def test_migration_invalidates_old_tokens_and_updates_client_id(self):
        # Setup DB with old credentials and an active token
        update_config_values({
            'client_id': OLD_TRAKT_CLIENT_ID,
            'client_secret': 'old_secret_123',
            'trakt_token': 'old_access_token',
            'trakt.token': 'old_access_token',
            'trakt_refresh': 'old_refresh_token',
            'trakt.refresh': 'old_refresh_token',
            'trakt_user': 'testuser'
        }, self.db_path)

        with open(self.config_json, "w", encoding="utf-8") as f:
            json.dump({
                "TRAKT": {
                    "client_id": OLD_TRAKT_CLIENT_ID,
                    "client_secret": "old_secret_123"
                }
            }, f)

        # Run migration
        migrated = migrate_trakt_credentials(self.db_path, self.config_json)
        self.assertTrue(migrated)

        # Verify client ID updated and secret cleared
        self.assertEqual(get_config_value('client_id', self.db_path), DEFAULT_TRAKT_CLIENT_ID)
        self.assertEqual(get_config_value('client_secret', self.db_path), '')

        # Verify old tokens wiped
        self.assertEqual(get_config_value('trakt_token', self.db_path), 'empty_setting')
        self.assertEqual(get_config_value('trakt_refresh', self.db_path), 'empty_setting')

        # Verify re-auth flag set
        self.assertEqual(get_config_value('trakt_reauth_required', self.db_path), 'true')

        # Verify config.json updated
        with open(self.config_json, "r", encoding="utf-8") as f:
            cdata = json.load(f)
        self.assertEqual(cdata["TRAKT"]["client_id"], DEFAULT_TRAKT_CLIENT_ID)
        self.assertEqual(cdata["TRAKT"]["client_secret"], "")

    @patch("requests.post")
    def test_trakt_auth_refresh_omits_client_secret_when_empty(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token"
        }
        mock_post.return_value = mock_response

        update_config_values({
            'client_id': DEFAULT_TRAKT_CLIENT_ID,
            'client_secret': '',
            'trakt_refresh': 'current_refresh_token'
        }, self.db_path)

        auth = TraktAuth(addon="service.orac", config_db_path=self.db_path)
        success = auth.refresh_token()
        self.assertTrue(success)

        # Inspect POST payload
        self.assertTrue(mock_post.called)
        sent_payload = mock_post.call_args[1].get("json") or mock_post.call_args[0][1]
        self.assertEqual(sent_payload["client_id"], DEFAULT_TRAKT_CLIENT_ID)
        self.assertEqual(sent_payload["grant_type"], "refresh_token")
        self.assertNotIn("client_secret", sent_payload)


if __name__ == "__main__":
    unittest.main()
