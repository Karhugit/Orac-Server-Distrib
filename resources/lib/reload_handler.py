import os
from resources.lib.log_utils import log, LOGERROR
from resources.lib.db_init import init_static_movie_db, init_static_tvshows_db, init_dynamic_movie_db, init_dynamic_tvshows_db, init_lists_db

def clear_databases(movies_static_db_path, movies_dynamic_db_path, lists_db_path, tvshows_static_db_path, tvshows_dynamic_db_path):

    try:
        # Delete the databases
        if os.path.exists(movies_static_db_path):
            os.remove(movies_static_db_path)
        if os.path.exists(movies_dynamic_db_path):
            os.remove(movies_dynamic_db_path)
        if os.path.exists(lists_db_path):
            os.remove(lists_db_path)
        if os.path.exists(tvshows_static_db_path):
            os.remove(tvshows_static_db_path)
        if os.path.exists(tvshows_dynamic_db_path):
            os.remove(tvshows_dynamic_db_path)

        # Reinitialize the databases
        if not init_static_movie_db(movies_static_db_path):
            log(f"[Orac] Error initializing static movie DB", level=LOGERROR)
            return False
        if not init_static_tvshows_db(tvshows_static_db_path):
            log(f"[Orac] Error initializing static TV shows DB", level=LOGERROR)
            return False
        if not init_dynamic_movie_db(movies_dynamic_db_path):
            log(f"[Orac] Error initializing dynamic movie DB", level=LOGERROR)
            return False
        if not init_dynamic_tvshows_db(tvshows_dynamic_db_path):
            log(f"[Orac] Error initializing dynamic TV shows DB", level=LOGERROR)
            return False
        if not init_lists_db(lists_db_path):
            log(f"[Orac] Error initializing lists DB", level=LOGERROR)
            return False

        return True
    except Exception as e:
        log(f"[Orac] Error clearing databases: {e}", level=LOGERROR)
        return False