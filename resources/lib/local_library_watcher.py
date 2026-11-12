import os
import re
import time
import json
import sqlite3
import threading
from typing import Optional, Dict, Any, List, Tuple

from resources.lib.log_utils import log, LOGINFO, LOGERROR, LOGWARNING, LOGDEBUG
from resources.lib.db_utils import db_connect, add_tvshow
from resources.lib.config_handler import get_config_value, update_config_values
from resources.lib.local_folders_handler import SUPPORTED_VIDEO_EXTENSIONS
from resources.lib.media_inspector import inspect_media_file


def init_local_media_library_table(config_db_path: str):
    """Initializes the local_media_library table in config.db."""
    with db_connect(config_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS local_media_library (
                path TEXT PRIMARY KEY,
                media_type TEXT,
                folder_slot TEXT,
                file_size INTEGER,
                mtime REAL,
                parsed_title TEXT,
                parsed_year INTEGER,
                season INTEGER,
                episode INTEGER,
                tmdb_id INTEGER,
                trakt_id INTEGER,
                video_codec TEXT,
                audio_codec TEXT,
                resolution TEXT,
                width INTEGER,
                height INTEGER,
                last_scanned REAL
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_local_media_type ON local_media_library(media_type);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_local_media_tmdb ON local_media_library(tmdb_id);")
        conn.commit()


def parse_media_filename(file_path: str, media_type: str = "movie") -> Dict[str, Any]:
    """
    Parses a local media file path to determine title, year, season, and episode.
    """
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    parent_dir = os.path.basename(os.path.dirname(file_path))

    if media_type == "movie":
        if base_name.lower() in ("movie", "video", "cd1", "cd2", "part1", "part2", "main") and parent_dir:
            raw_title = parent_dir
        else:
            raw_title = base_name

        year = None
        year_match = re.search(r'[\.\s\(_\-]((?:19|20)\d{2})(?:[\.\s\)_\\-]|$)', raw_title)
        if year_match:
            year = int(year_match.group(1))
            raw_title = raw_title[:year_match.start()]
        elif parent_dir:
            ym = re.search(r'[\.\s\(_\-]((?:19|20)\d{2})(?:[\.\s\)_\\-]|$)', parent_dir)
            if ym:
                year = int(ym.group(1))

        tags_pattern = r'\b(1080p|720p|480p|2160p|4k|uhd|bluray|bdrip|brrip|dvdrip|web-dl|webrip|hdtv|x264|x265|h264|h265|hevc|avc|aac|dts|ac3|atmos|hdr|remux|yify|rarbg|eztv|proper|repack)\b'
        clean = re.split(tags_pattern, raw_title, flags=re.IGNORECASE)[0]
        clean = re.sub(r'[\.\-_\(\)\[\]]', ' ', clean)
        clean = ' '.join(clean.split())
        return {'title': clean, 'year': year, 'season': None, 'episode': None}

    else:
        ep_match = re.search(r'[Ss](\d{1,2})[Ee](\d{1,3})', base_name)
        if not ep_match:
            ep_match = re.search(r'(\d{1,2})x(\d{1,3})', base_name)

        season = None
        episode = None
        grandparent = os.path.basename(os.path.dirname(os.path.dirname(file_path))) if os.path.dirname(file_path) else None
        if ep_match:
            season = int(ep_match.group(1))
            episode = int(ep_match.group(2))
            show_raw = base_name[:ep_match.start()].strip()
            if len(show_raw) < 2 and parent_dir:
                if re.match(r'season\s*\d+', parent_dir, re.IGNORECASE) and grandparent:
                    show_raw = grandparent
                else:
                    show_raw = parent_dir
        else:
            show_raw = parent_dir or base_name

        year = None
        year_match = re.search(r'[\.\s\(_\-]((?:19|20)\d{2})(?:[\.\s\)_\\-]|$)', show_raw)
        if year_match:
            year = int(year_match.group(1))
            show_raw = show_raw[:year_match.start()]
        else:
            for folder in (grandparent, parent_dir):
                if folder:
                    ym = re.search(r'[\.\s\(_\-]((?:19|20)\d{2})(?:[\.\s\)_\\-]|$)', folder)
                    if ym:
                        year = int(ym.group(1))
                        break

        clean = re.sub(r'[\.\-_\(\)\[\]]', ' ', show_raw)
        clean = ' '.join(clean.split())
        return {'title': clean, 'year': year, 'season': season, 'episode': episode}


class LocalLibraryWatcher:
    """
    Monitors local media folders configured in Orac Server, performs automatic
    metadata enrichment with TMDb, extracts technical audio/video specs, and
    maintains 'Orac Local' lists in lists_cache.db.
    """

    def __init__(
        self,
        config_db_path: str,
        movies_static_db_path: str,
        movies_dynamic_db_path: str,
        tvshows_static_db_path: str,
        tvshows_dynamic_db_path: str,
        lists_db_path: str,
        trakt_update_queue_path: Optional[str] = None,
        tmdb_handler=None,
        trakt_handler=None,
        scan_interval: int = 900
    ):
        self.config_db_path = config_db_path
        self.movies_static_db_path = movies_static_db_path
        self.movies_dynamic_db_path = movies_dynamic_db_path
        self.tvshows_static_db_path = tvshows_static_db_path
        self.tvshows_dynamic_db_path = tvshows_dynamic_db_path
        self.lists_db_path = lists_db_path
        self.trakt_update_queue_path = trakt_update_queue_path
        self.tmdb_handler = tmdb_handler
        self.trakt_handler = trakt_handler
        self.scan_interval = scan_interval

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._scan_lock = threading.Lock()
        self._last_scan_time: float = 0.0
        self._last_scan_results: Dict[str, Any] = {}

    def start(self):
        """Starts the background watcher thread."""
        init_local_media_library_table(self.config_db_path)
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="LocalLibraryWatcher", daemon=True)
            self._thread.start()
            log("[LocalWatcher] Local library watcher background service started.", level=LOGINFO)

    def stop(self):
        """Stops the background watcher thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
            log("[LocalWatcher] Local library watcher background service stopped.", level=LOGINFO)

    def trigger_scan(self, synchronous: bool = False) -> Dict[str, Any]:
        """Triggers an immediate scan either synchronously or in background."""
        if synchronous:
            return self.run_scan()
        else:
            threading.Thread(target=self.run_scan, name="LocalLibraryScanTrigger", daemon=True).start()
            return {"status": "started", "message": "Local library scan triggered in background"}

    def _run_loop(self):
        """Background daemon loop."""
        # Initial boot delay to allow server and TMDb handler initialization
        time.sleep(10)
        while not self._stop_event.is_set():
            try:
                self.run_scan()
            except Exception as e:
                log(f"[LocalWatcher] Error during library scan loop: {e}", level=LOGERROR)

            # Sleep in 1-second intervals for responsive shutdown
            slept = 0
            while slept < self.scan_interval and not self._stop_event.is_set():
                time.sleep(1)
                slept += 1

    def run_scan(self) -> Dict[str, Any]:
        """
        Executes a scan over configured local movie and TV directories.
        Performs fast mtime/size diffing and synchronizes database caches.
        """
        if not self._scan_lock.acquire(blocking=False):
            log("[LocalWatcher] Scan already in progress, skipping trigger.", level=LOGDEBUG)
            return {"status": "in_progress", "message": "Scan already in progress"}

        start_time = time.time()
        log("[LocalWatcher] Starting local library scan...", level=LOGINFO)

        stats = {
            "movies_scanned": 0,
            "movies_added": 0,
            "movies_modified": 0,
            "movies_deleted": 0,
            "shows_scanned": 0,
            "shows_added": 0,
            "shows_modified": 0,
            "shows_deleted": 0,
            "errors": 0,
            "duration": 0.0
        }

        try:
            # 1. Load existing database state: {path: (mtime, file_size, tmdb_id, media_type)}
            with db_connect(self.config_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT path, mtime, file_size, tmdb_id, media_type FROM local_media_library")
                db_files = {row[0]: (row[1], row[2], row[3], row[4]) for row in cursor.fetchall()}

            seen_paths = set()
            changed_movies = False
            changed_shows = False

            # 2. Collect scan targets from config slots 1..5
            targets_movies: List[Tuple[str, str]] = []
            targets_shows: List[Tuple[str, str]] = []

            for slot in range(1, 6):
                s = str(slot)
                m_dir = get_config_value(f"local_folder.{s}.movies_directory", self.config_db_path, default="none")
                tv_dir = get_config_value(f"local_folder.{s}.tv_shows_directory", self.config_db_path, default="none")
                if m_dir and m_dir.lower() not in ("none", "empty_setting", "") and os.path.isdir(os.path.normpath(m_dir)):
                    targets_movies.append((s, os.path.normpath(m_dir)))
                if tv_dir and tv_dir.lower() not in ("none", "empty_setting", "") and os.path.isdir(os.path.normpath(tv_dir)):
                    targets_shows.append((s, os.path.normpath(tv_dir)))

            # 3. Scan Movies
            for slot, root_dir in targets_movies:
                for root, _, files in os.walk(root_dir):
                    for fname in files:
                        if self._stop_event.is_set():
                            break
                        ext = os.path.splitext(fname)[-1].lower()
                        if ext not in SUPPORTED_VIDEO_EXTENSIONS:
                            continue
                        full_path = os.path.join(root, fname)
                        try:
                            st = os.stat(full_path)
                            size_bytes = st.st_size
                            mtime = st.st_mtime
                        except OSError:
                            continue

                        # Skip samples (< 50MB with sample in name)
                        if "sample" in fname.lower() and size_bytes < 52428800:
                            continue

                        seen_paths.add(full_path)
                        stats["movies_scanned"] += 1

                        # Incremental check: unchanged if mtime & size match and valid tmdb_id
                        existing = db_files.get(full_path)
                        if existing and existing[0] == mtime and existing[1] == size_bytes and existing[2]:
                            continue

                        # New or modified movie file
                        is_new = existing is None
                        success = self._process_movie_file(full_path, slot, size_bytes, mtime)
                        if success:
                            if is_new:
                                stats["movies_added"] += 1
                            else:
                                stats["movies_modified"] += 1
                            changed_movies = True
                        else:
                            stats["errors"] += 1

            # 4. Scan TV Shows
            for slot, root_dir in targets_shows:
                for root, _, files in os.walk(root_dir):
                    for fname in files:
                        if self._stop_event.is_set():
                            break
                        ext = os.path.splitext(fname)[-1].lower()
                        if ext not in SUPPORTED_VIDEO_EXTENSIONS:
                            continue
                        full_path = os.path.join(root, fname)
                        try:
                            st = os.stat(full_path)
                            size_bytes = st.st_size
                            mtime = st.st_mtime
                        except OSError:
                            continue

                        if "sample" in fname.lower() and size_bytes < 52428800:
                            continue

                        seen_paths.add(full_path)
                        stats["shows_scanned"] += 1

                        existing = db_files.get(full_path)
                        if existing and existing[0] == mtime and existing[1] == size_bytes and existing[2]:
                            continue

                        is_new = existing is None
                        success = self._process_show_file(full_path, slot, size_bytes, mtime)
                        if success:
                            if is_new:
                                stats["shows_added"] += 1
                            else:
                                stats["shows_modified"] += 1
                            changed_shows = True
                        else:
                            stats["errors"] += 1

            # 5. Handle deleted files
            deleted_paths = set(db_files.keys()) - seen_paths
            if deleted_paths:
                with db_connect(self.config_db_path) as conn:
                    cursor = conn.cursor()
                    for p in deleted_paths:
                        m_type = db_files[p][3]
                        if m_type == "movie":
                            stats["movies_deleted"] += 1
                            changed_movies = True
                        else:
                            stats["shows_deleted"] += 1
                            changed_shows = True
                        cursor.execute("DELETE FROM local_media_library WHERE path = ?", (p,))
                    conn.commit()

            # 6. Update lists if any changes or if lists are missing
            self._sync_local_lists(force=bool(changed_movies or changed_shows))

        except Exception as e:
            log(f"[LocalWatcher] Unhandled error during scan: {e}", level=LOGERROR)
            import traceback
            log(traceback.format_exc(), level=LOGERROR)
            stats["errors"] += 1
        finally:
            stats["duration"] = round(time.time() - start_time, 3)
            self._last_scan_time = time.time()
            self._last_scan_results = stats
            self._scan_lock.release()
            log(f"[LocalWatcher] Scan completed in {stats['duration']}s: {stats}", level=LOGINFO)

        return stats

    def _process_movie_file(self, full_path: str, slot: str, size_bytes: int, mtime: float) -> bool:
        """Inspects, parses, looks up TMDb, enriches static cache, and saves movie to DB."""
        try:
            parsed = parse_media_filename(full_path, "movie")
            title = parsed["title"]
            year = parsed["year"]

            if not title:
                return False

            # Inspect media container specs
            media_info = inspect_media_file(full_path) or {}
            quality = media_info.get("quality") or "SD"
            vcodec = media_info.get("video_codec") or ""
            acodec = media_info.get("audio_codec") or ""
            width = media_info.get("width") or 0
            height = media_info.get("height") or 0

            # Match in TMDb
            tmdb_id = None
            movie_details = None
            if self.tmdb_handler:
                search_res = self.tmdb_handler.search("movie", title)
                results = search_res.get("results", []) if isinstance(search_res, dict) else []

                # Find best matching result
                matched = None
                if year:
                    for r in results:
                        if isinstance(r, dict):
                            r_date = r.get("release_date") or ""
                            if r_date.startswith(str(year)):
                                matched = r
                                break

                if not matched and results and isinstance(results[0], dict):
                    matched = results[0]

                if matched and isinstance(matched, dict):
                    tmdb_id = matched.get("id")
                    if tmdb_id:
                        found, details = self.tmdb_handler.get_movie_details_from_tmdb(tmdb_id)
                        if found and isinstance(details, dict):
                            movie_details = details

            if not tmdb_id:
                log(f"[LocalWatcher] Could not match movie on TMDb: '{title}' ({full_path})", level=LOGWARNING)
                return False

            # Enrich static DB cache with movie metadata, posters, fanart
            if movie_details:
                self._save_movie_to_static_db(tmdb_id, movie_details)

            # Save file record in local_media_library
            with db_connect(self.config_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO local_media_library (
                        path, media_type, folder_slot, file_size, mtime,
                        parsed_title, parsed_year, season, episode,
                        tmdb_id, trakt_id, video_codec, audio_codec,
                        resolution, width, height, last_scanned
                    ) VALUES (?, 'movie', ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        folder_slot=excluded.folder_slot,
                        file_size=excluded.file_size,
                        mtime=excluded.mtime,
                        parsed_title=excluded.parsed_title,
                        parsed_year=excluded.parsed_year,
                        tmdb_id=excluded.tmdb_id,
                        trakt_id=excluded.trakt_id,
                        video_codec=excluded.video_codec,
                        audio_codec=excluded.audio_codec,
                        resolution=excluded.resolution,
                        width=excluded.width,
                        height=excluded.height,
                        last_scanned=excluded.last_scanned
                """, (
                    full_path, slot, size_bytes, mtime,
                    title, year, tmdb_id, -tmdb_id,
                    vcodec, acodec, quality, width, height, time.time()
                ))
                conn.commit()

            log(f"[LocalWatcher] Enriched movie: '{movie_details.get('title') if movie_details else title}' (TMDb: {tmdb_id}) [{quality} | {vcodec} | {acodec}]", level=LOGINFO)
            return True

        except Exception as e:
            log(f"[LocalWatcher] Error processing movie file {full_path}: {e}", level=LOGERROR)
            return False

    def _save_movie_to_static_db(self, tmdb_id: int, details: Dict[str, Any]):
        """Persists movie details, artwork, and genres into movies_static_cache.db."""
        try:
            images = details.get("images", {})
            released_val = 0
            r_str = str(details.get("released", ""))
            if len(r_str) >= 8 and r_str.isdigit():
                released_val = int(r_str[:8])
            elif len(r_str) == 4 and r_str.isdigit():
                released_val = int(r_str) * 10000 + 101

            belongs_to = details.get("belongs_to")
            serialized_belongs_to = json.dumps(belongs_to) if belongs_to else None
            studios = details.get("studios", [])
            serialized_studios = json.dumps(studios) if studios else None

            safe_trakt_id = -tmdb_id

            with db_connect(self.movies_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO movies (
                        tmdb_id, trakt_id, title, year, imdb_id, tagline, overview,
                        released, runtime, country, rating, certification, original_title,
                        studio, poster_path, fanart_path, thumbnail_path, clearlogo_path,
                        landscape_path, belongs_to_collection, trailer
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tmdb_id) DO UPDATE SET
                        title=COALESCE(excluded.title, movies.title),
                        year=COALESCE(excluded.year, movies.year),
                        imdb_id=COALESCE(excluded.imdb_id, movies.imdb_id),
                        tagline=COALESCE(excluded.tagline, movies.tagline),
                        overview=COALESCE(excluded.overview, movies.overview),
                        released=COALESCE(excluded.released, movies.released),
                        runtime=COALESCE(excluded.runtime, movies.runtime),
                        country=COALESCE(excluded.country, movies.country),
                        rating=COALESCE(excluded.rating, movies.rating),
                        certification=COALESCE(excluded.certification, movies.certification),
                        original_title=COALESCE(excluded.original_title, movies.original_title),
                        studio=COALESCE(excluded.studio, movies.studio),
                        poster_path=COALESCE(excluded.poster_path, movies.poster_path),
                        fanart_path=COALESCE(excluded.fanart_path, movies.fanart_path),
                        thumbnail_path=COALESCE(excluded.thumbnail_path, movies.thumbnail_path),
                        clearlogo_path=COALESCE(excluded.clearlogo_path, movies.clearlogo_path),
                        landscape_path=COALESCE(excluded.landscape_path, movies.landscape_path),
                        belongs_to_collection=COALESCE(excluded.belongs_to_collection, movies.belongs_to_collection),
                        trailer=COALESCE(excluded.trailer, movies.trailer)
                """, (
                    tmdb_id, safe_trakt_id, details.get("title", ""), details.get("year", 0),
                    details.get("imdb_id", ""), details.get("tagline", ""), details.get("overview", ""),
                    released_val, details.get("runtime", 0), details.get("country", ""),
                    details.get("rating", 0.0), details.get("certification", ""), details.get("original_title", ""),
                    serialized_studios, images.get("poster"), images.get("fanart"), images.get("thumb"),
                    images.get("clearlogo"), images.get("landscape"), serialized_belongs_to, details.get("trailer", "")
                ))

                for genre_info in details.get("genres", []):
                    genre_name = genre_info.get("name") if isinstance(genre_info, dict) else str(genre_info)
                    if genre_name:
                        genre_slug = genre_name.lower().replace(" ", "-")
                        cursor.execute("INSERT OR IGNORE INTO genres(name) VALUES(?)", (genre_slug,))
                        cursor.execute("INSERT OR IGNORE INTO movie_genres(tmdb_id, trakt_id, genre) VALUES(?, ?, ?)", (tmdb_id, safe_trakt_id, genre_slug))
                conn.commit()

        except Exception as e:
            log(f"[LocalWatcher] Error saving movie {tmdb_id} to static DB: {e}", level=LOGERROR)

    def _process_show_file(self, full_path: str, slot: str, size_bytes: int, mtime: float) -> bool:
        """Inspects, parses, looks up TMDb, enriches static cache, and saves show to DB."""
        try:
            parsed = parse_media_filename(full_path, "show")
            title = parsed["title"]
            year = parsed["year"]
            season = parsed["season"]
            episode = parsed["episode"]

            if not title:
                return False

            media_info = inspect_media_file(full_path) or {}
            quality = media_info.get("quality") or "SD"
            vcodec = media_info.get("video_codec") or ""
            acodec = media_info.get("audio_codec") or ""
            width = media_info.get("width") or 0
            height = media_info.get("height") or 0

            show_tmdb_id = None
            if self.tmdb_handler:
                search_res = self.tmdb_handler.search("tv_show", title)
                results = search_res.get("results", []) if isinstance(search_res, dict) else []
                if not results:
                    search_res = self.tmdb_handler.search("tv", title)
                    results = search_res.get("results", []) if isinstance(search_res, dict) else []

                matched = None
                if year:
                    for r in results:
                        if isinstance(r, dict):
                            air_date = r.get("first_air_date") or ""
                            if air_date.startswith(str(year)):
                                matched = r
                                break
                if not matched and results and isinstance(results[0], dict):
                    matched = results[0]

                if matched and isinstance(matched, dict):
                    show_tmdb_id = matched.get("id")

            if not show_tmdb_id:
                log(f"[LocalWatcher] Could not match TV show on TMDb: '{title}' ({full_path})", level=LOGWARNING)
                return False

            # Ensure show exists in static TV DB
            self._ensure_show_in_static_db(show_tmdb_id, title)

            with db_connect(self.config_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO local_media_library (
                        path, media_type, folder_slot, file_size, mtime,
                        parsed_title, parsed_year, season, episode,
                        tmdb_id, trakt_id, video_codec, audio_codec,
                        resolution, width, height, last_scanned
                    ) VALUES (?, 'show', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        folder_slot=excluded.folder_slot,
                        file_size=excluded.file_size,
                        mtime=excluded.mtime,
                        parsed_title=excluded.parsed_title,
                        parsed_year=excluded.parsed_year,
                        season=excluded.season,
                        episode=excluded.episode,
                        tmdb_id=excluded.tmdb_id,
                        trakt_id=excluded.trakt_id,
                        video_codec=excluded.video_codec,
                        audio_codec=excluded.audio_codec,
                        resolution=excluded.resolution,
                        width=excluded.width,
                        height=excluded.height,
                        last_scanned=excluded.last_scanned
                """, (
                    full_path, slot, size_bytes, mtime,
                    title, year, season, episode,
                    show_tmdb_id, -show_tmdb_id,
                    vcodec, acodec, quality, width, height, time.time()
                ))
                conn.commit()

            log(f"[LocalWatcher] Enriched show episode: '{title}' S{season:02d}E{episode:02d} (TMDb: {show_tmdb_id}) [{quality} | {vcodec}]", level=LOGINFO)
            return True

        except Exception as e:
            log(f"[LocalWatcher] Error processing show file {full_path}: {e}", level=LOGERROR)
            return False

    def _ensure_show_in_static_db(self, show_tmdb_id: int, title_fallback: str):
        """Ensures the TV show and its seasons/episodes are populated in tvshows_static_cache.db."""
        try:
            with db_connect(self.tvshows_static_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT show_tmdb_id FROM shows WHERE show_tmdb_id = ?", (show_tmdb_id,))
                if cursor.fetchone():
                    return

            if self.tmdb_handler:
                full_show = self.tmdb_handler.get_full_show_details(show_tmdb_id)
                if full_show:
                    show_payload = {
                        "ids": {
                            "trakt": -show_tmdb_id,
                            "tmdb": show_tmdb_id
                        },
                        "title": full_show.get("name") or title_fallback,
                        "genres": full_show.get("genres", [])
                    }
                    with db_connect(self.tvshows_static_db_path) as s_conn:
                        s_cur = s_conn.cursor()
                        add_tvshow(
                            tvshows_static_cursor=s_cur,
                            tvshows_dynamic_cursor=None,
                            trakt_queue_cursor=None,
                            media_id={"tmdb": show_tmdb_id, "trakt": -show_tmdb_id},
                            trakt_handler=self.trakt_handler,
                            tmdb_handler=self.tmdb_handler,
                            show=show_payload
                        )
                        s_conn.commit()
        except Exception as e:
            log(f"[LocalWatcher] Error ensuring show {show_tmdb_id} in static DB: {e}", level=LOGERROR)

    def _sync_local_lists(self, force: bool = False):
        """
        Maintains the 'Orac Local' lists ('Local Movies' and 'Local TV Shows')
        and their items in lists_cache.db.
        """
        try:
            # Query distinct movies and shows from local_media_library
            with db_connect(self.config_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT DISTINCT tmdb_id, trakt_id
                    FROM local_media_library
                    WHERE media_type = 'movie' AND tmdb_id IS NOT NULL
                """)
                local_movies = cursor.fetchall()

                cursor.execute("""
                    SELECT DISTINCT tmdb_id, trakt_id
                    FROM local_media_library
                    WHERE media_type = 'show' AND tmdb_id IS NOT NULL
                """)
                local_shows = cursor.fetchall()

            movies_list_id = "local:movies:orac_local"
            shows_list_id = "local:shows:orac_local"
            user_label = "Orac Local"
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            with db_connect(self.lists_db_path) as conn:
                cursor = conn.cursor()

                # Check if lists already exist
                cursor.execute("SELECT list_id FROM lists WHERE list_id = ?", (movies_list_id,))
                has_movies_list = cursor.fetchone() is not None

                cursor.execute("SELECT list_id FROM lists WHERE list_id = ?", (shows_list_id,))
                has_shows_list = cursor.fetchone() is not None

                # 1. Upsert Local Movies list metadata
                cursor.execute("""
                    INSERT INTO lists (
                        list_id, source, user, owned_by_user, slug, name,
                        description, last_checked, item_count_movies, item_count_shows, add_to_library
                    ) VALUES (?, 'local', ?, 1, 'orac-local-movies', 'Local Movies',
                              'Media stored in local Orac server folders', ?, ?, 0, 1)
                    ON CONFLICT(list_id) DO UPDATE SET
                        source='local',
                        user=excluded.user,
                        owned_by_user=1,
                        slug='orac-local-movies',
                        name='Local Movies',
                        last_checked=excluded.last_checked,
                        item_count_movies=excluded.item_count_movies,
                        item_count_shows=0,
                        add_to_library=1
                """, (movies_list_id, user_label, now_iso, len(local_movies)))

                # 2. Upsert Local TV Shows list metadata
                cursor.execute("""
                    INSERT INTO lists (
                        list_id, source, user, owned_by_user, slug, name,
                        description, last_checked, item_count_movies, item_count_shows, add_to_library
                    ) VALUES (?, 'local', ?, 1, 'orac-local-shows', 'Local TV Shows',
                              'Media stored in local Orac server folders', ?, 0, ?, 1)
                    ON CONFLICT(list_id) DO UPDATE SET
                        source='local',
                        user=excluded.user,
                        owned_by_user=1,
                        slug='orac-local-shows',
                        name='Local TV Shows',
                        last_checked=excluded.last_checked,
                        item_count_movies=0,
                        item_count_shows=excluded.item_count_shows,
                        add_to_library=1
                """, (shows_list_id, user_label, now_iso, len(local_shows)))

                # 3. Synchronize items for Local Movies
                cursor.execute("DELETE FROM list_items WHERE list_id = ?", (movies_list_id,))
                for tmdb_id, trakt_id in local_movies:
                    t_id = trakt_id if trakt_id is not None else -tmdb_id
                    cursor.execute("""
                        INSERT INTO list_items (list_id, media_type, trakt_id, tmdb_id)
                        VALUES (?, 'movie', ?, ?)
                    """, (movies_list_id, str(t_id), str(tmdb_id)))

                # 4. Synchronize items for Local TV Shows
                cursor.execute("DELETE FROM list_items WHERE list_id = ?", (shows_list_id,))
                for tmdb_id, trakt_id in local_shows:
                    t_id = trakt_id if trakt_id is not None else -tmdb_id
                    cursor.execute("""
                        INSERT INTO list_items (list_id, media_type, trakt_id, tmdb_id)
                        VALUES (?, 'show', ?, ?)
                    """, (shows_list_id, str(t_id), str(tmdb_id)))

                conn.commit()

            log(f"[LocalWatcher] Local lists updated: {len(local_movies)} movies, {len(local_shows)} TV shows.", level=LOGINFO)

        except Exception as e:
            log(f"[LocalWatcher] Error updating local lists in lists_cache.db: {e}", level=LOGERROR)
