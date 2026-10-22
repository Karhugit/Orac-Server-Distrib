# -*- coding: utf-8 -*-
"""
AniList & MyAnimeList Client for Orac Server's Anime Dual-Indexing Bridge.

Provides:
- GraphQL queries for browsing native AniList menus (trending, popular, seasonal,
  airing, top_rated, movies, user lists, search, and single-item details).
- GraphQL mutations for scrobbling/updating episode watch progress on AniList.
- Optional MyAnimeList (MAL) v2 API progress update when a MAL token is configured.
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional
import requests

from resources.lib.log_utils import log, LOGINFO, LOGWARNING, LOGERROR, LOGDEBUG
from resources.lib.config_handler import get_config_value

ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"
MAL_API_BASE_URL = "https://api.myanimelist.net/v2"
DEFAULT_TIMEOUT = 15

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_description(desc: Optional[str]) -> str:
    if not desc:
        return ""
    cleaned = _HTML_TAG_RE.sub("", desc)
    return cleaned.replace("&quot;", '"').replace("&amp;", "&").replace("&#039;", "'").strip()


def _current_anime_season() -> tuple:
    now = datetime.utcnow()
    month = now.month
    year = now.year
    if month in (1, 2, 3):
        return "WINTER", year
    if month in (4, 5, 6):
        return "SPRING", year
    if month in (7, 8, 9):
        return "SUMMER", year
    return "FALL", year


def get_anilist_credentials(config_db_path: Optional[str]) -> Dict[str, str]:
    if not config_db_path:
        return {"token": "", "user": "", "user_id": "", "mal_token": ""}
    token = (
        get_config_value("anilist.token", config_db_path)
        or get_config_value("anilist_token", config_db_path)
        or ""
    )
    user = (
        get_config_value("anilist.user", config_db_path)
        or get_config_value("anilist_user", config_db_path)
        or ""
    )
    user_id = (
        get_config_value("anilist.user_id", config_db_path)
        or get_config_value("anilist_user_id", config_db_path)
        or ""
    )
    mal_token = (
        get_config_value("mal.token", config_db_path)
        or get_config_value("mal_token", config_db_path)
        or ""
    )
    for val_name, val in list(locals().items()):
        pass
    if token == "empty_setting":
        token = ""
    if user == "empty_setting":
        user = ""
    if user_id == "empty_setting":
        user_id = ""
    if mal_token == "empty_setting":
        mal_token = ""
    return {
        "token": token.strip(),
        "user": user.strip(),
        "user_id": str(user_id).strip(),
        "mal_token": mal_token.strip(),
    }


def _graphql_request(
    query: str,
    variables: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[Dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "OracServer/AnimeBridge",
    }
    if token and token not in ("empty_setting", ""):
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.post(
            ANILIST_GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code != 200:
            log(
                f"[AniList] GraphQL HTTP {resp.status_code}: {resp.text[:300]}",
                level=LOGWARNING,
            )
            return None
        payload = resp.json()
        if payload.get("errors"):
            log(f"[AniList] GraphQL errors: {payload['errors']}", level=LOGWARNING)
        return payload.get("data")
    except Exception as e:
        log(f"[AniList] GraphQL request error: {e}", level=LOGERROR)
        return None


def fetch_viewer_info(token: str) -> Optional[Dict[str, Any]]:
    """Validates an AniList OAuth token and returns the authenticated user's profile."""
    if not token or token == "empty_setting":
        return None
    query = """
    query {
      Viewer {
        id
        name
        avatar {
          large
        }
      }
    }
    """
    data = _graphql_request(query, token=token)
    if data and data.get("Viewer"):
        return data["Viewer"]
    return None


_MEDIA_FRAGMENT = """
fragment MediaFields on Media {
  id
  idMal
  title {
    romaji
    english
    native
    userPreferred
  }
  synonyms
  format
  status
  description(asHtml: false)
  startDate {
    year
    month
    day
  }
  season
  seasonYear
  episodes
  duration
  genres
  averageScore
  popularity
  isAdult
  coverImage {
    extraLarge
    large
    medium
    color
  }
  bannerImage
  nextAiringEpisode {
    airingAt
    timeUntilAiring
    episode
  }
}
"""


