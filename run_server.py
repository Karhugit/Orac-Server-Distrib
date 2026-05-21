from pathlib import Path
import sys
import os
import importlib
import time

# Force Python to import xbmc from "custom_modules" subdirectory
script_dir = os.path.dirname(os.path.abspath(__file__))  # Get current script directory
xbmc_path = os.path.join(script_dir, "stubs")   # Define path to the subdirectory

# Insert at the beginning of sys.path
sys.path.insert(0, xbmc_path)

from resources.lib.config_loader import ConfigLoader
# Initialize Config
config_loader = ConfigLoader()
config = config_loader.config

# Set Environment Variables
# These must be set BEFORE importing log_utils or other modules that read os.environ at module level
os.environ["ORAC_ENV"] = config.get("ORAC_ENV")
os.environ["ORAC_LOG_FILE"] = config.get("ORAC_LOG_FILE")
os.environ["ORAC_LOG_PATH"] = config.get("ORAC_LOG_PATH")

from resources.lib.trakt_handler import TraktAuth
from resources.lib.tmdb_handler import TMDbAPI
from resources.scrapers.scraper_manager import ScraperManager
from resources.lib.http_server import start_http_server
from resources.lib.db_init import (
    init_static_movie_db, init_dynamic_movie_db, init_lists_db,
    init_static_tvshows_db, init_dynamic_tvshows_db, init_update_queue,
    init_config_db, init_ext_indexes_db, init_tags_db, init_undesirables_db,
    init_trakt_history_sync_db
)
from resources.lib.config_handler import get_config_value
from resources.lib.log_utils import log, LOGERROR, LOGINFO

# Database Paths
db_paths = config_loader.db_paths
movies_static_db_path = db_paths["movies_static"]
movies_dynamic_db_path = db_paths["movies_dynamic"]
lists_db_path = db_paths["lists"]
tvshows_static_db_path = db_paths["tvshows_static"]
tvshows_dynamic_db_path = db_paths["tvshows_dynamic"]
trakt_update_queue_path = db_paths["trakt_update_queue"]
config_db_path = db_paths["config"]
ext_indexes_db_path = db_paths["ext_indexes"]
tags_db_path = db_paths["tags"]
undesirables_db_path = db_paths["undesirables"]
trakt_history_sync_db_path = db_paths.get("trakt_history_sync", "trakt_history_sync.db")

# Initialize Database Manager
from resources.lib.database_manager import DatabaseManager
db_manager = DatabaseManager()
db_manager.configure(db_paths)

# Initialize SQLite databases
init_success = True

if not init_static_movie_db(movies_static_db_path):
    log("[Orac] Failed to initialize static movie cache database", level=LOGERROR)
    init_success = False
if not init_dynamic_movie_db(movies_dynamic_db_path):
    log("[Orac] Failed to initialize dynamic moviecache database", level=LOGERROR)
    init_success = False
if not init_static_tvshows_db(tvshows_static_db_path):
    log("[Orac] Failed to initialize static tvshows cache database", level=LOGERROR)
    init_success = False
if not init_dynamic_tvshows_db(tvshows_dynamic_db_path):
    log("[Orac] Failed to initialize dynamic tvshows cache database", level=LOGERROR)
    init_success = False
if not init_lists_db(lists_db_path):
    log("[Orac] Failed to initialize lists cache database", level=LOGERROR)
    init_success = False
if not init_update_queue(trakt_update_queue_path):
    log("[Orac] Failed to initialize update queue database", level=LOGERROR)
    init_success = False
if not init_config_db(config_db_path):
    log("[Orac] Failed to initialize config database", level=LOGERROR)
    init_success = False
if not init_ext_indexes_db(ext_indexes_db_path):
    log("[Orac] Failed to initialize external indexes database", level=LOGERROR)
    init_success = False
if not init_tags_db(tags_db_path):
    log("[Orac] Failed to initialize tags database", level=LOGERROR)
    init_success = False
if not init_undesirables_db(undesirables_db_path):
    log("[Orac] Failed to initialize undesirables database", level=LOGERROR)
    init_success = False
if not init_trakt_history_sync_db(trakt_history_sync_db_path):
    log("[Orac] Failed to initialize trakt_history_sync database", level=LOGERROR)
    init_success = False

if not init_success:
    log("[Orac] Critical database initialization failed. Exiting.", level=LOGERROR)
    sys.exit(1)

addon = "service.orac"

# Initialize Handlers
trakt_handler = TraktAuth(
    client_id=config_loader.trakt_config.get("client_id"),
    client_secret=config_loader.trakt_config.get("client_secret"),
    addon=addon,
    config_db_path=config_db_path
)

stored_tmdb_key = get_config_value("tmdb_api_key", config_db_path)
tmdb_api_key = stored_tmdb_key if stored_tmdb_key else config_loader.tmdb_config.get("api_key")

tmdb_handler = TMDbAPI(
    api_key=tmdb_api_key,
    static_db_path=tvshows_static_db_path
)

scraper_manager = ScraperManager(scrapers_dir="resources/scrapers")

# Start Server
start_http_server(
    trakt_handler=trakt_handler,
    tmdb_handler=tmdb_handler,
    port=config_loader.server_config.get("port"),
    movies_static_db_path=movies_static_db_path,
    movies_dynamic_db_path=movies_dynamic_db_path,
    lists_db_path=lists_db_path,
    tvshows_static_db_path=tvshows_static_db_path,
    tvshows_dynamic_db_path=tvshows_dynamic_db_path,
    trakt_update_queue_path=trakt_update_queue_path,
    config_db_path=config_db_path,
    ext_indexes_db_path=ext_indexes_db_path,
    tags_db_path=tags_db_path,
    scrapers_dir='resources/scrapers',
    db_manager=db_manager,
    trakt_history_sync_db_path=trakt_history_sync_db_path
)

while True:
    time.sleep(1)
