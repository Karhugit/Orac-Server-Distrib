# -*- coding: utf-8 -*-
"""
anime_service.py
----------------
Two-Layer Anime Dual-Indexing Bridge for Orac Server:
  - Layer 1: Global Identity & Alias Ingestion (Manami Project: anime-offline-database.jsonl)
  - Layer 2: Episode Coordinate Translation (AniBridge Mappings: anibridge/anibridge-mappings)
  - Custom Overrides Engine (local JSON/YAML file + SQLite anime_custom_overrides table)
  - AnimeCoordinateService:
      * resolve_anime_to_western(anilist_id, episode_num)
      * resolve_western_to_anime(tmdb_id, season, episode)
      * get_search_aliases(media_identifier)
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

from resources.lib.db_init import init_anime_db
from resources.lib.db_utils import db_connect
from resources.lib.log_utils import log, LOGDEBUG, LOGERROR, LOGINFO, LOGWARNING

MANAMI_JSONL_URL = (
    "https://github.com/manami-project/anime-offline-database/releases/latest/download/anime-offline-database.jsonl"
)
ANIBRIDGE_MAPPINGS_URL = (
    "https://github.com/anibridge/anibridge-mappings/releases/latest/download/mappings.min.json"
)
ANIBRIDGE_FALLBACK_URL = (
    "https://github.com/anibridge/anibridge-mappings/releases/download/v3/mappings.min.json"
)

_ingestion_lock = threading.Lock()

# Regex patterns for extracting provider IDs from Manami `sources` URLs
_SOURCE_PATTERNS = [
    ("anilist_id", re.compile(r"anilist\.co/anime/(\d+)", re.I)),
    ("mal_id", re.compile(r"myanimelist\.net/anime/(\d+)", re.I)),
    ("anidb_id", re.compile(r"anidb\.net/(?:anime/|perl-bin/animedb\.pl\?show=anime&aid=)(\d+)", re.I)),
    ("kitsu_id", re.compile(r"kitsu\.(?:app|io)/anime/(\d+)", re.I)),
    ("simkl_id", re.compile(r"simkl\.com/anime/(\d+)", re.I)),
    ("tmdb_movie_id", re.compile(r"themoviedb\.org/movie/(\d+)", re.I)),
    ("tmdb_tv_id", re.compile(r"themoviedb\.org/tv/(\d+)", re.I)),
    ("tvdb_id", re.compile(r"thetvdb\.com/(?:series/|movies/|.*id=)(\d+)", re.I)),
    ("imdb_id", re.compile(r"imdb\.com/title/(tt\d+)", re.I)),
]

# Detect Japanese characters (Hiragana, Katakana, CJK Unified Ideographs)
_CJK_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u3400-\u4DBF]")

# Common English words / indicators to distinguish English titles from Romaji
_ENGLISH_HINTS_RE = re.compile(
    r"\b(the|of|in|on|at|to|for|with|from|by|and|or|is|are|was|were|"
    r"my|your|his|her|our|their|season|part|movie|special|ova|episode|"
    r"chronicles|adventures|legend|story|world|princess|prince|knight|"
    r"magic|school|high|academy|girl|boy|love|war|dragon|sword|shield|"
    r"hero|demon|king|queen|reincarnated|another|level|life|death|beyond)\b",
    re.I,
)

# Common Japanese Romaji particles & tokens
_ROMAJI_HINTS_RE = re.compile(
    r"\b(no|wa|ga|wo|ni|de|to|kara|made|yori|tachi|chan|kun|san|sama|"
    r"senpai|sensei|ouji|hime|mahou|gakuen|isekai|tensei|yuusha|maou|"
    r"monogatari|densetsu|bouken|shoujo|shounen|kanojo|ore|boku|watashi)\b",
    re.I,
)


def normalize_search_title(title: str) -> str:
    """
    Normalizes a title string for scraper query generation and indexed lookup.
    Strips punctuation, normalizes whitespace, and converts to lowercase.
    """
    if not title:
        return ""
    cleaned = re.sub(r"[^\w\s\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", " ", str(title))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return cleaned


def clean_scraper_query_title(title: str) -> str:
    """
    Produces a clean scraper-friendly title (preserving capitalization/numbers,
    removing special brackets/symbols that break torrent indexers).
    """
    if not title:
        return ""
    cleaned = re.sub(r"[\"'`“”‘’«»「」『』【】\[\]()（）]", " ", str(title))
    cleaned = re.sub(r"[:;/\\|~*?!@#$%^&+=<>]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def classify_title_variant(title: str, is_primary: bool = False) -> str:
    """
    Classifies an anime title variant into 'primary', 'native', 'romaji', 'english', or 'synonym'.
    """
    if not title:
        return "synonym"
    if _CJK_RE.search(title):
        return "native"
    if is_primary:
        return "romaji"
    has_romaji = bool(_ROMAJI_HINTS_RE.search(title))
    has_english = bool(_ENGLISH_HINTS_RE.search(title))
    if has_romaji and not has_english:
        return "romaji"
    if has_english and not has_romaji:
        return "english"
    return "synonym"


def parse_manami_sources(sources: List[str]) -> Dict[str, Any]:
    """
    Extracts cross-referenced database IDs from a Manami entry's `sources` URL list.
    """
    extracted: Dict[str, Any] = {}
    for url in sources or []:
        if not isinstance(url, str):
            continue
        for key, pattern in _SOURCE_PATTERNS:
            if key in extracted:
                continue
            m = pattern.search(url)
            if m:
                val = m.group(1)
                if key == "imdb_id":
                    extracted[key] = val
                else:
                    try:
                        extracted[key] = int(val)
                    except ValueError:
                        pass
    return extracted


def parse_descriptor(descriptor: str) -> Tuple[str, str, str]:
    """
    Parses an AniBridge v3 descriptor `provider:id[:scope]` into `(provider, id, scope)`.
    Examples:
      - 'anilist:1535'       -> ('anilist', '1535', '')
      - 'tmdb_show:26209:s1' -> ('tmdb_show', '26209', 's1')
      - 'tmdb_movie:128'     -> ('tmdb_movie', '128', '')
      - 'anidb:1:R'          -> ('anidb', '1', 'R')
    """
    parts = str(descriptor or "").strip().split(":")
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip(), ""
    if len(parts) >= 3:
        return parts[0].strip(), parts[1].strip(), ":".join(parts[2:]).strip()
    return str(descriptor or "").strip(), "", ""


def parse_season_scope(scope: str) -> Optional[int]:
    """
    Extracts the integer season number from a scope like 's1', 's0', 's02', or '1'.
    Returns None if the scope does not represent a season.
    """
    if scope is None:
        return None
    s = str(scope).strip().lower()
    if not s:
        return None
    if s.startswith("s") and s[1:].isdigit():
        return int(s[1:])
    if s.isdigit():
        return int(s)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Range Expression Evaluation Engine (AniBridge v3 & v2)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_single_range_token(token: str) -> Optional[Tuple[int, Optional[int]]]:
    """
    Parses a range token like '1-12', 'e1-e12', '5', '14-' into `(start, end)`.
    `end` is None if the range is open-ended (e.g. '14-').
    """
    t = str(token or "").strip().lower()
    if not t:
        return None
    t = re.sub(r"^e", "", t)
    if "-" in t:
        left, right = t.split("-", 1)
        left = re.sub(r"^e", "", left.strip())
        right = re.sub(r"^e", "", right.strip())
        if not left.isdigit():
            return None
        start = int(left)
        if right == "":
            return (start, None)
        if right.isdigit():
            return (start, int(right))
        return None
    if t.isdigit():
        val = int(t)
        return (val, val)
    return None


def _expand_target_segments(target_expr: str, max_len: int = 2000) -> Tuple[List[int], Optional[int], int]:
    """
    Parses a target expression of the form:
      `x[-y][,x2[-y2]...][|ratio]` or legacy `e1-e12:2`
    Returns:
      `(explicit_episodes_list, open_ended_start, ratio)`
      - `ratio` defaults to 1. Positive ratio `n` means 1 source ep -> `n` target eps.
        Negative ratio `-n` means `n` source eps -> 1 target ep.
    """
    raw = str(target_expr or "").strip()
    ratio = 1

    # Check for '|ratio' (v3) or ':ratio' (v2)
    if "|" in raw:
        raw, ratio_str = raw.rsplit("|", 1)
        try:
            ratio = int(ratio_str.strip())
            if ratio == 0:
                ratio = 1
        except ValueError:
            ratio = 1
    elif ":" in raw:
        raw, ratio_str = raw.rsplit(":", 1)
        try:
            ratio = int(ratio_str.strip())
            if ratio == 0:
                ratio = 1
        except ValueError:
            ratio = 1

    episodes: List[int] = []
    open_start: Optional[int] = None

    for seg in raw.split(","):
        parsed = _parse_single_range_token(seg)
        if not parsed:
            continue
        start, end = parsed
        if end is None:
            open_start = start
        else:
            if end >= start and (end - start) <= max_len:
                episodes.extend(range(start, end + 1))
            elif end < start and (start - end) <= max_len:
                episodes.extend(range(start, end - 1, -1))

    return episodes, open_start, ratio


def evaluate_range_map(range_map: Dict[str, str], source_episode: int) -> Optional[int]:
    """
    Evaluates an AniBridge range_map dictionary (e.g. `{"1-12": "13-24", "13-": "25-|2"}`)
    for a given `source_episode` (1-based) and returns the mapped target episode number,
    or None if `source_episode` is not covered by `range_map`.
    """
    if not isinstance(range_map, dict) or source_episode is None:
        return None
    try:
        ep = int(source_episode)
    except (ValueError, TypeError):
        return None

    # Special case: empty range_map `{}` in AniBridge means 1:1 identity mapping
    if len(range_map) == 0:
        return ep

    for src_expr, dst_expr in range_map.items():
        src_parsed = _parse_single_range_token(src_expr)
        if not src_parsed:
            continue
        src_start, src_end = src_parsed
        if ep < src_start:
            continue
        if src_end is not None and ep > src_end:
            continue

        offset = ep - src_start
        dst_eps, dst_open_start, ratio = _expand_target_segments(dst_expr)

        # Compute target index based on ratio
        if ratio > 1:
            target_idx = offset * ratio
        elif ratio < -1:
            div = abs(ratio)
            target_idx = offset // div
        else:
            target_idx = offset

        if target_idx < len(dst_eps):
            return dst_eps[target_idx]
        if dst_open_start is not None:
            remaining_idx = target_idx - len(dst_eps)
            return dst_open_start + remaining_idx

    return None


def evaluate_reverse_range_map(range_map: Dict[str, str], target_episode: int) -> Optional[int]:
    """
    Inverts an AniBridge `range_map` (`source_range -> target_range`) to find which
    `source_episode` maps to `target_episode`.
    Used when only a unidirectional `anilist -> tmdb_show` mapping is stored, or
    when evaluating custom overrides in reverse.
    """
    if not isinstance(range_map, dict) or target_episode is None:
        return None
    try:
        t_ep = int(target_episode)
    except (ValueError, TypeError):
        return None

    if len(range_map) == 0:
        return t_ep

    for src_expr, dst_expr in range_map.items():
        src_parsed = _parse_single_range_token(src_expr)
        if not src_parsed:
            continue
        src_start, src_end = src_parsed
        dst_eps, dst_open_start, ratio = _expand_target_segments(dst_expr)

        target_idx: Optional[int] = None
        if t_ep in dst_eps:
            target_idx = dst_eps.index(t_ep)
        elif dst_open_start is not None and t_ep >= dst_open_start:
            target_idx = len(dst_eps) + (t_ep - dst_open_start)

        if target_idx is None:
            continue

        if ratio > 1:
            src_offset = target_idx // ratio
        elif ratio < -1:
            src_offset = target_idx * abs(ratio)
        else:
            src_offset = target_idx

        candidate_src = src_start + src_offset
        if src_end is not None and candidate_src > src_end:
            continue
        return candidate_src

    return None


def compose_range_maps(
    anidb_to_src: Dict[str, str],
    anidb_to_dst: Dict[str, str],
) -> Dict[str, str]:
    """
    Given two mappings from a common pivot (e.g. `anidb:1:R -> anilist:290` and
    `anidb:1:R -> tmdb_show:26209:s1`), composes a direct `src -> dst` range map.
    """
    composed: Dict[str, str] = {}
    if not anidb_to_src or not anidb_to_dst:
        return composed

    # Fast path: if both have identical keys, map the values directly
    for k, src_val in anidb_to_src.items():
        if k in anidb_to_dst:
            dst_val = anidb_to_dst[k]
            # Strip any ratio from key side if needed
            src_clean = src_val.split("|")[0].split(",")[0].strip()
            if src_clean:
                composed[src_clean] = dst_val

    if composed:
        return composed

    # Episode-by-episode composition for bounded ranges (up to 500 eps)
    pairs: List[Tuple[int, int]] = []
    for pivot_ep in range(1, 501):
        s_ep = evaluate_range_map(anidb_to_src, pivot_ep)
        d_ep = evaluate_range_map(anidb_to_dst, pivot_ep)
        if s_ep is not None and d_ep is not None:
            pairs.append((s_ep, d_ep))

    if not pairs:
        return composed

    # Group contiguous (s_ep, d_ep) runs with constant offset
    pairs.sort(key=lambda x: x[0])
    run_start_s, run_start_d = pairs[0]
    prev_s, prev_d = pairs[0]

    for s_ep, d_ep in pairs[1:]:
        if s_ep == prev_s + 1 and d_ep == prev_d + 1:
            prev_s, prev_d = s_ep, d_ep
        else:
            s_key = f"{run_start_s}-{prev_s}" if run_start_s != prev_s else str(run_start_s)
            d_val = f"{run_start_d}-{prev_d}" if run_start_d != prev_d else str(run_start_d)
            composed[s_key] = d_val
            run_start_s, run_start_d = s_ep, d_ep
            prev_s, prev_d = s_ep, d_ep

    s_key = f"{run_start_s}-{prev_s}" if run_start_s != prev_s else str(run_start_s)
    d_val = f"{run_start_d}-{prev_d}" if run_start_d != prev_d else str(run_start_d)
    composed[s_key] = d_val
    return composed


# ─────────────────────────────────────────────────────────────────────────────
# Data Ingestion & Maintenance Tasks (Layer 1 & Layer 2)
# ─────────────────────────────────────────────────────────────────────────────

def _default_anime_db_path(anime_db_path: Optional[str] = None, db_path: Optional[str] = None) -> str:
    if anime_db_path:
        return str(anime_db_path)
    if db_path:
        return str(db_path)
    try:
        from resources.lib.database_manager import DatabaseManager
        p = DatabaseManager().get_path("anime_mappings")
        if p:
            return str(p)
    except Exception:
        pass
    try:
        from resources.lib.config_loader import ConfigLoader
        p = ConfigLoader().db_paths.get("anime_mappings")
        if p:
            return str(p)
    except Exception:
        pass
    return str(Path(__file__).resolve().parent.parent.parent / "anime_mappings.db")


def ingest_manami_database(
    anime_db_path: Optional[str] = None,
    source_path_or_url: str = MANAMI_JSONL_URL,
    batch_size: int = 2500,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Downloads (or reads from a local file path) the Manami `anime-offline-database.jsonl`
    (or `.json`) dataset and ingests all entries and title aliases into `anime_mappings.db`.
    """
    anime_db_path = _default_anime_db_path(anime_db_path, db_path)
    init_anime_db(anime_db_path)
    t0 = time.time()
    temp_file: Optional[str] = None

    try:
        if os.path.exists(source_path_or_url):
            local_path = source_path_or_url
            log(f"[AnimeBridge] Ingesting Manami dataset from local file: {local_path}", level=LOGINFO)
        else:
            log(f"[AnimeBridge] Downloading Manami dataset from {source_path_or_url} ...", level=LOGINFO)
            fd, temp_file = tempfile.mkstemp(suffix=".jsonl", prefix="manami_")
            os.close(fd)
            with requests.get(source_path_or_url, stream=True, timeout=120) as resp:
                resp.raise_for_status()
                with open(temp_file, "wb") as f_out:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f_out.write(chunk)
            local_path = temp_file

        identity_batch: List[tuple] = []
        alias_batch: List[tuple] = []
        total_ingested = 0
        last_update_meta = ""
        now_ts = int(time.time())

        def _flush_batches(conn: sqlite3.Connection):
            nonlocal identity_batch, alias_batch
            if not identity_batch and not alias_batch:
                return
            cur = conn.cursor()
            if identity_batch:
                cur.executemany(
                    """
                    INSERT INTO anime_identities (
                        anilist_id, mal_id, anidb_id, kitsu_id, simkl_id,
                        tmdb_id, tmdb_media_type, tvdb_id, imdb_id,
                        title, title_romaji, title_english, title_native,
                        anime_type, episodes, status, season_name, release_year,
                        picture, thumbnail, synonyms_json, tags_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(anilist_id) DO UPDATE SET
                        mal_id          = COALESCE(excluded.mal_id, anime_identities.mal_id),
                        anidb_id        = COALESCE(excluded.anidb_id, anime_identities.anidb_id),
                        kitsu_id        = COALESCE(excluded.kitsu_id, anime_identities.kitsu_id),
                        simkl_id        = COALESCE(excluded.simkl_id, anime_identities.simkl_id),
                        tmdb_id         = COALESCE(excluded.tmdb_id, anime_identities.tmdb_id),
                        tmdb_media_type = COALESCE(excluded.tmdb_media_type, anime_identities.tmdb_media_type),
                        tvdb_id         = COALESCE(excluded.tvdb_id, anime_identities.tvdb_id),
                        imdb_id         = COALESCE(excluded.imdb_id, anime_identities.imdb_id),
                        title           = excluded.title,
                        title_romaji    = COALESCE(excluded.title_romaji, anime_identities.title_romaji),
                        title_english   = COALESCE(excluded.title_english, anime_identities.title_english),
                        title_native    = COALESCE(excluded.title_native, anime_identities.title_native),
                        anime_type      = excluded.anime_type,
                        episodes        = excluded.episodes,
                        status          = excluded.status,
                        season_name     = excluded.season_name,
                        release_year    = excluded.release_year,
                        picture         = excluded.picture,
                        thumbnail       = excluded.thumbnail,
                        synonyms_json   = excluded.synonyms_json,
                        tags_json       = excluded.tags_json,
                        updated_at      = excluded.updated_at
                    """,
                    identity_batch,
                )
                identity_batch = []
            if alias_batch:
                cur.executemany(
                    """
                    INSERT OR IGNORE INTO anime_aliases (anilist_id, alias, normalized_alias, alias_type)
                    VALUES (?, ?, ?, ?)
                    """,
                    alias_batch,
                )
                alias_batch = []
            conn.commit()

        def _process_entry(entry: Dict[str, Any], conn: sqlite3.Connection):
            nonlocal total_ingested, last_update_meta
            if "$schema" in entry and "sources" not in entry:
                last_update_meta = str(entry.get("lastUpdate") or "")
                return

            ids = parse_manami_sources(entry.get("sources", []))
            anilist_id = ids.get("anilist_id")
            if not anilist_id:
                return

            anime_type = str(entry.get("type") or "UNKNOWN").upper()
            tmdb_id = ids.get("tmdb_tv_id") or ids.get("tmdb_movie_id")
            if ids.get("tmdb_movie_id") or anime_type == "MOVIE":
                tmdb_media_type = "movie"
            else:
                tmdb_media_type = "tv"

            primary_title = str(entry.get("title") or "").strip()
            synonyms = [str(s).strip() for s in (entry.get("synonyms") or []) if str(s).strip()]

            title_romaji = primary_title if not _CJK_RE.search(primary_title) else None
            title_english = None
            title_native = primary_title if _CJK_RE.search(primary_title) else None

            all_aliases: List[Tuple[str, str]] = []
            if primary_title:
                p_type = "native" if _CJK_RE.search(primary_title) else "romaji"
                all_aliases.append((primary_title, p_type))

            for syn in synonyms:
                v_type = classify_title_variant(syn, is_primary=False)
                if v_type == "native" and not title_native:
                    title_native = syn
                elif v_type == "english" and not title_english:
                    title_english = syn
                elif v_type == "romaji" and not title_romaji:
                    title_romaji = syn
                all_aliases.append((syn, v_type))

            season_info = entry.get("animeSeason") or {}
            season_name = season_info.get("season")
            release_year = season_info.get("year")

            identity_batch.append(
                (
                    anilist_id,
                    ids.get("mal_id"),
                    ids.get("anidb_id"),
                    ids.get("kitsu_id"),
                    ids.get("simkl_id"),
                    tmdb_id,
                    tmdb_media_type,
                    ids.get("tvdb_id"),
                    ids.get("imdb_id"),
                    primary_title,
                    title_romaji or primary_title,
                    title_english,
                    title_native,
                    anime_type,
                    entry.get("episodes"),
                    entry.get("status"),
                    season_name,
                    release_year,
                    entry.get("picture"),
                    entry.get("thumbnail"),
                    json.dumps(synonyms, ensure_ascii=False),
                    json.dumps(entry.get("tags") or [], ensure_ascii=False),
                    now_ts,
                )
            )

            seen_aliases = set()
            for alias_str, alias_type in all_aliases:
                if len(alias_str) < 2 or alias_str in seen_aliases:
                    continue
                seen_aliases.add(alias_str)
                norm = normalize_search_title(alias_str)
                if norm:
                    alias_batch.append((anilist_id, alias_str, norm, alias_type))

            total_ingested += 1
            if len(identity_batch) >= batch_size:
                _flush_batches(conn)

        with db_connect(anime_db_path) as conn:
            with open(local_path, "r", encoding="utf-8") as f:
                first_char = f.read(1)
                f.seek(0)
                if first_char == "{":
                    # Check if JSONL or single JSON object with "data" array
                    first_line = f.readline()
                    second_line = f.readline()
                    f.seek(0)
                    if second_line.strip().startswith("{"):
                        # JSONL format
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                                _process_entry(obj, conn)
                            except json.JSONDecodeError:
                                continue
                    else:
                        # Standard JSON object with {"data": [...]}
                        payload = json.load(f)
                        last_update_meta = str(payload.get("lastUpdate") or "")
                        for obj in payload.get("data", []):
                            _process_entry(obj, conn)

            _flush_batches(conn)

            cur = conn.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO anime_sync_meta (key, value, updated_at) VALUES (?, ?, ?)",
                ("manami_last_synced", str(now_ts), now_ts),
            )
            cur.execute(
                "INSERT OR REPLACE INTO anime_sync_meta (key, value, updated_at) VALUES (?, ?, ?)",
                ("manami_entry_count", str(total_ingested), now_ts),
            )
            if last_update_meta:
                cur.execute(
                    "INSERT OR REPLACE INTO anime_sync_meta (key, value, updated_at) VALUES (?, ?, ?)",
                    ("manami_upstream_date", last_update_meta, now_ts),
                )
            conn.commit()

        elapsed = round(time.time() - t0, 2)
        log(
            f"[AnimeBridge] Manami ingestion complete: {total_ingested} anime entries indexed in {elapsed}s.",
            level=LOGINFO,
        )
        return {
            "success": True,
            "entries_ingested": total_ingested,
            "upstream_date": last_update_meta,
            "elapsed_seconds": elapsed,
        }

    except Exception as exc:
        log(f"[AnimeBridge] Error ingesting Manami database: {exc}", level=LOGERROR)
        return {"success": False, "error": str(exc)}
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass


def ingest_anibridge_mappings(
    anime_db_path: Optional[str] = None,
    source_path_or_url: str = ANIBRIDGE_MAPPINGS_URL,
    batch_size: int = 4000,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Downloads (or reads from a local file path) the AniBridge `mappings.min.json` / `mappings.json`
    dataset and ingests all episode range mappings into `anime_episode_mappings`, while also
    enriching `anime_identities` with `tmdb_id`, `tvdb_id`, `mal_id`, and `imdb_id`.

    Supports both:
      - AniBridge v3 descriptor schema (`provider:id[:scope]` -> `{target_desc: {src_range: dst_range}}`),
        including automatic synthesis of direct `anilist <-> tmdb/tvdb` rules when bridged via `anidb`.
      - Legacy AniBridge v2 schema (`{anilist_id: {tmdb_id, tvdb_id, tmdb_mappings: {s1: "e1-e12"}}}`).
    """
    anime_db_path = _default_anime_db_path(anime_db_path, db_path)
    init_anime_db(anime_db_path)
    t0 = time.time()

    try:
        if os.path.exists(source_path_or_url):
            log(f"[AnimeBridge] Loading AniBridge mappings from local file: {source_path_or_url}", level=LOGINFO)
            with open(source_path_or_url, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        else:
            log(f"[AnimeBridge] Downloading AniBridge mappings from {source_path_or_url} ...", level=LOGINFO)
            try:
                resp = requests.get(source_path_or_url, timeout=90)
                resp.raise_for_status()
            except Exception:
                log(f"[AnimeBridge] Primary URL failed, trying fallback {ANIBRIDGE_FALLBACK_URL} ...", level=LOGWARNING)
                resp = requests.get(ANIBRIDGE_FALLBACK_URL, timeout=90)
                resp.raise_for_status()
            raw_data = resp.json()

        meta = raw_data.get("$meta") if isinstance(raw_data, dict) else None
        schema_version = str(meta.get("schema_version") if isinstance(meta, dict) else "2.0")

        mapping_rows: Dict[Tuple[str, str], Tuple[str, str, str, str, str, str, str, str, str]] = {}
        identity_enrichment: Dict[int, Dict[str, Any]] = {}

        def _add_mapping_rule(src_desc: str, dst_desc: str, range_map: Dict[str, str], overwrite: bool = True):
            if not src_desc or not dst_desc or src_desc == dst_desc:
                return
            key = (src_desc, dst_desc)
            if not overwrite and key in mapping_rows:
                return
            s_prov, s_id, s_scope = parse_descriptor(src_desc)
            t_prov, t_id, t_scope = parse_descriptor(dst_desc)
            if not s_prov or not s_id or not t_prov or not t_id:
                return
            mapping_rows[key] = (
                src_desc,
                s_prov,
                s_id,
                s_scope,
                dst_desc,
                t_prov,
                t_id,
                t_scope,
                json.dumps(range_map or {}, ensure_ascii=False),
            )

        def _record_enrichment(anilist_id: int, field: str, val: Any, media_type: Optional[str] = None):
            if not anilist_id or val is None:
                return
            rec = identity_enrichment.setdefault(anilist_id, {})
            if field not in rec:
                rec[field] = val
            if media_type and "tmdb_media_type" not in rec:
                rec["tmdb_media_type"] = media_type

        if isinstance(raw_data, dict):
            for top_key, top_val in raw_data.items():
                if top_key.startswith("$") or not isinstance(top_val, dict):
                    continue

                # Detect v2 vs v3 entry
                if ":" in top_key:
                    # v3 Descriptor entry
                    s_prov, s_id, s_scope = parse_descriptor(top_key)

                    # Store all explicit descriptor pairs
                    for target_desc, range_map in top_val.items():
                        if not isinstance(range_map, dict):
                            continue
                        _add_mapping_rule(top_key, target_desc, range_map, overwrite=True)

                        t_prov, t_id, _ = parse_descriptor(target_desc)
                        if s_prov == "anilist" and s_id.isdigit():
                            al_id = int(s_id)
                            if t_prov == "tmdb_show" and t_id.isdigit():
                                _record_enrichment(al_id, "tmdb_id", int(t_id), "tv")
                            elif t_prov == "tmdb_movie" and t_id.isdigit():
                                _record_enrichment(al_id, "tmdb_id", int(t_id), "movie")
                            elif t_prov in ("tvdb_show", "tvdb_movie") and t_id.isdigit():
                                _record_enrichment(al_id, "tvdb_id", int(t_id))
                            elif t_prov == "mal" and t_id.isdigit():
                                _record_enrichment(al_id, "mal_id", int(t_id))
                            elif t_prov in ("imdb_show", "imdb_movie"):
                                _record_enrichment(al_id, "imdb_id", t_id)

                    # If this is an anidb pivot entry (e.g. "anidb:1:R"), synthesize direct
                    # anilist <-> tmdb/tvdb rules for any pairs sharing this pivot!
                    if s_prov == "anidb":
                        anilist_targets = [
                            (td, rm) for td, rm in top_val.items()
                            if td.startswith("anilist:") and isinstance(rm, dict)
                        ]
                        western_targets = [
                            (td, rm) for td, rm in top_val.items()
                            if td.startswith(("tmdb_show:", "tmdb_movie:", "tvdb_show:", "tvdb_movie:", "imdb_show:", "imdb_movie:", "mal:"))
                            and isinstance(rm, dict)
                        ]
                        if anilist_targets and s_id.isdigit():
                            for al_desc, _ in anilist_targets:
                                _, al_id_str, _ = parse_descriptor(al_desc)
                                if al_id_str.isdigit():
                                    _record_enrichment(int(al_id_str), "anidb_id", int(s_id))

                        for al_desc, al_rm in anilist_targets:
                            _, al_id_str, _ = parse_descriptor(al_desc)
                            al_id = int(al_id_str) if al_id_str.isdigit() else 0
                            for w_desc, w_rm in western_targets:
                                w_prov, w_id, _ = parse_descriptor(w_desc)
                                if al_id:
                                    if w_prov == "tmdb_show" and w_id.isdigit():
                                        _record_enrichment(al_id, "tmdb_id", int(w_id), "tv")
                                    elif w_prov == "tmdb_movie" and w_id.isdigit():
                                        _record_enrichment(al_id, "tmdb_id", int(w_id), "movie")
                                    elif w_prov in ("tvdb_show", "tvdb_movie") and w_id.isdigit():
                                        _record_enrichment(al_id, "tvdb_id", int(w_id))
                                    elif w_prov == "mal" and w_id.isdigit():
                                        _record_enrichment(al_id, "mal_id", int(w_id))
                                    elif w_prov in ("imdb_show", "imdb_movie"):
                                        _record_enrichment(al_id, "imdb_id", w_id)

                                if w_prov.startswith(("tmdb_", "tvdb_")):
                                    forward_map = compose_range_maps(al_rm, w_rm)
                                    if forward_map:
                                        _add_mapping_rule(al_desc, w_desc, forward_map, overwrite=False)
                                    reverse_map = compose_range_maps(w_rm, al_rm)
                                    if reverse_map:
                                        _add_mapping_rule(w_desc, al_desc, reverse_map, overwrite=False)

                elif top_key.isdigit():
                    # Legacy v2 format: top_key is anilist_id
                    al_id = int(top_key)
                    al_desc = f"anilist:{al_id}"
                    tmdb_id = top_val.get("tmdb_id")
                    tvdb_id = top_val.get("tvdb_id")
                    mal_id = top_val.get("mal_id")
                    if isinstance(tmdb_id, list):
                        tmdb_id = tmdb_id[0] if tmdb_id else None
                    if isinstance(tvdb_id, list):
                        tvdb_id = tvdb_id[0] if tvdb_id else None
                    if isinstance(mal_id, list):
                        mal_id = mal_id[0] if mal_id else None

                    if tmdb_id:
                        _record_enrichment(al_id, "tmdb_id", int(tmdb_id), "tv")
                    if tvdb_id:
                        _record_enrichment(al_id, "tvdb_id", int(tvdb_id))
                    if mal_id:
                        _record_enrichment(al_id, "mal_id", int(mal_id))

                    for map_field, prov_prefix, prov_id in [
                        ("tmdb_mappings", "tmdb_show", tmdb_id),
                        ("tvdb_mappings", "tvdb_show", tvdb_id),
                    ]:
                        m_dict = top_val.get(map_field)
                        if isinstance(m_dict, dict) and prov_id:
                            for s_scope, expr in m_dict.items():
                                w_desc = f"{prov_prefix}:{prov_id}:{s_scope}"
                                parsed_tok = _parse_single_range_token(str(expr))
                                if parsed_tok:
                                    st, en = parsed_tok
                                    length = (en - st + 1) if en is not None else 12
                                    rm = {f"1-{length}": f"{st}-{en}" if en is not None else f"{st}-"}
                                    rev_rm = {f"{st}-{en}" if en is not None else f"{st}-": f"1-{length}"}
                                    _add_mapping_rule(al_desc, w_desc, rm, overwrite=True)
                                    _add_mapping_rule(w_desc, al_desc, rev_rm, overwrite=True)

        now_ts = int(time.time())
        all_rule_tuples = list(mapping_rows.values())

        with db_connect(anime_db_path) as conn:
            cur = conn.cursor()
            for i in range(0, len(all_rule_tuples), batch_size):
                chunk = all_rule_tuples[i : i + batch_size]
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO anime_episode_mappings (
                        source_descriptor, source_provider, source_id, source_scope,
                        target_descriptor, target_provider, target_id, target_scope,
                        range_map_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    chunk,
                )

            # Enrich anime_identities with discovered TMDB / TVDB / MAL / IMDb IDs
            enrichment_tuples = []
            for al_id, fields in identity_enrichment.items():
                enrichment_tuples.append(
                    (
                        al_id,
                        fields.get("mal_id"),
                        fields.get("anidb_id"),
                        fields.get("tmdb_id"),
                        fields.get("tmdb_media_type", "tv"),
                        fields.get("tvdb_id"),
                        fields.get("imdb_id"),
                        now_ts,
                    )
                )

            for i in range(0, len(enrichment_tuples), batch_size):
                chunk = enrichment_tuples[i : i + batch_size]
                cur.executemany(
                    """
                    INSERT INTO anime_identities (
                        anilist_id, mal_id, anidb_id, tmdb_id, tmdb_media_type, tvdb_id, imdb_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(anilist_id) DO UPDATE SET
                        mal_id          = COALESCE(anime_identities.mal_id, excluded.mal_id),
                        anidb_id        = COALESCE(anime_identities.anidb_id, excluded.anidb_id),
                        tmdb_id         = COALESCE(excluded.tmdb_id, anime_identities.tmdb_id),
                        tmdb_media_type = COALESCE(excluded.tmdb_media_type, anime_identities.tmdb_media_type),
                        tvdb_id         = COALESCE(excluded.tvdb_id, anime_identities.tvdb_id),
                        imdb_id         = COALESCE(excluded.imdb_id, anime_identities.imdb_id),
                        updated_at      = excluded.updated_at
                    """,
                    chunk,
                )

            cur.execute(
                "INSERT OR REPLACE INTO anime_sync_meta (key, value, updated_at) VALUES (?, ?, ?)",
                ("anibridge_last_synced", str(now_ts), now_ts),
            )
            cur.execute(
                "INSERT OR REPLACE INTO anime_sync_meta (key, value, updated_at) VALUES (?, ?, ?)",
                ("anibridge_schema_version", schema_version, now_ts),
            )
            cur.execute(
                "INSERT OR REPLACE INTO anime_sync_meta (key, value, updated_at) VALUES (?, ?, ?)",
                ("anibridge_rule_count", str(len(all_rule_tuples)), now_ts),
            )
            conn.commit()

        elapsed = round(time.time() - t0, 2)
        log(
            f"[AnimeBridge] AniBridge mappings ingestion complete: {len(all_rule_tuples)} rules & "
            f"{len(enrichment_tuples)} ID enrichments saved in {elapsed}s (schema v{schema_version}).",
            level=LOGINFO,
        )
        return {
            "success": True,
            "rules_ingested": len(all_rule_tuples),
            "identities_enriched": len(enrichment_tuples),
            "schema_version": schema_version,
            "elapsed_seconds": elapsed,
        }

    except Exception as exc:
        log(f"[AnimeBridge] Error ingesting AniBridge mappings: {exc}", level=LOGERROR)
        return {"success": False, "error": str(exc)}


def sync_all_anime_datasets(
    anime_db_path: Optional[str] = None,
    overrides_file_path: Optional[str] = None,
    force: bool = False,
    db_path: Optional[str] = None,
    overrides_path: Optional[str] = None,
    max_age_seconds: Optional[int] = None,
    max_age_hours: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Synchronizes both Layer 1 (Manami) and Layer 2 (AniBridge) datasets and reloads local custom overrides.
    """
    anime_db_path = _default_anime_db_path(anime_db_path, db_path)
    overrides_file_path = overrides_file_path or overrides_path
    if max_age_seconds is None and max_age_hours is not None:
        max_age_seconds = int(max_age_hours) * 3600
    if not _ingestion_lock.acquire(blocking=False):
        return {"success": False, "status": "already_running", "message": "Anime dataset sync is already in progress."}

    try:
        init_anime_db(anime_db_path)
        now = int(time.time())

        if not force:
            with db_connect(anime_db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT key, value FROM anime_sync_meta")
                meta = {r[0]: r[1] for r in cur.fetchall()}
                last_manami = int(meta.get("manami_last_synced") or 0)
                last_anibridge = int(meta.get("anibridge_last_synced") or 0)
                manami_ttl = int(max_age_seconds) if max_age_seconds is not None else 604800
                anibridge_ttl = min(int(max_age_seconds), 86400) if max_age_seconds is not None else 86400
                # Refresh AniBridge daily (86400s) and Manami weekly (604800s)
                need_manami = (now - last_manami) >= manami_ttl or int(meta.get("manami_entry_count") or 0) == 0
                need_anibridge = (now - last_anibridge) >= anibridge_ttl or int(meta.get("anibridge_rule_count") or 0) == 0
        else:
            need_manami = True
            need_anibridge = True

        manami_res = (
            ingest_manami_database(anime_db_path)
            if need_manami
            else {"success": True, "skipped": True, "reason": "Up to date"}
        )
        anibridge_res = (
            ingest_anibridge_mappings(anime_db_path)
            if need_anibridge
            else {"success": True, "skipped": True, "reason": "Up to date"}
        )
        overrides_res = sync_custom_overrides_file(anime_db_path, overrides_file_path)

        return {
            "success": bool(manami_res.get("success") and anibridge_res.get("success")),
            "manami": manami_res,
            "anibridge": anibridge_res,
            "overrides": overrides_res,
        }
    finally:
        _ingestion_lock.release()


# ─────────────────────────────────────────────────────────────────────────────
# Custom Overrides Engine (File + SQLite Table)
# ─────────────────────────────────────────────────────────────────────────────

def sync_custom_overrides_file(
    anime_db_path: Optional[str] = None,
    overrides_file_path: Optional[str] = None,
    db_path: Optional[str] = None,
    file_path: Optional[str] = None,
) -> Dict[str, Any]:
    anime_db_path = _default_anime_db_path(anime_db_path, db_path)
    overrides_file_path = overrides_file_path or file_path
    init_anime_db(anime_db_path)
    if not overrides_file_path:
        root_dir = Path(anime_db_path).resolve().parent
        for candidate in ("anime_overrides.json", "anime_overrides.yaml", "anime_overrides.yml"):
            p = root_dir / candidate
            if p.exists():
                overrides_file_path = str(p)
                break

    if not overrides_file_path or not os.path.exists(overrides_file_path):
        return {"loaded": 0, "file": None}

    try:
        with open(overrides_file_path, "r", encoding="utf-8") as f:
            if overrides_file_path.endswith((".yaml", ".yml")):
                try:
                    import yaml  # type: ignore
                    data = yaml.safe_load(f)
                except ImportError:
                    log("[AnimeBridge] PyYAML not installed; use anime_overrides.json for file overrides.", level=LOGWARNING)
                    return {"loaded": 0, "error": "PyYAML not installed"}
            else:
                data = json.load(f)

        count = 0
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                if upsert_custom_override(anime_db_path, item):
                    count += 1
        elif isinstance(data, dict):
            if isinstance(data.get("overrides"), list):
                for item in data["overrides"]:
                    if isinstance(item, dict) and upsert_custom_override(anime_db_path, item):
                        count += 1
            else:
                for src_desc, targets in data.items():
                    if str(src_desc).startswith("_") or not isinstance(targets, dict):
                        continue
                    for dst_desc, range_map in targets.items():
                        if not isinstance(range_map, dict):
                            continue
                        if upsert_custom_override(
                            anime_db_path,
                            {
                                "source_descriptor": src_desc,
                                "target_descriptor": dst_desc,
                                "range_map": range_map,
                                "notes": "Loaded from overrides file",
                            },
                        ):
                            count += 1

        log(f"[AnimeBridge] Loaded {count} custom override rules from {overrides_file_path}", level=LOGINFO)
        return {"loaded": count, "overrides_loaded": count, "file": overrides_file_path}
    except Exception as exc:
        log(f"[AnimeBridge] Error loading overrides file {overrides_file_path}: {exc}", level=LOGERROR)
        return {"loaded": 0, "overrides_loaded": 0, "error": str(exc)}


def upsert_custom_override(
    arg1: Union[str, Dict[str, Any], None] = None,
    arg2: Union[Dict[str, Any], str, None] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Inserts or updates a high-priority custom override rule in `anime_custom_overrides`.
    Accepts either `(anime_db_path, rule)` or `(rule, db_path)` argument order.
    """
    if isinstance(arg1, dict):
        rule = arg1
        anime_db_path = _default_anime_db_path(arg2 if isinstance(arg2, str) else None, db_path)
    else:
        anime_db_path = _default_anime_db_path(arg1 if isinstance(arg1, str) else None, db_path)
        rule = arg2 if isinstance(arg2, dict) else {}

    init_anime_db(anime_db_path)
    anilist_id = rule.get("anilist_id")
    tmdb_id = rule.get("tmdb_id")
    tvdb_id = rule.get("tvdb_id")
    target_provider = str(rule.get("target_provider") or "").lower()
    media_type = str(rule.get("media_type") or ("movie" if "movie" in target_provider else "tv")).lower()
    season = rule.get("season") if rule.get("season") is not None else rule.get("target_season")
    notes = str(rule.get("notes") or "")

    src_desc = rule.get("source_descriptor")
    dst_desc = rule.get("target_descriptor")
    range_map = rule.get("range_map") or rule.get("range_mapping") or {}
    if isinstance(range_map, str):
        try:
            range_map = json.loads(range_map)
        except Exception:
            range_map = {}

    if not src_desc and anilist_id:
        src_desc = f"anilist:{int(anilist_id)}"
    if not dst_desc and tmdb_id:
        if media_type == "movie" or season is None:
            dst_desc = f"tmdb_movie:{int(tmdb_id)}"
        else:
            dst_desc = f"tmdb_show:{int(tmdb_id)}:s{int(season)}"
    elif not dst_desc and tvdb_id:
        if media_type == "movie" or season is None:
            dst_desc = f"tvdb_movie:{int(tvdb_id)}"
        else:
            dst_desc = f"tvdb_show:{int(tvdb_id)}:s{int(season)}"

    if not src_desc or not dst_desc:
        return {"success": False, "error": "Missing source_descriptor or target_descriptor"}

    s_prov, s_id, s_scope = parse_descriptor(src_desc)
    t_prov, t_id, t_scope = parse_descriptor(dst_desc)

    if not anilist_id:
        if s_prov == "anilist" and s_id.isdigit():
            anilist_id = int(s_id)
        elif t_prov == "anilist" and t_id.isdigit():
            anilist_id = int(t_id)

    if not tmdb_id:
        if t_prov in ("tmdb_show", "tmdb_movie") and t_id.isdigit():
            tmdb_id = int(t_id)
            media_type = "movie" if t_prov == "tmdb_movie" else "tv"
            if season is None and t_scope:
                season = parse_season_scope(t_scope)
        elif s_prov in ("tmdb_show", "tmdb_movie") and s_id.isdigit():
            tmdb_id = int(s_id)
            media_type = "movie" if s_prov == "tmdb_movie" else "tv"
            if season is None and s_scope:
                season = parse_season_scope(s_scope)

    now_ts = int(time.time())
    with db_connect(anime_db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO anime_custom_overrides (
                anilist_id, tmdb_id, tvdb_id, media_type, season,
                source_descriptor, target_descriptor, range_map_json, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                anilist_id,
                tmdb_id,
                tvdb_id,
                media_type,
                season,
                src_desc,
                dst_desc,
                json.dumps(range_map, ensure_ascii=False),
                notes,
                now_ts,
            ),
        )
        override_id = cur.lastrowid
        conn.commit()
    return {
        "success": True,
        "id": override_id,
        "source_descriptor": src_desc,
        "target_descriptor": dst_desc,
    }


def get_custom_overrides(anime_db_path: Optional[str] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    anime_db_path = _default_anime_db_path(anime_db_path, db_path)
    init_anime_db(anime_db_path)
    with db_connect(anime_db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM anime_custom_overrides ORDER BY updated_at DESC, id DESC")
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            try:
                d["range_map"] = json.loads(d.get("range_map_json") or "{}")
            except Exception:
                d["range_map"] = {}
            rows.append(d)
        return rows


def delete_custom_override(
    arg1: Union[str, int, None] = None,
    override_id: Union[int, str, None] = None,
    source_descriptor: Optional[str] = None,
    target_descriptor: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    if isinstance(arg1, int) or (isinstance(arg1, str) and arg1.isdigit()):
        real_override_id = int(arg1)
        anime_db_path = _default_anime_db_path(override_id if isinstance(override_id, str) else None, db_path)
    else:
        anime_db_path = _default_anime_db_path(arg1 if isinstance(arg1, str) else None, db_path)
        real_override_id = int(override_id) if override_id is not None and str(override_id).isdigit() else None

    init_anime_db(anime_db_path)
    with db_connect(anime_db_path) as conn:
        cur = conn.cursor()
        if real_override_id is not None:
            cur.execute("DELETE FROM anime_custom_overrides WHERE id = ?", (int(real_override_id),))
        elif source_descriptor and target_descriptor:
            cur.execute(
                "DELETE FROM anime_custom_overrides WHERE source_descriptor = ? AND target_descriptor = ?",
                (source_descriptor, target_descriptor),
            )
        elif source_descriptor:
            cur.execute("DELETE FROM anime_custom_overrides WHERE source_descriptor = ?", (source_descriptor,))
        else:
            return False
        conn.commit()
        return cur.rowcount > 0


# ─────────────────────────────────────────────────────────────────────────────
# Core Translation Service: AnimeCoordinateService
# ─────────────────────────────────────────────────────────────────────────────

class AnimeCoordinateService:
    """
    Core translation service bridging Anime identifiers & absolute episode numbers
    with Western TMDB/TVDB Season + Episode coordinates and multi-language search aliases.
    """

    def __init__(
        self,
        anime_db_path: Optional[str] = None,
        tmdb_handler: Any = None,
        tvshows_static_db_path: Optional[str] = None,
        db_path: Optional[str] = None,
    ):
        self.anime_db_path = _default_anime_db_path(anime_db_path, db_path)
        self.db_path = self.anime_db_path
        self.tmdb_handler = tmdb_handler
        self.tvshows_static_db_path = tvshows_static_db_path
        init_anime_db(self.anime_db_path)

    def ensure_synced(self, max_age_hours: int = 168) -> Dict[str, Any]:
        """Ensures Layer 1 & Layer 2 datasets are synced if older than `max_age_hours`."""
        return sync_all_anime_datasets(
            anime_db_path=self.anime_db_path,
            force=False,
            max_age_seconds=int(max_age_hours) * 3600,
        )

    def sync_datasets(self, force: bool = True) -> Dict[str, Any]:
        """Triggers a full sync of Manami, AniBridge, and local custom overrides."""
        return sync_all_anime_datasets(anime_db_path=self.anime_db_path, force=force)

    def get_identity(
        self,
        anilist_id: Optional[int] = None,
        mal_id: Optional[int] = None,
        tmdb_id: Optional[int] = None,
        tvdb_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Looks up an anime identity row by `anilist_id`, `mal_id`, `tmdb_id`, or `tvdb_id`.
        """
        with db_connect(self.anime_db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            if anilist_id:
                cur.execute("SELECT * FROM anime_identities WHERE anilist_id = ?", (int(anilist_id),))
            elif mal_id:
                cur.execute("SELECT * FROM anime_identities WHERE mal_id = ? LIMIT 1", (int(mal_id),))
            elif tmdb_id:
                cur.execute("SELECT * FROM anime_identities WHERE tmdb_id = ? ORDER BY anilist_id ASC LIMIT 1", (int(tmdb_id),))
            elif tvdb_id:
                cur.execute("SELECT * FROM anime_identities WHERE tvdb_id = ? ORDER BY anilist_id ASC LIMIT 1", (int(tvdb_id),))
            else:
                return None

            row = cur.fetchone()
            if not row:
                return None
            data = dict(row)
            try:
                data["synonyms"] = json.loads(data.get("synonyms_json") or "[]")
            except Exception:
                data["synonyms"] = []
            try:
                data["tags"] = json.loads(data.get("tags_json") or "[]")
            except Exception:
                data["tags"] = []
            return data

    def _resolve_tvdb_to_tmdb(self, tvdb_id: int) -> Optional[int]:
        """
        Resolves a TVDB show ID to a TMDB show ID using:
          1. `tvshows_static_cache.db` (local confirmed library)
          2. `tmdb_handler.find_by_external_id(tvdb_id, source='tvdb_id')` (canonical TMDB API)
          3. `anime_identities` cross-reference
        """
        if not tvdb_id:
            return None

        if self.tvshows_static_db_path and os.path.exists(self.tvshows_static_db_path):
            try:
                with db_connect(self.tvshows_static_db_path) as s_conn:
                    s_cur = s_conn.cursor()
                    s_cur.execute("SELECT show_tmdb_id FROM shows WHERE tvdb_id = ? LIMIT 1", (str(tvdb_id),))
                    r = s_cur.fetchone()
                    if r and r[0]:
                        return int(r[0])
            except Exception:
                pass

        if self.tmdb_handler and hasattr(self.tmdb_handler, "find_by_external_id"):
            try:
                res = self.tmdb_handler.find_by_external_id(str(tvdb_id), source="tvdb_id")
                if res and res.get("id"):
                    canonical_id = int(res["id"])
                    try:
                        with db_connect(self.anime_db_path) as conn:
                            cur = conn.cursor()
                            cur.execute(
                                "UPDATE anime_identities SET tmdb_id = ? WHERE tvdb_id = ? AND (tmdb_id IS NULL OR tmdb_id != ?)",
                                (canonical_id, int(tvdb_id), canonical_id),
                            )
                            conn.commit()
                    except Exception:
                        pass
                    return canonical_id
            except Exception:
                pass

        with db_connect(self.anime_db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT tmdb_id FROM anime_identities WHERE tvdb_id = ? AND tmdb_id IS NOT NULL LIMIT 1",
                (int(tvdb_id),),
            )
            r = cur.fetchone()
            if r and r[0]:
                return int(r[0])

        return None

    def resolve_anime_to_western(
        self,
        anilist_id: int,
        episode_num: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """
        Translates `(anilist_id, episode_num)` into Western TMDB/TVDB coordinates:
          `{
              "success": bool,
              "anilist_id": int,
              "mal_id": int | None,
              "tmdb_id": int | None,
              "tvdb_id": int | None,
              "imdb_id": str | None,
              "media_type": "episode" | "movie",
              "season": int | None,
              "episode": int | None,
              "absolute_episode": int,
              "mapping_source": "custom_override" | "anibridge" | "fallback_1to1",
              "fallback_used": bool
          }`
        """
        if not anilist_id:
            return None
        al_id = int(anilist_id)
        ep_num = int(episode_num) if episode_num is not None else 1
        src_desc = f"anilist:{al_id}"

        identity = self.get_identity(anilist_id=al_id) or {}
        mal_id = identity.get("mal_id")
        imdb_id = identity.get("imdb_id")
        tvdb_id = identity.get("tvdb_id")
        known_tmdb_id = identity.get("tmdb_id")
        anime_type = str(identity.get("anime_type") or "").upper()

        with db_connect(self.anime_db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 1. Check Custom Overrides first (forward & reverse)
            cur.execute(
                """
                SELECT * FROM anime_custom_overrides
                WHERE source_descriptor = ? OR target_descriptor = ? OR anilist_id = ?
                ORDER BY updated_at DESC
                """,
                (src_desc, src_desc, al_id),
            )
            override_rows = cur.fetchall()
            for ov in override_rows:
                try:
                    rm = json.loads(ov["range_map_json"] or "{}")
                except Exception:
                    rm = {}

                if ov["source_descriptor"] == src_desc:
                    target_desc = ov["target_descriptor"]
                    mapped_ep = evaluate_range_map(rm, ep_num)
                else:
                    target_desc = ov["source_descriptor"]
                    mapped_ep = evaluate_reverse_range_map(rm, ep_num)

                if mapped_ep is None:
                    continue

                t_prov, t_id, t_scope = parse_descriptor(target_desc)
                if t_prov == "tmdb_movie" and t_id.isdigit():
                    return {
                        "success": True,
                        "anilist_id": al_id,
                        "mal_id": mal_id,
                        "tmdb_id": int(t_id),
                        "tvdb_id": ov["tvdb_id"] or tvdb_id,
                        "imdb_id": imdb_id,
                        "media_type": "movie",
                        "season": None,
                        "episode": None,
                        "absolute_episode": ep_num,
                        "mapping_source": "custom_override",
                        "fallback_used": False,
                    }
                if t_prov in ("tmdb_show", "tvdb_show") and t_id.isdigit():
                    season_num = parse_season_scope(t_scope)
                    if season_num is None:
                        season_num = ov["season"] if ov["season"] is not None else 1
                    resolved_tmdb = int(t_id) if t_prov == "tmdb_show" else (ov["tmdb_id"] or known_tmdb_id or self._resolve_tvdb_to_tmdb(int(t_id)))
                    resolved_tvdb = int(t_id) if t_prov == "tvdb_show" else (ov["tvdb_id"] or tvdb_id)
                    return {
                        "success": True,
                        "anilist_id": al_id,
                        "mal_id": mal_id,
                        "tmdb_id": resolved_tmdb,
                        "tvdb_id": resolved_tvdb,
                        "imdb_id": imdb_id,
                        "media_type": "episode",
                        "season": season_num,
                        "episode": int(mapped_ep),
                        "absolute_episode": ep_num,
                        "mapping_source": "custom_override",
                        "fallback_used": False,
                    }

            # 2. Check Layer 2 AniBridge Episode Mappings (forward from anilist:{id})
            cur.execute(
                """
                SELECT * FROM anime_episode_mappings
                WHERE source_descriptor = ?
                  AND target_provider IN ('tmdb_show', 'tmdb_movie', 'tvdb_show', 'tvdb_movie')
                """,
                (src_desc,),
            )
            forward_rules = cur.fetchall()

            # Sort so tmdb_show / tmdb_movie rules are evaluated before tvdb_show / tvdb_movie
            priority_order = {"tmdb_show": 0, "tmdb_movie": 1, "tvdb_show": 2, "tvdb_movie": 3}
            forward_rules = sorted(
                forward_rules,
                key=lambda r: priority_order.get(r["target_provider"], 99),
            )

            tvdb_match: Optional[Dict[str, Any]] = None
            for rule in forward_rules:
                try:
                    rm = json.loads(rule["range_map_json"] or "{}")
                except Exception:
                    rm = {}
                mapped_ep = evaluate_range_map(rm, ep_num)
                if mapped_ep is None:
                    continue

                t_prov = rule["target_provider"]
                t_id = rule["target_id"]
                t_scope = rule["target_scope"]

                if t_prov == "tmdb_movie" and str(t_id).isdigit():
                    return {
                        "success": True,
                        "anilist_id": al_id,
                        "mal_id": mal_id,
                        "tmdb_id": int(t_id),
                        "tvdb_id": tvdb_id,
                        "imdb_id": imdb_id,
                        "media_type": "movie",
                        "season": None,
                        "episode": None,
                        "absolute_episode": ep_num,
                        "mapping_source": "anibridge",
                        "fallback_used": False,
                    }

                if t_prov == "tmdb_show" and str(t_id).isdigit():
                    season_num = parse_season_scope(t_scope)
                    if season_num is None:
                        season_num = 1
                    return {
                        "success": True,
                        "anilist_id": al_id,
                        "mal_id": mal_id,
                        "tmdb_id": int(t_id),
                        "tvdb_id": tvdb_id,
                        "imdb_id": imdb_id,
                        "media_type": "episode",
                        "season": season_num,
                        "episode": int(mapped_ep),
                        "absolute_episode": ep_num,
                        "mapping_source": "anibridge",
                        "fallback_used": False,
                    }

                if t_prov == "tvdb_show" and str(t_id).isdigit() and tvdb_match is None:
                    season_num = parse_season_scope(t_scope)
                    if season_num is None:
                        season_num = 1
                    resolved_tmdb = known_tmdb_id or self._resolve_tvdb_to_tmdb(int(t_id))
                    tvdb_match = {
                        "success": True,
                        "anilist_id": al_id,
                        "mal_id": mal_id,
                        "tmdb_id": resolved_tmdb,
                        "tvdb_id": int(t_id),
                        "imdb_id": imdb_id,
                        "media_type": "episode",
                        "season": season_num,
                        "episode": int(mapped_ep),
                        "absolute_episode": ep_num,
                        "mapping_source": "anibridge_tvdb",
                        "fallback_used": False,
                    }

            if tvdb_match:
                return tvdb_match

            # 3. Also check reverse AniBridge rules where target_descriptor == 'anilist:{id}'
            cur.execute(
                """
                SELECT * FROM anime_episode_mappings
                WHERE target_descriptor = ?
                  AND source_provider IN ('tmdb_show', 'tmdb_movie', 'tvdb_show', 'tvdb_movie')
                """,
                (src_desc,),
            )
            reverse_rules = sorted(
                cur.fetchall(),
                key=lambda r: priority_order.get(r["source_provider"], 99),
            )
            for rule in reverse_rules:
                try:
                    rm = json.loads(rule["range_map_json"] or "{}")
                except Exception:
                    rm = {}
                mapped_ep = evaluate_reverse_range_map(rm, ep_num)
                if mapped_ep is None:
                    continue
                s_prov = rule["source_provider"]
                s_id = rule["source_id"]
                s_scope = rule["source_scope"]
                if s_prov == "tmdb_movie" and str(s_id).isdigit():
                    return {
                        "success": True,
                        "anilist_id": al_id,
                        "mal_id": mal_id,
                        "tmdb_id": int(s_id),
                        "tvdb_id": tvdb_id,
                        "imdb_id": imdb_id,
                        "media_type": "movie",
                        "season": None,
                        "episode": None,
                        "absolute_episode": ep_num,
                        "mapping_source": "anibridge_reverse",
                        "fallback_used": False,
                    }
                if s_prov in ("tmdb_show", "tvdb_show") and str(s_id).isdigit():
                    season_num = parse_season_scope(s_scope)
                    if season_num is None:
                        season_num = 1
                    resolved_tmdb = int(s_id) if s_prov == "tmdb_show" else (known_tmdb_id or self._resolve_tvdb_to_tmdb(int(s_id)))
                    return {
                        "success": True,
                        "anilist_id": al_id,
                        "mal_id": mal_id,
                        "tmdb_id": resolved_tmdb,
                        "tvdb_id": int(s_id) if s_prov == "tvdb_show" else tvdb_id,
                        "imdb_id": imdb_id,
                        "media_type": "episode",
                        "season": season_num,
                        "episode": int(mapped_ep),
                        "absolute_episode": ep_num,
                        "mapping_source": "anibridge_reverse",
                        "fallback_used": False,
                    }

        # 4. Graceful Fallback: 1:1 Default Mapping (Season 1, Episode N or Movie)
        resolved_tmdb = known_tmdb_id or (self._resolve_tvdb_to_tmdb(tvdb_id) if tvdb_id else None)
        is_movie = anime_type == "MOVIE" or identity.get("tmdb_media_type") == "movie"
        log(
            f"[AnimeBridge] No explicit AniBridge range rule for anilist:{al_id} ep {ep_num}; "
            f"falling back to 1:1 default (tmdb_id={resolved_tmdb}, is_movie={is_movie}).",
            level=LOGWARNING,
        )
        if is_movie:
            return {
                "success": True,
                "anilist_id": al_id,
                "mal_id": mal_id,
                "tmdb_id": resolved_tmdb,
                "tvdb_id": tvdb_id,
                "imdb_id": imdb_id,
                "media_type": "movie",
                "season": None,
                "episode": None,
                "absolute_episode": ep_num,
                "mapping_source": "fallback_1to1",
                "fallback_used": True,
            }
        return {
            "success": True,
            "anilist_id": al_id,
            "mal_id": mal_id,
            "tmdb_id": resolved_tmdb,
            "tvdb_id": tvdb_id,
            "imdb_id": imdb_id,
            "media_type": "episode",
            "season": 1,
            "episode": ep_num,
            "absolute_episode": ep_num,
            "mapping_source": "fallback_1to1",
            "fallback_used": True,
        }

    def resolve_western_to_anime(
        self,
        tmdb_id: int,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        media_type: str = "tv",
    ) -> Optional[Dict[str, Any]]:
        """
        Translates Western `(tmdb_id, season, episode)` coordinates into Anime `(anilist_id, absolute_episode)`:
          `{
              "success": bool,
              "anilist_id": int,
              "mal_id": int | None,
              "tmdb_id": int,
              "season": int | None,
              "episode": int | None,
              "absolute_episode": int,
              "title": str | None,
              "title_romaji": str | None,
              "mapping_source": "custom_override" | "anibridge" | "fallback_1to1"
          }`
        Returns None if the `tmdb_id` does not correspond to any known anime entry.
        """
        if not tmdb_id:
            return None
        t_id = int(tmdb_id)
        is_movie = str(media_type or "").lower() == "movie" or season is None
        s_num = int(season) if season is not None else None
        e_num = int(episode) if episode is not None else 1

        src_desc = f"tmdb_movie:{t_id}" if is_movie else f"tmdb_show:{t_id}:s{s_num}"

        with db_connect(self.anime_db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 1. Check Custom Overrides first
            cur.execute(
                """
                SELECT * FROM anime_custom_overrides
                WHERE source_descriptor = ? OR target_descriptor = ? OR (tmdb_id = ? AND (season = ? OR season IS NULL))
                ORDER BY updated_at DESC
                """,
                (src_desc, src_desc, t_id, s_num),
            )
            for ov in cur.fetchall():
                try:
                    rm = json.loads(ov["range_map_json"] or "{}")
                except Exception:
                    rm = {}

                if ov["source_descriptor"] == src_desc:
                    other_desc = ov["target_descriptor"]
                    abs_ep = evaluate_range_map(rm, e_num)
                elif ov["target_descriptor"] == src_desc:
                    other_desc = ov["source_descriptor"]
                    abs_ep = evaluate_reverse_range_map(rm, e_num)
                else:
                    continue

                if abs_ep is None:
                    continue
                o_prov, o_id, _ = parse_descriptor(other_desc)
                if o_prov == "anilist" and o_id.isdigit():
                    al_id = int(o_id)
                    ident = self.get_identity(anilist_id=al_id) or {}
                    return {
                        "success": True,
                        "anilist_id": al_id,
                        "mal_id": ident.get("mal_id"),
                        "tmdb_id": t_id,
                        "season": s_num,
                        "episode": None if is_movie else e_num,
                        "absolute_episode": int(abs_ep),
                        "title": ident.get("title"),
                        "title_romaji": ident.get("title_romaji"),
                        "mapping_source": "custom_override",
                    }

            # 2. Check direct AniBridge rules: tmdb_show:{id}:s{season} -> anilist:{id}
            cur.execute(
                """
                SELECT * FROM anime_episode_mappings
                WHERE source_descriptor = ? AND target_provider = 'anilist'
                """,
                (src_desc,),
            )
            for rule in cur.fetchall():
                try:
                    rm = json.loads(rule["range_map_json"] or "{}")
                except Exception:
                    rm = {}
                abs_ep = evaluate_range_map(rm, e_num)
                if abs_ep is not None and str(rule["target_id"]).isdigit():
                    al_id = int(rule["target_id"])
                    ident = self.get_identity(anilist_id=al_id) or {}
                    return {
                        "success": True,
                        "anilist_id": al_id,
                        "mal_id": ident.get("mal_id"),
                        "tmdb_id": t_id,
                        "season": s_num,
                        "episode": None if is_movie else e_num,
                        "absolute_episode": int(abs_ep),
                        "title": ident.get("title"),
                        "title_romaji": ident.get("title_romaji"),
                        "mapping_source": "anibridge",
                    }

            # 3. Check reverse AniBridge rules: anilist:{id} -> tmdb_show:{id}:s{season}
            cur.execute(
                """
                SELECT * FROM anime_episode_mappings
                WHERE target_descriptor = ? AND source_provider = 'anilist'
                """,
                (src_desc,),
            )
            for rule in cur.fetchall():
                try:
                    rm = json.loads(rule["range_map_json"] or "{}")
                except Exception:
                    rm = {}
                abs_ep = evaluate_reverse_range_map(rm, e_num)
                if abs_ep is not None and str(rule["source_id"]).isdigit():
                    al_id = int(rule["source_id"])
                    ident = self.get_identity(anilist_id=al_id) or {}
                    return {
                        "success": True,
                        "anilist_id": al_id,
                        "mal_id": ident.get("mal_id"),
                        "tmdb_id": t_id,
                        "season": s_num,
                        "episode": None if is_movie else e_num,
                        "absolute_episode": int(abs_ep),
                        "title": ident.get("title"),
                        "title_romaji": ident.get("title_romaji"),
                        "mapping_source": "anibridge_reverse",
                    }

            # 4. Fallback: check if tmdb_id exists in anime_identities (for Season 1 or Movie)
            if is_movie or s_num in (1, None):
                cur.execute(
                    "SELECT * FROM anime_identities WHERE tmdb_id = ? ORDER BY anilist_id ASC LIMIT 1",
                    (t_id,),
                )
                ident_row = cur.fetchone()
                if ident_row:
                    ident = dict(ident_row)
                    return {
                        "success": True,
                        "anilist_id": int(ident["anilist_id"]),
                        "mal_id": ident.get("mal_id"),
                        "tmdb_id": t_id,
                        "season": s_num,
                        "episode": None if is_movie else e_num,
                        "absolute_episode": 1 if is_movie else e_num,
                        "title": ident.get("title"),
                        "title_romaji": ident.get("title_romaji"),
                        "mapping_source": "fallback_1to1",
                    }

        return None

    def get_search_aliases(
        self,
        media_identifier: Union[int, str, Dict[str, Any]],
        episode_num: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Returns normalized and categorized lists of Romaji, English, Native (Kanji/Kana),
        and alternate titles for debrid/indexer query generation.

        `media_identifier` can be:
          - An integer `anilist_id` (e.g. `154587`)
          - A descriptor string (`'anilist:154587'`, `'mal:52991'`, `'tmdb:209867'`, `'tvdb:424536'`)
          - A dict containing `anilist_id`, `mal_id`, `tmdb_id`, or `tvdb_id`.
        """
        anilist_id: Optional[int] = None
        mal_id: Optional[int] = None
        tmdb_id: Optional[int] = None
        tvdb_id: Optional[int] = None

        if isinstance(media_identifier, int):
            anilist_id = media_identifier
        elif isinstance(media_identifier, str):
            s = media_identifier.strip()
            if ":" in s:
                prov, val, _ = parse_descriptor(s)
                if val.isdigit():
                    v_int = int(val)
                    if prov == "anilist":
                        anilist_id = v_int
                    elif prov == "mal":
                        mal_id = v_int
                    elif prov.startswith("tmdb"):
                        tmdb_id = v_int
                    elif prov.startswith("tvdb"):
                        tvdb_id = v_int
            elif s.isdigit():
                anilist_id = int(s)
        elif isinstance(media_identifier, dict):
            if media_identifier.get("anilist_id"):
                anilist_id = int(media_identifier["anilist_id"])
            elif media_identifier.get("mal_id"):
                mal_id = int(media_identifier["mal_id"])
            elif media_identifier.get("tmdb_id"):
                tmdb_id = int(media_identifier["tmdb_id"])
            elif media_identifier.get("tvdb_id"):
                tvdb_id = int(media_identifier["tvdb_id"])

        identity = self.get_identity(
            anilist_id=anilist_id,
            mal_id=mal_id,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
        )

        if not identity:
            return {
                "anilist_id": anilist_id,
                "tmdb_id": tmdb_id,
                "mal_id": mal_id,
                "ids": {
                    "anilist_id": anilist_id,
                    "mal_id": mal_id,
                    "tmdb_id": tmdb_id,
                    "tvdb_id": tvdb_id,
                },
                "romaji": [],
                "english": [],
                "native": [],
                "synonyms": [],
                "all_titles": [],
                "search_queries": [],
            }

        al_id = int(identity["anilist_id"])
        romaji_list: List[str] = []
        english_list: List[str] = []
        native_list: List[str] = []
        synonym_list: List[str] = []

        def _append_unique(target: List[str], val: Optional[str]):
            if not val:
                return
            v = str(val).strip()
            if v and v not in target:
                target.append(v)

        if identity.get("title_romaji"):
            _append_unique(romaji_list, identity["title_romaji"])
        if identity.get("title") and not _CJK_RE.search(str(identity["title"])):
            _append_unique(romaji_list, identity["title"])
        if identity.get("title_english"):
            _append_unique(english_list, identity["title_english"])
        if identity.get("title_native"):
            _append_unique(native_list, identity["title_native"])

        with db_connect(self.anime_db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT alias, alias_type FROM anime_aliases WHERE anilist_id = ?", (al_id,))
            for r in cur.fetchall():
                a_str = r["alias"]
                a_type = r["alias_type"]
                if a_type == "romaji":
                    _append_unique(romaji_list, a_str)
                elif a_type == "english":
                    _append_unique(english_list, a_str)
                elif a_type == "native":
                    _append_unique(native_list, a_str)
                else:
                    _append_unique(synonym_list, a_str)

        # Build prioritized deduplicated title list (Romaji -> English -> clean non-CJK synonyms)
        all_titles: List[str] = []
        for lst in (romaji_list, english_list, synonym_list):
            for t in lst:
                cleaned = clean_scraper_query_title(t)
                if len(cleaned) >= 2 and cleaned not in all_titles:
                    all_titles.append(cleaned)

        # Generate ready-to-use scraper queries if an episode number is provided
        search_queries: List[str] = []
        if episode_num is not None:
            ep = int(episode_num)
            ep_pad2 = f"{ep:02d}"
            ep_pad3 = f"{ep:03d}"
            western = self.resolve_anime_to_western(al_id, ep)
            s_ep_tag = None
            if western and western.get("season") is not None and western.get("episode") is not None:
                s_ep_tag = f"S{int(western['season']):02d}E{int(western['episode']):02d}"

            for base_title in all_titles[:4]:
                for q in (
                    f"{base_title} - {ep_pad2}",
                    f"{base_title} {ep_pad2}",
                    f"{base_title} {ep_pad3}" if ep >= 100 else None,
                    f"{base_title} {s_ep_tag}" if s_ep_tag else None,
                ):
                    if q and q not in search_queries:
                        search_queries.append(q)
        else:
            search_queries = all_titles[:6]

        return {
            "anilist_id": al_id,
            "mal_id": identity.get("mal_id"),
            "tmdb_id": identity.get("tmdb_id"),
            "tvdb_id": identity.get("tvdb_id"),
            "imdb_id": identity.get("imdb_id"),
            "ids": {
                "anilist_id": al_id,
                "mal_id": identity.get("mal_id"),
                "anidb_id": identity.get("anidb_id"),
                "kitsu_id": identity.get("kitsu_id"),
                "simkl_id": identity.get("simkl_id"),
                "tmdb_id": identity.get("tmdb_id"),
                "tvdb_id": identity.get("tvdb_id"),
                "imdb_id": identity.get("imdb_id"),
            },
            "anime_type": identity.get("anime_type"),
            "episodes": identity.get("episodes"),
            "season_year": identity.get("release_year"),
            "romaji": romaji_list,
            "english": english_list,
            "native": native_list,
            "synonyms": synonym_list,
            "all_titles": all_titles,
            "search_queries": search_queries,
        }

    def get_sync_status(self) -> Dict[str, Any]:
        """Returns database counts and sync watermarks for Layer 1, Layer 2, and Custom Overrides."""
        with db_connect(self.anime_db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM anime_sync_meta")
            meta = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM anime_identities")
            ident_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM anime_aliases")
            alias_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM anime_episode_mappings")
            rule_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM anime_custom_overrides")
            override_count = cur.fetchone()[0]

        return {
            "identities_count": ident_count,
            "aliases_count": alias_count,
            "episode_mapping_rules_count": rule_count,
            "custom_overrides_count": override_count,
            "manami_last_synced": int(meta.get("manami_last_synced") or 0),
            "manami_upstream_date": meta.get("manami_upstream_date") or "",
            "anibridge_last_synced": int(meta.get("anibridge_last_synced") or 0),
            "anibridge_schema_version": meta.get("anibridge_schema_version") or "",
        }