def _normalize_media_item(
    media: Dict[str, Any],
    anime_service=None,
    user_progress: Optional[int] = None,
    user_status: Optional[str] = None,
) -> Dict[str, Any]:
    if not media:
        return {}
    anilist_id = media.get("id")
    mal_id = media.get("idMal")
    title_obj = media.get("title") or {}
    romaji_title = title_obj.get("romaji") or ""
    english_title = title_obj.get("english") or ""
    native_title = title_obj.get("native") or ""
    display_title = english_title or romaji_title or title_obj.get("userPreferred") or native_title or f"Anime #{anilist_id}"

    start_date = media.get("startDate") or {}
    year = media.get("seasonYear") or start_date.get("year")
    cover = media.get("coverImage") or {}
    poster = cover.get("extraLarge") or cover.get("large") or cover.get("medium") or ""
    fanart = media.get("bannerImage") or poster
    fmt = media.get("format") or "TV"
    media_type = "movie" if fmt == "MOVIE" else "tvshow"

    item = {
        "anilist_id": anilist_id,
        "mal_id": mal_id,
        "title": display_title,
        "romaji_title": romaji_title,
        "english_title": english_title,
        "native_title": native_title,
        "synonyms": media.get("synonyms") or [],
        "format": fmt,
        "media_type": media_type,
        "status": media.get("status"),
        "episodes": media.get("episodes"),
        "duration": media.get("duration"),
        "season": media.get("season"),
        "season_year": media.get("seasonYear"),
        "year": year,
        "overview": _clean_description(media.get("description")),
        "genres": media.get("genres") or [],
        "rating": round((media.get("averageScore") or 0) / 10.0, 1) if media.get("averageScore") else None,
        "average_score": media.get("averageScore"),
        "popularity": media.get("popularity"),
        "poster_path": poster,
        "thumbnail_path": cover.get("large") or poster,
        "fanart_path": fanart,
        "landscape_path": fanart,
        "next_airing_episode": media.get("nextAiringEpisode"),
    }

    if user_progress is not None:
        item["user_progress"] = user_progress
    if user_status is not None:
        item["user_status"] = user_status

    # Enrich with Western cross-references (tmdb_id, tvdb_id, imdb_id) if anime_service is available
    if anime_service and anilist_id:
        try:
            aliases_info = anime_service.get_search_aliases({"anilist_id": anilist_id})
            ids = aliases_info.get("ids") or {}
            if ids.get("tmdb_id"):
                item["tmdb_id"] = ids["tmdb_id"]
            if ids.get("tvdb_id"):
                item["tvdb_id"] = ids["tvdb_id"]
            if ids.get("imdb_id"):
                item["imdb_id"] = ids["imdb_id"]
            if ids.get("anidb_id"):
                item["anidb_id"] = ids["anidb_id"]
            if ids.get("kitsu_id"):
                item["kitsu_id"] = ids["kitsu_id"]
        except Exception:
            pass

    return item


