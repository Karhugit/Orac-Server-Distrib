# -*- coding: utf-8 -*-
import unittest
import sqlite3
import tempfile
import os
import gc
from unittest.mock import patch, MagicMock

from resources.lib.simkl_api import (
    get_simkl_user_info,
    fetch_simkl_user_custom_lists,
    fetch_simkl_custom_list_items,
    DEFAULT_SIMKL_CLIENT_ID,
)
from resources.lib.trakt_list_sync import update_list_in_db


class TestSimklCustomLists(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = os.path.join(self.temp_dir.name, "config.db")
        self.lists_db = os.path.join(self.temp_dir.name, "lists.db")

        # Setup minimal config db with full columns
        with sqlite3.connect(self.config_db) as conn:
            conn.execute("""
                CREATE TABLE config (
                    key VARCHAR(255) PRIMARY KEY,
                    value TEXT,
                    user_id TEXT,
                    is_encrypted BOOLEAN DEFAULT false
                )
            """)
            conn.execute("INSERT INTO config (key, value) VALUES ('simkl.token', 'simkl_at_test_token')")
            conn.execute(f"INSERT INTO config (key, value) VALUES ('simkl.client', '{DEFAULT_SIMKL_CLIENT_ID}')")
            conn.execute("INSERT INTO config (key, value) VALUES ('simkl.user', 'Simon Miller')")

        # Setup minimal lists db
        with sqlite3.connect(self.lists_db) as conn:
            conn.execute("""
                CREATE TABLE lists (
                    list_id TEXT PRIMARY KEY,
                    source TEXT,
                    user TEXT,
                    owned_by_user INTEGER,
                    slug TEXT,
                    name TEXT,
                    description TEXT,
                    last_checked TEXT,
                    item_count_movies INTEGER DEFAULT 0,
                    item_count_shows INTEGER DEFAULT 0,
                    add_to_library INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE list_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_id TEXT,
                    media_type TEXT,
                    trakt_id TEXT,
                    tmdb_id TEXT,
                    UNIQUE(list_id, trakt_id)
                )
            """)

    def tearDown(self):
        # Force garbage collection to close any SQLite handles on Windows
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    @patch("resources.lib.simkl_api.simkl_get")
    def test_get_simkl_user_info_numeric_account_id(self, mock_get):
        # Simkl API returns user name in user, and numeric ID in account
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "user": {"name": "Simon Miller"},
            "account": {"id": 5326556, "type": "pro"}
        }
        mock_get.return_value = mock_resp

        info = get_simkl_user_info(self.config_db)
        self.assertEqual(info["username"], "Simon Miller")
        self.assertEqual(info["user_id"], "5326556")

        # Verify stored in DB
        with sqlite3.connect(self.config_db) as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM config WHERE key='simkl.user_id'")
            row = c.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "5326556")

    @patch("resources.lib.simkl_api.simkl_get")
    def test_fetch_custom_lists_with_dict_description(self, mock_get):
        # Mock settings response then list response
        mock_resp_settings = MagicMock()
        mock_resp_settings.status_code = 200
        mock_resp_settings.json.return_value = {
            "user": {"name": "Simon Miller"},
            "account": {"id": 5326556, "type": "pro"}
        }

        mock_resp_lists = MagicMock()
        mock_resp_lists.status_code = 200
        mock_resp_lists.text = '{"lists": []}'
        mock_resp_lists.json.return_value = {
            "pagination": {"page": 1, "limit": 50, "total_items": 1, "total_pages": 1},
            "lists": [
                {
                    "id": 232374,
                    "name": "Simkl tv test list",
                    "slug": "simkl-tv-test-list",
                    "description": {"short": None, "full": "A great list"},
                    "media_type": "tv"
                }
            ]
        }
        mock_get.side_effect = [mock_resp_settings, mock_resp_lists]

        res = fetch_simkl_user_custom_lists(self.config_db)
        self.assertEqual(len(res["lists"]), 1)
        self.assertEqual(res["lists"][0]["name"], "Simkl tv test list")

    def test_update_list_in_db_handles_dict_description(self):
        # Ensure update_list_in_db does not crash with InterfaceError/ProgrammingError
        list_meta = {
            "list_id": "simkl:custom:232374",
            "source": "simkl",
            "name": "Simkl tv test list",
            "description": {"short": None, "full": "Detailed test list"},
            "ids": {"slug": "simkl-tv-test-list"},
            "user": {"ids": {"slug": "Simon Miller"}},
            "owned_by_user": True,
            "item_count": {"movies": 0, "shows": 1}
        }
        items = [
            {
                "type": "show",
                "show": {
                    "title": "Dark Matter",
                    "ids": {"tmdb": 196322, "trakt": -196322}
                }
            }
        ]

        result = update_list_in_db(self.lists_db, list_meta, items)
        self.assertEqual(result["list_id"], "simkl:custom:232374")
        self.assertEqual(result["added_items"], 1)

        with sqlite3.connect(self.lists_db) as conn:
            c = conn.cursor()
            c.execute("SELECT name, description FROM lists WHERE list_id='simkl:custom:232374'")
            row = c.fetchone()
            self.assertEqual(row[0], "Simkl tv test list")
            self.assertEqual(row[1], "Detailed test list")


if __name__ == "__main__":
    unittest.main()
