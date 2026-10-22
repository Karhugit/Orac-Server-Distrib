import sqlite3
from resources.lib.log_utils import log, LOGERROR, LOGINFO
from resources.lib.database_manager import DatabaseManager


def init_update_queue(db_path=None, conn=None):
    close_conn = False
    if conn is None:
        # Use DatabaseManager to get a configured connection
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS update_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                priority INTEGER DEFAULT 0,  -- Lower number means higher priority
                trakt_id TEXT NOT NULL,
                media_type TEXT NOT NULL,  -- 'movie', 'show', 'episodes'
                update_type TEXT NOT NULL,  -- 'loadshow', 'loadseasons', 'loadepisodes', 'loadmovie', 'loadmetadata', etc.
                payload TEXT,  -- command and data
                status TEXT DEFAULT 'pending',  -- 'pending', 'processing', 'done', 'failed', 'retry'
                attempts INTEGER DEFAULT 0,  -- Number of attempts made,
                last_response_code INTEGER,  -- HTTP response code from last attempt
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now')),
                provider TEXT DEFAULT 'trakt' -- 'trakt' or 'tmdb'
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_update_queue_status ON update_queue(status);
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_update_queue_status_priority
            ON update_queue (status, priority, created_at);
        """)

        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Error initializing Update queue DB: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()

def init_static_movie_db(db_path=None, conn=None):
    close_conn = False
    if conn is None:
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        # Movies
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS movies (
        tmdb_id           INTEGER PRIMARY KEY,
        trakt_id          INTEGER UNIQUE,
        title             TEXT,
        year              INTEGER,
        imdb_id           TEXT,
        tvdb_id           TEXT,
        tagline           TEXT,
        overview          TEXT,
        released          INTEGER,
        runtime           INTEGER,
        country           TEXT,
        rating            REAL,
        language          TEXT,
        certification     TEXT,
        original_title    TEXT,
        studio            TEXT,  -- Comma-separated list of studios
        poster_path TEXT,  -- Path to poster image
        fanart_path TEXT,  -- Path to fanart image
        thumbnail_path TEXT,  -- Path to thumbnail image
        clearlogo_path TEXT,  -- Path to clear logo image
        landscape_path TEXT,  -- Path to landscape image
        belongs_to_collection TEXT,  -- JSON string of collection details
        trailer           TEXT
        )
        """)

        # Genres
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS genres (
        name TEXT PRIMARY KEY
        )
        """)

        # Mapping
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS movie_genres (
        tmdb_id  INTEGER,
        trakt_id INTEGER,
        genre    TEXT,
        PRIMARY KEY (tmdb_id, genre),
        FOREIGN KEY (tmdb_id) REFERENCES movies(tmdb_id) ON DELETE CASCADE,
        FOREIGN KEY (genre)  REFERENCES genres(name) ON DELETE CASCADE
        )
        """)
        # Migration: Add fanart last updated and trailer columns if they do not exist
        for col, col_type in [("fanart_last_updated", "INTEGER"), ("trailer", "TEXT")]:
            try:
                cursor.execute(f"ALTER TABLE movies ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass # Already exists

        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Error initializing static movie DB: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()

def init_static_tvshows_db(db_path=None, conn=None):
    close_conn = False
    if conn is None:
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shows (
                show_tmdb_id INTEGER PRIMARY KEY,
                show_trakt_id INTEGER UNIQUE,
                title TEXT,
                original_title TEXT,
                year INTEGER,
                first_aired TEXT,
                slug TEXT,
                overview TEXT,
                imdb_id TEXT,
                last_updated TEXT,
                dropped integer DEFAULT 0,  -- 0 = not dropped, 1 = dropped
                status TEXT,  -- 'returning series', 'ended', 'in production', etc.
                poster_path TEXT,  -- Path to poster image
                fanart_path TEXT,  -- Path to fanart image
                thumbnail_path TEXT,  -- Path to thumbnail image
                clearlogo_path TEXT,  -- Path to clear logo image
                landscape_path TEXT,  -- Path to landscape image
                trailer TEXT,
                tagline TEXT,
                country TEXT,
                rating REAL,
                votes INTEGER,
                certification TEXT,
                network TEXT,
                language TEXT

            );
        """)

        # Migration: Add language, tvdb_id, and fanart_last_updated columns if they do not exist
        for col, col_type in [
            ("language", "TEXT"),
            ("tvdb_id", "TEXT"),
            ("fanart_last_updated", "INTEGER")
        ]:
            try:
                cursor.execute(f"ALTER TABLE shows ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass # Column already exists

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                episode_trakt_id INTEGER,
                show_id INTEGER,
                season INTEGER,
                episode_number INTEGER,
                episode_title TEXT,
                episode_overview TEXT,
                air_date TEXT,
                slug TEXT,
                tmdb_id INTEGER PRIMARY KEY,
                imdb_id TEXT,
                tvdb_id TEXT,
                rating REAL,
                first_aired TEXT,
                updated_at TEXT,
                votes INTEGER,
                runtime INTEGER,
                episode_type TEXT,
                original_title TEXT,
                episode_poster_path TEXT,  -- Path to poster image
                episode_fanart_path TEXT,  -- Path to fanart image
                episode_thumbnail_path TEXT,  -- Path to thumbnail image
                episode_clearlogo_path TEXT,  -- Path to clear logo image
                episode_landscape_path TEXT,  -- Path to landscape image
                intro TEXT,  -- Intro segment timestamps (JSON)
                outro TEXT,  -- Outro segment timestamps (JSON)
                FOREIGN KEY(show_id) REFERENCES shows(show_tmdb_id)
            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_episodes_show_season_number ON episodes(show_id, season, episode_number);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_episodes_lookup ON episodes (show_id, season, episode_number);
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seasons (
                season_id TEXT PRIMARY KEY,    -- Unique ID like "{show_id}:S{season_number}"
                show_id INTEGER,
                season INTEGER,
                title TEXT,
                overview TEXT,
                episode_count INTEGER,
                air_date TEXT,
                poster_path TEXT,  -- Path to poster image
                fanart_path TEXT,  -- Path to fanart image
                thumbnail_path TEXT,  -- Path to thumbnail image
                clearlogo_path TEXT,  -- Path to clear logo image
                landscape_path TEXT,  -- Path to landscape image
                FOREIGN KEY(show_id) REFERENCES shows(show_tmdb_id)
            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_seasons_show ON seasons(show_id);
        """)

        # Genres
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS genres (
        name TEXT PRIMARY KEY
        )
        """)

        # Mapping
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tvshows_genres (
        tmdb_id  INTEGER,
        genre    TEXT,
        PRIMARY KEY (tmdb_id, genre),
        FOREIGN KEY (tmdb_id) REFERENCES shows(tmdb_id) ON DELETE CASCADE,
        FOREIGN KEY (genre)  REFERENCES genres(name) ON DELETE CASCADE
        )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Error initializing static TV shows DB: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()

def init_dynamic_movie_db(db_path=None, conn=None):
    close_conn = False
    if conn is None:
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS movie_status (
                tmdb_id INTEGER PRIMARY KEY,
                trakt_id INTEGER,
                watched INTEGER DEFAULT 0, -- % watched, 100 = watched all
                watched_status INTEGER DEFAULT 0, -- 0: Unwatched, 1: In Progress, 2: Watched
                user_rating INTEGER,
                last_updated INTEGER
            )
        """)

        # Migration: Add watched_status column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE movie_status ADD COLUMN watched_status INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass # Column already exists

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_movie_status_updated
            ON movie_status (last_updated)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watched_history (
                tmdb_id INTEGER PRIMARY KEY,
                is_watched BOOLEAN DEFAULT 0,
                last_watched_at TEXT,
                trakt_synced_at TEXT,
                simkl_synced_at TEXT,
                mdblist_synced_at TEXT,
                punchplay_synced_at TEXT
            )
        """)

        # Migration: Add mdblist_synced_at column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE watched_history ADD COLUMN mdblist_synced_at TEXT")
        except sqlite3.OperationalError:
            pass # Column already exists

        # Migration: Add punchplay_synced_at column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE watched_history ADD COLUMN punchplay_synced_at TEXT")
        except sqlite3.OperationalError:
            pass # Column already exists

        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Error initializing dynamic DB: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()

def init_dynamic_tvshows_db(db_path=None, conn=None):
    close_conn = False
    if conn is None:
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watched_episodes (
                user TEXT NOT NULL,
                episode_trakt_id INTEGER,
                tmdb_id INTEGER NOT NULL,
                season INTEGER,
                episode INTEGER,
                watched_at TEXT,
                percent_watched INTEGER DEFAULT 100,  -- Percentage watched, 100 = fully watched
                watched_status INTEGER DEFAULT 2, -- 0: Unwatched, 1: In Progress, 2: Watched
                PRIMARY KEY (user, tmdb_id),
                FOREIGN KEY(tmdb_id) REFERENCES episodes(tmdb_id) ON DELETE CASCADE
            );
        """)

        # Migration: Add watched_status column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE watched_episodes ADD COLUMN watched_status INTEGER DEFAULT 2")
        except sqlite3.OperationalError:
            pass # Column already exists

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_show_sync (
                user TEXT NOT NULL,
                show_tmdb_id INTEGER NOT NULL,
                last_updated_at TEXT NOT NULL,
                watched_status INTEGER DEFAULT 0, -- 0: Unwatched, 1: In Progress, 2: Watched
                PRIMARY KEY (user, show_tmdb_id)
            );
        """)

        # Migration: Add watched_status column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE user_show_sync ADD COLUMN watched_status INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass # Column already exists

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_watched_user_episode ON watched_episodes(user, episode_trakt_id);
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watched_history (
                show_tmdb_id INTEGER,
                season INTEGER,
                episode INTEGER,
                is_watched BOOLEAN DEFAULT 0,
                last_watched_at TEXT,
                trakt_synced_at TEXT,
                simkl_synced_at TEXT,
                mdblist_synced_at TEXT,
                punchplay_synced_at TEXT,
                PRIMARY KEY (show_tmdb_id, season, episode)
            )
        """)
        
        # Migration: Add mdblist_synced_at column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE watched_history ADD COLUMN mdblist_synced_at TEXT")
        except sqlite3.OperationalError:
            pass # Column already exists
        
        # Migration: Add punchplay_synced_at column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE watched_history ADD COLUMN punchplay_synced_at TEXT")
        except sqlite3.OperationalError:
            pass # Column already exists
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_watched_history_show ON watched_history(show_tmdb_id);
        """)

        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Error initializing dynamic tvshows DB: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()

def init_lists_db(db_path=None, conn=None):
    close_conn = False
    if conn is None:
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        # Table for list metadata
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lists (
                list_id TEXT PRIMARY KEY,
                source TEXT,
                user TEXT,
                owned_by_user boolean DEFAULT false,  -- false = no, true = yes
                slug TEXT,
                name TEXT,
                description TEXT,
                last_checked TEXT,
                item_count_movies INTEGER,
                item_count_shows INTEGER,
                add_to_library BOOLEAN DEFAULT false
                )
        """)

        # Check if list_items table exists and needs migration
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='list_items'")
        table_exists = cursor.fetchone() is not None
        
        if table_exists:
            # Check current schema
            cursor.execute("PRAGMA table_info(list_items)")
            columns = {row[1]: row for row in cursor.fetchall()}
            
            # Migration needed if old schema detected
            if 'media_id' in columns and 'trakt_id' not in columns:
                log("[Orac] Migrating list_items table to new schema (media_id → trakt_id + tmdb_id)...", level=LOGINFO)
                
                # Create new table with correct schema
                cursor.execute("""
                    CREATE TABLE list_items_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        list_id TEXT,
                        media_type TEXT,
                        trakt_id TEXT,
                        tmdb_id TEXT,
                        FOREIGN KEY (list_id) REFERENCES lists(list_id) ON DELETE CASCADE
                    )
                """)
                
                # Migrate data with TMDB ID resolution
                # For placeholder Trakt IDs (negative), tmdb_id is abs(trakt_id)
                cursor.execute("""
                    INSERT INTO list_items_new (id, list_id, media_type, trakt_id, tmdb_id)
                    SELECT 
                        li.id,
                        li.list_id,
                        li.media_type,
                        li.media_id AS trakt_id,
                        CASE 
                            -- For negative (placeholder) Trakt IDs, TMDB ID is abs value
                            WHEN CAST(li.media_id AS INTEGER) < 0 THEN CAST(ABS(CAST(li.media_id AS INTEGER)) AS TEXT)
                            -- For movies, lookup in movies table
                            WHEN li.media_type = 'movie' THEN (
                                SELECT CAST(m.tmdb_id AS TEXT) 
                                FROM movies m 
                                WHERE m.trakt_id = CAST(li.media_id AS INTEGER)
                            )
                            -- For shows, lookup in shows table
                            WHEN li.media_type = 'show' THEN (
                                SELECT CAST(s.show_tmdb_id AS TEXT)
                                FROM shows s
                                WHERE s.show_trakt_id = CAST(li.media_id AS INTEGER)
                            )
                            ELSE NULL
                        END AS tmdb_id
                    FROM list_items li
                """)
                
                # Count migrated rows
                cursor.execute("SELECT COUNT(*) FROM list_items_new")
                migrated_count = cursor.fetchone()[0]
                
                # Drop old table and rename new one
                cursor.execute("DROP TABLE list_items")
                cursor.execute("ALTER TABLE list_items_new RENAME TO list_items")
                
                # Create indexes
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_list_items_list_id ON list_items(list_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_list_items_tmdb_id ON list_items(tmdb_id)")
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_list_items_unique ON list_items(list_id, trakt_id)")
                
                log(f"[Orac] Migration complete: {migrated_count} items migrated", level=LOGINFO)
        else:
            # Create table with new schema
            cursor.execute("""
                CREATE TABLE list_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_id TEXT,
                    media_type TEXT,
                    trakt_id TEXT,
                    tmdb_id TEXT,
                    FOREIGN KEY (list_id) REFERENCES lists(list_id) ON DELETE CASCADE
                )
            """)
            
            # Create indexes
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_list_items_list_id ON list_items(list_id);
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_list_items_tmdb_id ON list_items(tmdb_id);
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_list_items_unique ON list_items(list_id, trakt_id);
            """)
        
        # Clean up any existing string representations of None, null or empty values to database NULL
        cursor.execute("""
            UPDATE list_items 
            SET tmdb_id = NULL 
            WHERE tmdb_id IS NOT NULL 
              AND (LOWER(tmdb_id) = 'none' OR LOWER(tmdb_id) = 'null' OR TRIM(tmdb_id) = '')
        """)
        cursor.execute("""
            UPDATE list_items 
            SET trakt_id = NULL 
            WHERE trakt_id IS NOT NULL 
              AND (LOWER(trakt_id) = 'none' OR LOWER(trakt_id) = 'null' OR TRIM(trakt_id) = '')
        """)
        
        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Failed to initialize lists database: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()

def init_config_db(db_path=None, conn=None):
    close_conn = False
    if conn is None:
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        # Table for configuration settings
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key VARCHAR(255) PRIMARY KEY,
                value TEXT,
                user_id TEXT,
                is_encrypted BOOLEAN DEFAULT false
            )
        """)
        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Failed to initialize config database: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()