def fetch_anime_list(
    list_type: str = "trending",
    page: int = 1,
    per_page: int = 50,
    season: Optional[str] = None,
    season_year: Optional[int] = None,
    search: Optional[str] = None,
    genre: Optional[str] = None,
    username: Optional[str] = None,
    token: Optional[str] = None,
    anime_service=None,
) -> Dict[str, Any]:
    """
    Fetches an anime list from AniList and normalizes it for Kodi/Web clients.
    Supported list_type values:
      - 'trending', 'popular', 'top_rated', 'seasonal', 'airing', 'upcoming', 'movies', 'search'
      - 'user_watching', 'user_planning', 'user_completed', 'user_dropped', 'user_paused'
    """
    lt = (list_type or "trending").lower().strip()

    if lt.startswith("user_"):
        status_map = {
            "user_watching": "CURRENT",
            "user_current": "CURRENT",
            "user_planning": "PLANNING",
            "user_completed": "COMPLETED",
            "user_dropped": "DROPPED",
            "user_paused": "PAUSED",
        }
        al_status = status_map.get(lt)
        return fetch_user_anime_list(
            username=username,
            status=al_status,
            token=token,
            anime_service=anime_service,
        )

    variables: Dict[str, Any] = {
        "page": max(1, int(page)),
        "perPage": min(50, max(1, int(per_page))),
        "isAdult": False,
    }

    if search:
        variables["search"] = search.strip()
        variables["sort"] = ["SEARCH_MATCH", "POPULARITY_DESC"]
    elif lt == "trending":
        variables["sort"] = ["TRENDING_DESC", "POPULARITY_DESC"]
    elif lt == "popular":
        variables["sort"] = ["POPULARITY_DESC"]
    elif lt in ("top_rated", "score", "top"):
        variables["sort"] = ["SCORE_DESC", "POPULARITY_DESC"]
    elif lt == "seasonal":
        def_season, def_year = _current_anime_season()
        variables["season"] = (season or def_season).upper()
        variables["seasonYear"] = int(season_year or def_year)
        variables["sort"] = ["POPULARITY_DESC"]
    elif lt == "airing":
        variables["status"] = "RELEASING"
        variables["sort"] = ["POPULARITY_DESC"]
    elif lt == "upcoming":
        variables["status"] = "NOT_YET_RELEASED"
        variables["sort"] = ["POPULARITY_DESC"]
    elif lt == "movies":
        variables["format"] = "MOVIE"
        variables["sort"] = ["POPULARITY_DESC"]
    else:
        variables["sort"] = ["TRENDING_DESC", "POPULARITY_DESC"]

    if genre:
        variables["genre"] = genre

    query = (
        _MEDIA_FRAGMENT
        + """
    query (
      $page: Int,
      $perPage: Int,
      $sort: [MediaSort],
      $season: MediaSeason,
      $seasonYear: Int,
      $status: MediaStatus,
      $format: MediaFormat,
      $search: String,
      $genre: String,
      $isAdult: Boolean
    ) {
      Page(page: $page, perPage: $perPage) {
        pageInfo {
          total
          perPage
          currentPage
          lastPage
          hasNextPage
        }
        media(
          type: ANIME,
          sort: $sort,
          season: $season,
          seasonYear: $seasonYear,
          status: $status,
          format: $format,
          search: $search,
          genre: $genre,
          isAdult: $isAdult
        ) {
          ...MediaFields
        }
      }
    }
    """
    )

    data = _graphql_request(query, variables=variables, token=token)
    if not data or not data.get("Page"):
        return {"success": False, "results": [], "page_info": {}}

    page_obj = data["Page"]
    media_list = page_obj.get("media") or []
    results = [_normalize_media_item(m, anime_service=anime_service) for m in media_list if m]

    return {
        "success": True,
        "list_type": lt,
        "count": len(results),
        "page_info": page_obj.get("pageInfo") or {},
        "results": results,
    }


def fetch_user_anime_list(
    username: Optional[str] = None,
    status: Optional[str] = None,
    token: Optional[str] = None,
    anime_service=None,
) -> Dict[str, Any]:
    """Fetches a user's AniList MediaListCollection (e.g., CURRENT, PLANNING, COMPLETED)."""
    if not username and token:
        viewer = fetch_viewer_info(token)
        if viewer:
            username = viewer.get("name")

    if not username:
        return {"success": False, "error": "AniList username or token required", "results": []}

    variables: Dict[str, Any] = {"userName": username}
    if status:
        variables["status"] = status.upper()

    query = (
        _MEDIA_FRAGMENT
        + """
    query ($userName: String, $status: MediaListStatus) {
      MediaListCollection(userName: $userName, type: ANIME, status: $status) {
        lists {
          name
          status
          entries {
            id
            status
            score
            progress
            updatedAt
            media {
              ...MediaFields
            }
          }
        }
      }
    }
    """
    )

    data = _graphql_request(query, variables=variables, token=token)
    if not data or not data.get("MediaListCollection"):
        return {"success": False, "results": []}

    lists = data["MediaListCollection"].get("lists") or []
    results = []
    seen_ids = set()
    for lst in lists:
        for entry in lst.get("entries") or []:
            media = entry.get("media")
            if not media or media.get("id") in seen_ids:
                continue
            seen_ids.add(media["id"])
            item = _normalize_media_item(
                media,
                anime_service=anime_service,
                user_progress=entry.get("progress"),
                user_status=entry.get("status"),
            )
            item["user_score"] = entry.get("score")
            item["updated_at"] = entry.get("updatedAt")
            results.append(item)

    return {
        "success": True,
        "username": username,
        "status": status,
        "count": len(results),
        "results": results,
    }


