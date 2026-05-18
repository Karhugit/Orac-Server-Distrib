# -*- coding: utf-8 -*-
import sqlite3
import os
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGDEBUG

class ScraperDB:
    def __init__(self, db_path='scrapers.db'):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        """Initializes the scrapers table with total_scrapes support."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # Create table with REAL and total_scrapes
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS scrapers (
                        name TEXT PRIMARY KEY,
                        active INTEGER DEFAULT 1,
                        score REAL DEFAULT 0,
                        total_scrapes INTEGER DEFAULT 0
                    )
                """)
                # Handle migration for existing DBs that don't have total_scrapes
                cursor.execute("PRAGMA table_info(scrapers)")
                cols = [c[1] for c in cursor.fetchall()]
                if 'total_scrapes' not in cols:
                    cursor.execute("ALTER TABLE scrapers ADD COLUMN total_scrapes INTEGER DEFAULT 0")
                
                conn.commit()
        except Exception as e:
            log(f"[ScraperDB] Initialization failed: {e}", level=LOGERROR)

    def register_scrapers(self, scraper_names):
        """Ensures all provided scrapers are in the database."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                for name in scraper_names:
                    cursor.execute("INSERT OR IGNORE INTO scrapers (name, active, score, total_scrapes) VALUES (?, 1, 0, 0)", (name,))
                conn.commit()
        except Exception as e:
            log(f"[ScraperDB] Failed to register scrapers: {e}", level=LOGERROR)

    def get_active_scrapers(self):
        """Returns a list of dictionaries for all active scrapers."""
        try:
            with self._get_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT name, score, total_scrapes, active FROM scrapers WHERE active = 1 ORDER BY score DESC")
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            log(f"[ScraperDB] Failed to get active scrapers: {e}", level=LOGERROR)
            return []

    def get_all_scrapers(self):
        """Returns a list of dictionaries for ALL scrapers (active and inactive)."""
        try:
            with self._get_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT name, score, total_scrapes, active FROM scrapers ORDER BY score DESC")
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            log(f"[ScraperDB] Failed to get all scrapers: {e}", level=LOGERROR)
            return []

    def set_active_status(self, name, status):
        """Sets the active status (0 or 1) for a specific scraper. Resets stats on reactivation."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # Check current status to see if we are reactivating
                cursor.execute("SELECT active FROM scrapers WHERE name = ?", (name,))
                row = cursor.fetchone()
                current_status = row[0] if row else 1
                
                new_status = 1 if status else 0
                if current_status == 0 and new_status == 1:
                    log(f"[ScraperDB] {name}: Reactivating. Resetting score and total_scrapes.", level=LOGINFO)
                    cursor.execute("UPDATE scrapers SET active = 1, score = 0, total_scrapes = 0 WHERE name = ?", (name,))
                else:
                    cursor.execute("UPDATE scrapers SET active = ? WHERE name = ?", (new_status, name))
                
                conn.commit()
                log(f"[ScraperDB] {name}: Active status set to {status}", level=LOGINFO)
        except Exception as e:
            log(f"[ScraperDB] Failed to set active status for {name}: {e}", level=LOGERROR)

    def update_score(self, name, session_score):
        """
        Updates the moving average score of a specific scraper.
        Formula: new_avg = (current_avg * total_scrapes + session_score) / (total_scrapes + 1)
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT score, total_scrapes FROM scrapers WHERE name = ?", (name,))
                row = cursor.fetchone()
                if row:
                    current_avg, total_scrapes = row
                    new_total = total_scrapes + 1
                    new_avg = ((current_avg * total_scrapes) + session_score) / new_total
                    
                    cursor.execute("UPDATE scrapers SET score = ?, total_scrapes = ? WHERE name = ?", 
                                 (new_avg, new_total, name))
                    conn.commit()
                    log(f"[ScraperDB] {name}: New Avg {new_avg:.2f} (Total {new_total})", level=LOGDEBUG)
        except Exception as e:
            log(f"[ScraperDB] Failed to update score for {name}: {e}", level=LOGERROR)

    def increment_scrape_count(self, name):
        """Increments total_scrapes without changing the score."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE scrapers SET total_scrapes = total_scrapes + 1 WHERE name = ?", (name,))
                conn.commit()
                log(f"[ScraperDB] {name}: Incremented total_scrapes", level=LOGDEBUG)
        except Exception as e:
            log(f"[ScraperDB] Failed to increment count for {name}: {e}", level=LOGERROR)

    def reset_all_scores(self):
        """Resets all scraper scores and scrape counts to zero."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE scrapers SET score = 0, total_scrapes = 0")
                conn.commit()
        except Exception as e:
            log(f"[ScraperDB] Failed to reset scores: {e}", level=LOGERROR)
