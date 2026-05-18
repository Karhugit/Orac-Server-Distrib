import sqlite3
import threading
from contextlib import contextmanager
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGDEBUG

class DatabaseManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(DatabaseManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.db_paths = {}
        # Thread-local storage for connections if we wanted to enforce one-per-thread,
        # but for now we will just create fresh connections with the correct settings
        # or implement a simple pool later. 
        # For this step, we mainly want to centralize the connection creation logic.

    def configure(self, db_paths):
        """
        Configure the manager with a dictionary of database paths.
        db_paths: dict of { 'db_name': 'path/to/db.sqlite' }
        """
        self.db_paths = db_paths

    def get_path(self, db_name):
        return self.db_paths.get(db_name)

    def get_connection(self, db_name_or_path):
        """
        Returns a configured sqlite3 connection.
        accepts either a known db_name (e.g., 'movies_static') or a direct path.
        """
        path = self.db_paths.get(db_name_or_path, db_name_or_path)
        
        if not path:
            raise ValueError(f"Database path not found for: {db_name_or_path}")

        try:
            conn = sqlite3.connect(path, timeout=30.0) # Increased timeout
            
            # Application-wide optimizations
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000") # 30s busy timeout
            
            return conn
        except sqlite3.Error as e:
            log(f"[DatabaseManager] Failed to connect to {path}: {e}", level=LOGERROR)
            raise

    @contextmanager
    def connection(self, db_name_or_path):
        """
        Context manager for a database connection.
        Automatically commits on success, rolls back on exception, and closes.
        """
        conn = self.get_connection(db_name_or_path)
        try:
            yield conn
            conn.commit()
        except:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def cursor(self, db_name_or_path):
        """
        Context manager for a database cursor.
        Automatically closes connection and cursor.
        """
        conn = self.get_connection(db_name_or_path)
        try:
            cursor = conn.cursor()
            yield cursor
        finally:
            conn.close()

    def execute_scalar(self, db_name, query, params=()):
        with self.cursor(db_name) as cursor:
            cursor.execute(query, params)
            result = cursor.fetchone()
            return result[0] if result else None

    def execute_fetchall(self, db_name, query, params=()):
        with self.cursor(db_name) as cursor:
            cursor.execute(query, params)
            return cursor.fetchall()
