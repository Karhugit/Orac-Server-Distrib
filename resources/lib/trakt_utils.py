from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING

PAGE_SIZE = 250  # June 2026 Trakt hard limit per paginated response

async def get_trakt_watchlist(trakt_handler):
    try:
        all_items = []
        page = 1
        while True:
            watchlist_resp = await trakt_handler.get(
                f"/users/me/watchlist?extended=full&limit={PAGE_SIZE}&page={page}"
            )
            if watchlist_resp is None:
                log(f"[Orac] No response received when fetching watchlist page {page}", level=LOGERROR)
                break
            if watchlist_resp.status_code != 200:
                log(f"[Orac] Failed to fetch watchlist: {watchlist_resp.status_code}", level=LOGWARNING)
                break
            page_items = watchlist_resp.json()
            all_items.extend(page_items)
            total_pages = int(watchlist_resp.headers.get("X-Pagination-Page-Count", 1))
            if page >= total_pages:
                break
            page += 1
        return all_items if all_items else None
    except Exception as e:
        log(f"[Orac] Error fetching Trakt watchlist: {e}", level=LOGERROR)
        return None
    
async def get_trakt_favorites(trakt_handler):
    try:
        all_items = []
        page = 1
        while True:
            favorites_resp = await trakt_handler.get(
                f"/users/me/favorites?extended=full&limit={PAGE_SIZE}&page={page}"
            )
            if favorites_resp is None:
                log(f"[Orac] No response received when fetching favorites page {page}", level=LOGERROR)
                break
            if favorites_resp.status_code != 200:
                log(f"[Orac] Failed to fetch favorites: {favorites_resp.status_code}", level=LOGWARNING)
                break
            page_items = favorites_resp.json()
            all_items.extend(page_items)
            total_pages = int(favorites_resp.headers.get("X-Pagination-Page-Count", 1))
            if page >= total_pages:
                break
            page += 1
        return all_items if all_items else None
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