def init_ext_indexes_db(db_path=None, conn=None):
    close_conn = False
    if conn is None:
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS external_indexes (
                id TEXT NOT NULL,
                media_type TEXT NOT NULL,  -- 'movie' or 'show'
                parameters TEXT,  -- JSON string of parameters
                add_to_library INTEGER DEFAULT 0,
                PRIMARY KEY (id, media_type)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS internal_indexes (
                id TEXT NOT NULL,
                media_type TEXT NOT NULL,  -- 'movie' or 'show'
                parameters TEXT,  -- JSON string of parameters (e.g., genre filters)
                add_to_library INTEGER DEFAULT 0,
                PRIMARY KEY (id, media_type)
            )
        """)

        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Error creating extended indexes: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()

def init_tags_db(db_path=None, conn=None):
    """Initialize tags database with tags and tag_items tables."""
    close_conn = False
    if conn is None:
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        # Table for unique tags
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                tag_name TEXT PRIMARY KEY
            )
        """)

        # Table for tag-item associations
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tag_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_name TEXT NOT NULL,
                media_type TEXT NOT NULL,  -- 'movie' or 'show'
                tmdb_id INTEGER NOT NULL,
                trakt_id INTEGER,
                FOREIGN KEY (tag_name) REFERENCES tags(tag_name) ON DELETE CASCADE,
                UNIQUE(tag_name, media_type, tmdb_id)
            )
        """)

        # Create indexes for fast lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tag_items_tag ON tag_items(tag_name);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tag_items_media ON tag_items(media_type, tmdb_id);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tag_items_trakt ON tag_items(trakt_id);
        """)

        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Failed to initialize tags database: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()

