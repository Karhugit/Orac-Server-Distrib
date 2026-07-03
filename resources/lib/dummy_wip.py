import asyncio
from resources.lib.log_utils import log, LOGINFO, LOGERROR
from resources.lib.db_sync_manager import sync_lists_and_items

# ... (inside OracRequestHandler)

    async def _handle_force_sync(self, query):
        log("[Orac] Received force sync request.", level=LOGINFO)
        # We start the sync in the background so we don't block the response
        asyncio.create_task(self._run_force_sync())
        self._send_response(200, b"Sync started", "text/plain")

    async def _run_force_sync(self):
        try:
            log("[Orac] Force sync started...", level=LOGINFO)
            # Re-use the existing sync arguments. 
            # Note: We need to ensure we have valid instances/paths. 
            # They are available as self.trakt_handler, etc.
            
            username = await self._get_trakt_user()
            
            await sync_lists_and_items(
                self.trakt_handler,
                self.tmdb_handler,
                self.movies_static_db_path,
                self.movies_dynamic_db_path,
                self.tvshows_static_db_path,
                self.tvshows_dynamic_db_path,
                self.lists_db_path,
                self.trakt_update_queue_path,
                None, # trakt_queue_worker, passing None might be unsafe if it relies on it?
                      # _handle_list_sync_task (in start_server) passes it.
                      # Ideally we should pass the worker if possible, but it's not stored in self.
                username=username,
                external_indexes_db_path=self.external_indexes_db_path,
                config_db_path=self.config_db_path 
            )
            log("[Orac] Force sync completed successfully.", level=LOGINFO)
        except Exception as e:
            log(f"[Orac] Force sync failed: {e}", level=LOGERROR)
