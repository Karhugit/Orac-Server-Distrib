from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING


async def get_trakt_watchlist(trakt_handler):
    try:
#        log("[Orac] Fetching Trakt watchlist...", level=LOGINFO)
        watchlist_resp = await trakt_handler.get("/users/me/watchlist?extended=full")
        if watchlist_resp is None:
            log(f"[Orac] No response received when fetching watchlist", level=LOGERROR)
            return None
        if watchlist_resp.status_code != 200:
            log(f"[Orac] Failed to fetch watchlist: {watchlist_resp.status_code}", level=LOGWARNING)
            return None
        return watchlist_resp.json()
    except Exception as e:
        log(f"[Orac] Error fetching Trakt watchlist: {e}", level=LOGERROR)
        return None
    
async def get_trakt_favorites(trakt_handler):
    try:
#        log("[Orac] Fetching Trakt favorites...", level=LOGINFO)
        favorites_resp = await trakt_handler.get("/users/me/favorites?extended=full")
        if favorites_resp is None:
            log(f"[Orac] No response received when fetching favorites", level=LOGERROR)
            return None
        if favorites_resp.status_code != 200:
            log(f"[Orac] Failed to fetch favorites: {favorites_resp.status_code}", level=LOGWARNING)
            return None
        return favorites_resp.json()
    except Exception as e:
        log(f"[Orac] Error fetching Trakt favorites: {e}", level=LOGERROR)
        return None
    
def unlike_trakt_list(trakt_handler, trakt_user, list_name, slug):
    try:
        log(f"[Orac] Unliking Trakt list '{list_name}' for user '{trakt_user}'", level=LOGINFO)
        endpoint = f"/users/{trakt_user}/lists/{slug}/like"
        resp = trakt_handler.delete(endpoint)
        if resp is None:
            log(f"[Orac] No response received when unliking list '{list_name}'", level=LOGERROR)
            return False
        if resp.status_code == 204:
            log(f"[Orac] Successfully unliked Trakt list '{list_name}'", level=LOGINFO)
            return True
        else:
            log(f"[Orac] Failed to unlike Trakt list '{list_name}': {resp.status_code}", level=LOGWARNING)
            return False
    except Exception as e:
        log(f"[Orac] Error unliking Trakt list '{list_name}': {e}", level=LOGERROR)
        return False