def init_undesirables_db(db_path=None, conn=None):
    """Initialize undesirables database with defaults."""
    close_conn = False
    if conn is None:
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        cursor.execute('''CREATE TABLE IF NOT EXISTS undesirables (
            keyword TEXT NOT NULL, 
            user_defined BOOL NOT NULL, 
            enabled BOOL NOT NULL, 
            UNIQUE(keyword)
        )''')

        # Insert defaults safely
        from resources.scrapers.modules.source_utils import UNDESIRABLES
        default_entries = [(keyword, False, True) for keyword in UNDESIRABLES]
        cursor.executemany('INSERT OR IGNORE INTO undesirables VALUES (?, ?, ?)', default_entries)

        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Failed to initialize undesirables database: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()


def init_trakt_history_sync_db(db_path=None, conn=None):
    """
    Initialize the trakt_history_sync database.

    Table: trakt_history_sync
    ─────────────────────────
    Maps a local queue row (local_id) to a Trakt history entry
    (trakt_history_id) and tracks whether the item is currently present
    on Trakt (is_on_trakt).  This supports the 90k-ceiling maintenance
    logic: items that are pruned from Trakt are flagged with
    is_on_trakt = 0 but their local record is preserved.

    Columns
    -------
    local_id         – PRIMARY KEY; matches update_queue.id
    tmdb_id          – TMDB ID of the movie or episode
    media_type       – 'movie' or 'episode'
    trakt_history_id – Trakt-assigned history entry ID (nullable until confirmed)
    watched_at       – ISO-8601 UTC timestamp of the watch event
    is_on_trakt      – 1 if the item exists in Trakt history, 0 if pruned
    synced_at        – Unix epoch when the row was last synced to Trakt
    purged_at        – Unix epoch when the row was purged from Trakt (nullable)
    """
    close_conn = False
    if conn is None:
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trakt_history_sync (
                local_id         INTEGER PRIMARY KEY,
                tmdb_id          INTEGER,
                media_type       TEXT NOT NULL,
                trakt_history_id INTEGER UNIQUE,
                watched_at       TEXT,
                is_on_trakt      INTEGER NOT NULL DEFAULT 1,
                synced_at        INTEGER,
                purged_at        INTEGER
            )
        """)

        # Fast look-up by Trakt history ID (used during purge verification)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trakthist_history_id
            ON trakt_history_sync (trakt_history_id)
        """)

        # Fast look-up by TMDB ID + media type (used for enrichment queries)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trakthist_tmdb
            ON trakt_history_sync (tmdb_id, media_type)
        """)

        # Filter by sync state
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trakthist_on_trakt
            ON trakt_history_sync (is_on_trakt)
        """)

        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Failed to initialize trakt_history_sync database: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()