def fetch_anime_details(
    anilist_id: int,
    token: Optional[str] = None,
    anime_service=None,
) -> Optional[Dict[str, Any]]:
    """Fetches detailed metadata, relations, and episode list for a single AniList ID."""
    query = (
        _MEDIA_FRAGMENT
        + """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        ...MediaFields
        studios(isMain: true) {
          nodes {
            id
            name
          }
        }
        relations {
          edges {
            relationType(version: 2)
            node {
              id
              idMal
              type
              format
              title {
                romaji
                english
                native
              }
              episodes
              status
              seasonYear
              coverImage {
                large
              }
            }
          }
        }
        streamingEpisodes {
          title
          thumbnail
          url
          site
        }
        mediaListEntry {
          id
          status
          progress
          score
        }
      }
    }
    """
    )

    data = _graphql_request(query, variables={"id": int(anilist_id)}, token=token)
    if not data or not data.get("Media"):
        return None

    media = data["Media"]
    entry = media.get("mediaListEntry") or {}
    normalized = _normalize_media_item(
        media,
        anime_service=anime_service,
        user_progress=entry.get("progress"),
        user_status=entry.get("status"),
    )

    studios = [
        n.get("name")
        for n in (media.get("studios", {}).get("nodes") or [])
        if n and n.get("name")
    ]
    normalized["studios"] = studios
    normalized["streaming_episodes"] = media.get("streamingEpisodes") or []

    relations = []
    for edge in (media.get("relations", {}).get("edges") or []):
        node = edge.get("node") or {}
        if node.get("type") != "ANIME":
            continue
        t_obj = node.get("title") or {}
        relations.append({
            "relation_type": edge.get("relationType"),
            "anilist_id": node.get("id"),
            "mal_id": node.get("idMal"),
            "format": node.get("format"),
            "title": t_obj.get("english") or t_obj.get("romaji") or t_obj.get("native"),
            "romaji_title": t_obj.get("romaji"),
            "episodes": node.get("episodes"),
            "status": node.get("status"),
            "year": node.get("seasonYear"),
            "poster_path": (node.get("coverImage") or {}).get("large"),
        })
    normalized["relations"] = relations

    # Build synthetic episode list (1..N) enriched with Western coordinate mapping
    total_eps = normalized.get("episodes")
    next_ep_info = normalized.get("next_airing_episode") or {}
    if not total_eps and next_ep_info.get("episode"):
        total_eps = max(1, int(next_ep_info["episode"]) - 1)

    if anime_service:
        aliases_data = anime_service.get_search_aliases({"anilist_id": int(anilist_id)})
        normalized["aliases_info"] = aliases_data

    episode_items = []
    stream_eps = normalized["streaming_episodes"]
    max_ep_count = min(int(total_eps or 0), 2000)
    user_prog = int(entry.get("progress") or 0)

    for ep_num in range(1, max_ep_count + 1):
        ep_title = f"Episode {ep_num}"
        ep_thumb = normalized.get("fanart_path") or normalized.get("poster_path")
        if ep_num <= len(stream_eps):
            se = stream_eps[ep_num - 1] or {}
            if se.get("title"):
                ep_title = se["title"]
            if se.get("thumbnail"):
                ep_thumb = se["thumbnail"]

        ep_entry: Dict[str, Any] = {
            "absolute_episode": ep_num,
            "episode": ep_num,
            "title": ep_title,
            "thumbnail_path": ep_thumb,
            "watched": ep_num <= user_prog,
        }
        if anime_service:
            res = anime_service.resolve_anime_to_western(int(anilist_id), ep_num)
            if res.get("success"):
                ep_entry["tmdb_id"] = res.get("tmdb_id")
                ep_entry["tvdb_id"] = res.get("tvdb_id")
                ep_entry["western_season"] = res.get("season")
                ep_entry["western_episode"] = res.get("episode")
                ep_entry["mapping_source"] = res.get("mapping_source")
        episode_items.append(ep_entry)

    normalized["episode_list"] = episode_items
    return normalized


