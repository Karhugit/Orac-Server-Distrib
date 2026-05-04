import xbmc
import xbmcgui
import xbmcaddon
import sys
import xbmcvfs  # Import xbmcvfs for file operations    
import os
import sqlite3  # Import sqlite3 for database operations
from resources.lib.trakt_handler import TraktAuth  # Import your TraktAuth class
from resources.lib.http_server import start_http_server  # Import your HTTP server function
import time
from resources.lib.db_init import init_static_movie_db, init_dynamic_movie_db, init_lists_db  # Import your database initialization functions
from resources.lib.sync_trakt_with_db import trakt_list_sync_task  # Import your sync function
from resources.lib.tmdb_handler import TMDbAPI  # Import TMDb handler
from resources.lib.log_utils import log, LOGERROR


class OracService:
    def __init__(self):
        self.addon = xbmcaddon.Addon("service.orac")
        config_path = xbmcvfs.translatePath("special://profile/addon_data/service.orac/")
        xbmcvfs.mkdirs(config_path)
        config_db_path = os.path.join(config_path, "config.db")
        self.trakt_handler = TraktAuth(
            addon=self.addon,
            config_db_path=config_db_path,
            client_id="378e7c8adf3569e809b57a26e318dee3d4080e3c58dafa817537f6b7d6662cd6",  # Replace with your actual Trakt client ID
            client_secret="e454afd65b734faea58be818af256bb05e88e6151404df987d5716025dbc0b29",  # Replace with your actual Trakt client secret
        )
        self._parse_argv()
        self.initialised = False

        if not self.initialised:
            self._init_service()

    def _init_service(self):
        # First lets set up the database paths
        cache_path = xbmcvfs.translatePath("special://profile/addon_data/service.orac/cache/")
        xbmcvfs.mkdirs(cache_path)  # Ensures the directory exists
        self.movie_static_db_path = os.path.join(cache_path, "movies_static_cache.db")
        self.movie_dynamic_db_path = os.path.join(cache_path, "movies_dynamic_cache.db")
        self.lists_db_path = os.path.join(cache_path, "lists_cache.db")
        self.http_server = start_http_server(trakt_handler=self.trakt_handler, port=5555, static_db_path=self.movie_static_db_path, dynamic_db_path=self.movie_dynamic_db_path)  # Start the HTTP server on port 5555
        self.initialised = True
        self.last_refresh_check = 0 # Initialize last refresh check time

        # Initialize the SQLite database
        if not init_static_movie_db(self.movie_static_db_path):
            log("[Orac] Failed to initialize static movie cache database", level=LOGERROR)
            return
        if not init_dynamic_movie_db(self.movie_dynamic_db_path):
            log("[Orac] Failed to initialize dynamic movie cache database", level=LOGERROR)
            return
        if not init_lists_db(self.lists_db_path):
            log("[Orac] Failed to initialize lists cache database", level=LOGERROR)
            return

        # Verify the trakt token
#        if not self.trakt_handler.verify_token():
#            log("[Orac] Trakt token verification failed, exiting", level=LOGERROR)
#            return


        # Cache the list if authenticated
        if self.trakt_handler.get_saved_tokens():
            tmdb_handler = TMDbAPI(api_key="b8f106f33261688001712a149f6f6990") # Use a default or config key
            
            async def run_sync():
                await trakt_list_sync_task(
                    trakt_auth=self.trakt_handler,
                    tmdb_handler=tmdb_handler,
                    lists_db_path=self.lists_db_path,
                    movie_static_db_path=self.movie_static_db_path,
                    movie_dynamic_db_path=self.movie_dynamic_db_path,
                    tvshows_static_db_path=self.movie_static_db_path.replace("movies", "tvshows"), # Assuming naming convention
                    tvshows_dynamic_db_path=self.movie_dynamic_db_path.replace("movies", "tvshows"),
                    trakt_queue_path=os.path.join(os.path.dirname(self.lists_db_path), "trakt_update_queue.db"),
                    username=self.trakt_handler.get_username()
                )
            
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_sync())
            loop.close()

    def _parse_argv(self):
        # Check if the script was called with arguments (like from the settings button)
        args = sys.argv
        if len(args) > 1:
            if args[1] == "auth_trakt":
                self._handle_auth_trakt()

    def _handle_auth_trakt(self):
        """Run Trakt authentication when the button is pressed"""
        if self.trakt_handler.authenticate():  # Returns True if successful
            xbmcgui.Dialog().notification(
                "Trakt Authorization",
                "Successfully authorized!",
                xbmcgui.NOTIFICATION_INFO,
                3000
            )
        else:
            xbmcgui.Dialog().notification(
                "Trakt Authorization",
                "Failed to authorize.",
                xbmcgui.NOTIFICATION_ERROR,
                3000
            )

    def run(self):
        if not self.initialised:    
            log("[Orac] Service not initialized, exiting", level=LOGERROR)
            return

        monitor = xbmc.Monitor()
        log("[Orac] Service is running", level=LOGINFO)
        try:
            while not monitor.abortRequested():
                tokens = self.trakt_handler.get_saved_tokens()
                if tokens:
                    now = time.time()

                    # Check if it's time to refresh the token
                    if now - self.last_refresh_check > 3600:
                        expires_at = tokens["created_at"] + tokens["expires_in"]

                        # Refresh if less than 23 hours remain
                        if now >= (expires_at - 82800):
                            success = self.trakt_handler.refresh_token()
                            log("[Orac] Proactively refreshed token", level=LOGINFO if success else LOGWARNING)
                        
                        self.last_refresh_check = now

                monitor.waitForAbort(60)
        finally:
            self.http_server.shutdown()
            log("[Orac] Service is shutting down", level=LOGINFO)

if __name__ == "__main__":
    OracService().run()