def init_anime_db(db_path=None, conn=None):
    """
    Initialize the two-layer Anime Identity & Episode Coordinate Mapping database.
    - Layer 1 (Manami Project): anime_identities & anime_aliases
    - Layer 2 (AniBridge Mappings): anime_episode_mappings & anime_custom_overrides
    """
    close_conn = False
    if conn is None:
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        # Layer 1: Global Anime Identity Cross-Reference Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS anime_identities (
                anilist_id      INTEGER PRIMARY KEY,
                mal_id          INTEGER,
                anidb_id        INTEGER,
                kitsu_id        INTEGER,
                simkl_id        INTEGER,
                tmdb_id         INTEGER,
                tmdb_media_type TEXT DEFAULT 'tv',
                tvdb_id         INTEGER,
                imdb_id         TEXT,
                title           TEXT,
                title_romaji    TEXT,
                title_english   TEXT,
                title_native    TEXT,
                anime_type      TEXT,
                episodes        INTEGER,
                status          TEXT,
                season_name     TEXT,
                release_year    INTEGER,
                picture         TEXT,
                thumbnail       TEXT,
                synonyms_json   TEXT,
                tags_json       TEXT,
                updated_at      INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_anime_id_mal ON anime_identities(mal_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_anime_id_anidb ON anime_identities(anidb_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_anime_id_kitsu ON anime_identities(kitsu_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_anime_id_simkl ON anime_identities(simkl_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_anime_id_tmdb ON anime_identities(tmdb_id, tmdb_media_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_anime_id_tvdb ON anime_identities(tvdb_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_anime_id_imdb ON anime_identities(imdb_id)")

        # Layer 1: Normalized Search Aliases (Romaji, English, Kanji, Synonyms)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS anime_aliases (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                anilist_id       INTEGER NOT NULL,
                alias            TEXT NOT NULL,
                normalized_alias TEXT NOT NULL,
                alias_type       TEXT DEFAULT 'synonym',
                UNIQUE(anilist_id, alias),
                FOREIGN KEY (anilist_id) REFERENCES anime_identities(anilist_id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_anime_aliases_anilist ON anime_aliases(anilist_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_anime_aliases_norm ON anime_aliases(normalized_alias)")

        # Layer 2: Episode Coordinate Mappings (AniBridge v3 / v2 descriptors & ranges)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS anime_episode_mappings (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                source_descriptor TEXT NOT NULL,
                source_provider   TEXT NOT NULL,
                source_id         TEXT NOT NULL,
                source_scope      TEXT DEFAULT '',
                target_descriptor TEXT NOT NULL,
                target_provider   TEXT NOT NULL,
                target_id         TEXT NOT NULL,
                target_scope      TEXT DEFAULT '',
                range_map_json    TEXT NOT NULL,
                UNIQUE(source_descriptor, target_descriptor)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_aem_source ON anime_episode_mappings(source_provider, source_id, source_scope)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_aem_target ON anime_episode_mappings(target_provider, target_id, target_scope)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_aem_source_desc ON anime_episode_mappings(source_descriptor)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_aem_target_desc ON anime_episode_mappings(target_descriptor)")

        # Layer 2: Custom Overrides Engine Table (takes priority over downloaded AniBridge rules)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS anime_custom_overrides (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                anilist_id        INTEGER,
                tmdb_id           INTEGER,
                tvdb_id           INTEGER,
                media_type        TEXT DEFAULT 'tv',
                season            INTEGER,
                source_descriptor TEXT NOT NULL,
                target_descriptor TEXT NOT NULL,
                range_map_json    TEXT NOT NULL,
                notes             TEXT,
                updated_at        INTEGER DEFAULT (strftime('%s', 'now')),
                UNIQUE(source_descriptor, target_descriptor)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_aco_anilist ON anime_custom_overrides(anilist_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_aco_tmdb ON anime_custom_overrides(tmdb_id, season)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_aco_source_desc ON anime_custom_overrides(source_descriptor)")

        # Sync Metadata & Watermarks
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS anime_sync_meta (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)

        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Failed to initialize anime mappings database: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()