def scrobble_mal_episode(
    mal_id: int,
    episode_num: int,
    mal_token: str,
    is_completed: bool = False,
) -> bool:
    """Updates episode watch progress on MyAnimeList v2 API if a MAL token is configured."""
    if not mal_id or not mal_token or mal_token == "empty_setting":
        return False
    try:
        url = f"{MAL_API_BASE_URL}/anime/{int(mal_id)}/my_list_status"
        headers = {
            "Authorization": f"Bearer {mal_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "num_watched_episodes": int(episode_num),
            "status": "completed" if is_completed else "watching",
        }
        resp = requests.patch(url, headers=headers, data=data, timeout=DEFAULT_TIMEOUT)
        if resp.status_code == 200:
            log(f"[MAL] Scrobbled mal_id={mal_id} progress={episode_num}", level=LOGINFO)
            return True
        log(f"[MAL] Failed to scrobble mal_id={mal_id}: HTTP {resp.status_code}", level=LOGWARNING)
    except Exception as e:
        log(f"[MAL] Error scrobbling mal_id={mal_id}: {e}", level=LOGERROR)
    return False


def scrobble_anilist_episode(
    anilist_id: int,
    episode_num: int,
    config_db_path: Optional[str] = None,
    token: Optional[str] = None,
    mal_id: Optional[int] = None,
    total_episodes: Optional[int] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Updates watch progress on AniList (and optionally MAL) for `anilist_id` to `episode_num`.
    """
    creds = get_anilist_credentials(config_db_path) if config_db_path else {}
    effective_token = token or creds.get("token") or ""
    mal_token = creds.get("mal_token") or ""

    if not effective_token:
        log(
            f"[AniList] Skipping remote AniList scrobble for anilist:{anilist_id} ep {episode_num} (no AniList token configured).",
            level=LOGDEBUG,
        )
        return {"success": False, "reason": "no_token", "anilist_id": anilist_id, "episode": episode_num}

    ep_int = max(1, int(episode_num))

    # Query current progress and total episodes on AniList first so we don't regress progress
    check_query = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        id
        idMal
        episodes
        mediaListEntry {
          id
          status
          progress
        }
      }
    }
    """
    media_data = _graphql_request(check_query, variables={"id": int(anilist_id)}, token=effective_token)
    current_progress = 0
    current_status = None
    if media_data and media_data.get("Media"):
        m = media_data["Media"]
        if not total_episodes and m.get("episodes"):
            total_episodes = int(m["episodes"])
        if not mal_id and m.get("idMal"):
            mal_id = int(m["idMal"])
        entry = m.get("mediaListEntry")
        if entry:
            current_progress = int(entry.get("progress") or 0)
            current_status = entry.get("status")

    if not force and current_progress >= ep_int and current_status in ("CURRENT", "COMPLETED", "REPEATING"):
        log(
            f"[AniList] anilist:{anilist_id} already at progress {current_progress} (>= {ep_int}), skipping duplicate mutation.",
            level=LOGDEBUG,
        )
        return {
            "success": True,
            "skipped": True,
            "anilist_id": int(anilist_id),
            "progress": current_progress,
            "status": current_status,
        }

    is_completed = bool(total_episodes and ep_int >= int(total_episodes))
    target_status = "COMPLETED" if is_completed else "CURRENT"

    mutation = """
    mutation ($mediaId: Int, $progress: Int, $status: MediaListStatus) {
      SaveMediaListEntry(mediaId: $mediaId, progress: $progress, status: $status) {
        id
        mediaId
        status
        progress
        updatedAt
      }
    }
    """
    mut_vars = {
        "mediaId": int(anilist_id),
        "progress": ep_int,
        "status": target_status,
    }
    res_data = _graphql_request(mutation, variables=mut_vars, token=effective_token)
    saved = (res_data or {}).get("SaveMediaListEntry")

    mal_scrobbled = False
    if mal_token and mal_id:
        mal_scrobbled = scrobble_mal_episode(int(mal_id), ep_int, mal_token, is_completed=is_completed)

    if saved:
        log(
            f"[AniList] Scrobbled anilist:{anilist_id} -> progress={saved.get('progress')} status={saved.get('status')}",
            level=LOGINFO,
        )
        return {
            "success": True,
            "anilist_id": int(anilist_id),
            "progress": saved.get("progress"),
            "status": saved.get("status"),
            "mal_scrobbled": mal_scrobbled,
        }

    return {
        "success": False,
        "reason": "mutation_failed",
        "anilist_id": int(anilist_id),
        "episode": ep_int,
        "mal_scrobbled": mal_scrobbled,
    }
