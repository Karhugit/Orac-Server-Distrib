"""
compare_trending.py  –  Temporary comparison script
=====================================================
Fetches:
  • Trakt  – "Trending Movies" (sorted by current watcher count)
  • TMDB   – "Popular Movies" via /discover sorted by popularity, released within the last 7 days
              (closest TMDB equivalent to "trending now")

Run from the orac_server directory:
    python compare_trending.py
or with a different page size:
    python compare_trending.py --limit 30
"""

import sys
import os
import json
import argparse
import requests
from datetime import datetime, timedelta

# ── Bootstrap: make sure the stubs (Kodi shims) are on the path ──────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "stubs"))

# ── Load credentials from config.json ─────────────────────────────────────────
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
with open(CONFIG_PATH, "r") as f:
    _cfg = json.load(f)

TRAKT_CLIENT_ID = _cfg["TRAKT"]["client_id"]
TMDB_API_KEY    = _cfg["TMDB"]["api_key"]

# ─────────────────────────────────────────────────────────────────────────────
# Trakt helpers
# ─────────────────────────────────────────────────────────────────────────────

TRAKT_BASE = "https://api.trakt.tv"
TRAKT_HEADERS = {
    "Content-Type": "application/json",
    "trakt-api-version": "2",
    "trakt-api-key": TRAKT_CLIENT_ID,
}


def trakt_get_trending_movies(limit=40):
    """
    GET /movies/trending  – movies being watched RIGHT NOW (sorted by watcher count).
    Returns a list of dicts: {rank, watchers, title, year, trakt_id, imdb_id, tmdb_id}
    """
    params = {"limit": limit}
    resp = requests.get(f"{TRAKT_BASE}/movies/trending", headers=TRAKT_HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    results = []
    for rank, entry in enumerate(resp.json(), start=1):
        watchers = entry.get("watchers", 0)
        movie    = entry.get("movie", {})
        ids      = movie.get("ids", {})
        results.append({
            "rank":     rank,
            "watchers": watchers,
            "title":    movie.get("title", "Unknown"),
            "year":     movie.get("year", "?"),
            "trakt_id": ids.get("trakt"),
            "imdb_id":  ids.get("imdb"),
            "tmdb_id":  ids.get("tmdb"),
        })
    return results


# ─────────────────────────────────────────────────────────────────────────────
# TMDB helpers
# ─────────────────────────────────────────────────────────────────────────────

TMDB_BASE = "https://api.themoviedb.org/3"


def tmdb_get(path, params=None):
    p = {"api_key": TMDB_API_KEY, "language": "en-US"}
    if params:
        p.update(params)
    resp = requests.get(f"{TMDB_BASE}{path}", params=p, timeout=15)
    resp.raise_for_status()
    return resp.json()


def tmdb_discover_popular_recent(limit=40):
    """
    /discover/movie sorted by popularity, primary_release_date within the last 7 days.
    This gives the most popular movies that are fresh releases — closest TMDB equivalent
    to "trending this week".

    We also fetch a second page if needed to fill the requested limit.
    """
    today      = datetime.utcnow().date()
    week_ago   = today - timedelta(days=7)
    params = {
        "sort_by":                    "popularity.desc",
        "primary_release_date.gte":   str(week_ago),
        "primary_release_date.lte":   str(today),
        "vote_count.gte":             0,           # include brand-new releases
        "include_adult":              "false",
        "include_video":              "false",
        "page":                       1,
    }
    results = []
    for page in (1, 2):
        params["page"] = page
        data = tmdb_get("/discover/movie", params=params)
        for item in data.get("results", []):
            results.append({
                "rank":       len(results) + 1,
                "popularity": round(item.get("popularity", 0), 1),
                "title":      item.get("title", "Unknown"),
                "year":       (item.get("release_date") or "?")[:4],
                "release":    item.get("release_date", "?"),
                "tmdb_id":    item.get("id"),
                "vote_avg":   item.get("vote_average", 0),
                "vote_count": item.get("vote_count", 0),
            })
            if len(results) >= limit:
                return results
        if data.get("total_pages", 1) < page + 1:
            break
    return results


def tmdb_trending_week(limit=40):
    """
    /trending/movie/week – TMDB's own trending list for the past week.
    Included as an extra column for a 3-way comparison.
    """
    results = []
    for page in (1, 2):
        data = tmdb_get("/trending/movie/week", params={"page": page})
        for item in data.get("results", []):
            results.append({
                "rank":       len(results) + 1,
                "popularity": round(item.get("popularity", 0), 1),
                "title":      item.get("title", "Unknown"),
                "year":       (item.get("release_date") or "?")[:4],
                "tmdb_id":    item.get("id"),
            })
            if len(results) >= limit:
                return results
        if data.get("total_pages", 1) < page + 1:
            break
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Display
# ─────────────────────────────────────────────────────────────────────────────

def col(text, width):
    """Left-pad / truncate to fixed width."""
    s = str(text)
    return s[:width].ljust(width)


def print_comparison(trakt_list, tmdb_discover_list, tmdb_trending_list, limit):
    # Build a quick lookup: TMDB trending rank by tmdb_id
    tmdb_trend_rank = {m["tmdb_id"]: m["rank"] for m in tmdb_trending_list}

    # Build a quick lookup: TMDB discover rank by title (lowered) for cross-referencing
    tmdb_disc_by_id   = {m["tmdb_id"]: m for m in tmdb_discover_list}
    trakt_tmdb_ids    = {m["tmdb_id"] for m in trakt_list}

    W_RANK  = 4
    W_TITLE = 40
    W_YEAR  = 6
    W_WATCH = 10   # Trakt watchers
    W_POP   = 10   # TMDB popularity
    W_VOTE  = 10   # TMDB vote avg
    W_TREND = 8    # TMDB trending rank

    sep = "─" * (W_RANK + W_TITLE + W_YEAR + W_WATCH + 5)

    print()
    print("=" * 80)
    print(f"  [Trakt]  Trending Movies  (top {limit})")
    print("=" * 80)
    print(f"{'#':<{W_RANK}}  {'Title':<{W_TITLE}}  {'Year':<{W_YEAR}}  {'Watchers':>{W_WATCH}}  "
          f"{'TMDB Pop':>{W_POP}}  {'TMDB Rating':>{W_VOTE}}  {'TMDB Trend#':>{W_TREND}}")
    print(sep)
    for m in trakt_list:
        tmdb_m    = tmdb_disc_by_id.get(m["tmdb_id"])
        pop_str   = str(tmdb_m["popularity"])  if tmdb_m else "–"
        vote_str  = str(tmdb_m["vote_avg"])    if tmdb_m else "–"
        trend_str = str(tmdb_trend_rank.get(m["tmdb_id"], "–"))
        print(f"{col(m['rank'], W_RANK)}  {col(m['title'], W_TITLE)}  {col(m['year'], W_YEAR)}  "
              f"{col(m['watchers'], W_WATCH):>{W_WATCH}}  "
              f"{col(pop_str, W_POP):>{W_POP}}  "
              f"{col(vote_str, W_VOTE):>{W_VOTE}}  "
              f"{col(trend_str, W_TREND):>{W_TREND}}")

    print()
    print("=" * 80)
    print(f"  [TMDB Discover]  Popular movies released in last 7 days  (top {limit})")
    print("=" * 80)
    print(f"{'#':<{W_RANK}}  {'Title':<{W_TITLE}}  {'Year':<{W_YEAR}}  {'Released':<10}  "
          f"{'Popularity':>{W_POP}}  {'Rating':>{W_VOTE}}  {'Votes':>{W_VOTE}}")
    print(sep)
    for m in tmdb_discover_list:
        on_trakt = " ← also on Trakt" if m["tmdb_id"] in trakt_tmdb_ids else ""
        print(f"{col(m['rank'], W_RANK)}  {col(m['title'], W_TITLE)}  {col(m['year'], W_YEAR)}  "
              f"{col(m['release'], 10)}  "
              f"{col(m['popularity'], W_POP):>{W_POP}}  "
              f"{col(m['vote_avg'], W_VOTE):>{W_VOTE}}  "
              f"{col(m['vote_count'], W_VOTE):>{W_VOTE}}"
              f"{on_trakt}")

    print()
    print("=" * 80)
    print(f"  [TMDB Trending]  Trending This Week  (top {limit})")
    print("=" * 80)
    print(f"{'#':<{W_RANK}}  {'Title':<{W_TITLE}}  {'Year':<{W_YEAR}}  {'Popularity':>{W_POP}}")
    print(sep)
    for m in tmdb_trending_list:
        on_trakt = " ← also on Trakt" if m["tmdb_id"] in trakt_tmdb_ids else ""
        print(f"{col(m['rank'], W_RANK)}  {col(m['title'], W_TITLE)}  {col(m['year'], W_YEAR)}  "
              f"{col(m['popularity'], W_POP):>{W_POP}}"
              f"{on_trakt}")

    # Overlap summary
    discover_ids   = {m["tmdb_id"] for m in tmdb_discover_list}
    trending_ids   = {m["tmdb_id"] for m in tmdb_trending_list}
    td_overlap     = trakt_tmdb_ids & trending_ids
    disc_overlap   = trakt_tmdb_ids & discover_ids

    print()
    print("-" * 80)
    print(f"  [OK]  Trakt ^ TMDB Trending Week  : {len(td_overlap)} titles in common")
    print(f"  [OK]  Trakt ^ TMDB Discover (7d)  : {len(disc_overlap)} titles in common")
    if td_overlap:
        shared = [m["title"] for m in trakt_list if m["tmdb_id"] in td_overlap]
        print(f"        Shared (Trakt & TMDB Trending): {', '.join(shared)}")
    print("-" * 80)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Compare Trakt vs TMDB trending movies")
    parser.add_argument("--limit", type=int, default=20, help="Number of results to show (default: 20)")
    args = parser.parse_args()
    limit = args.limit

    print(f"\nFetching data (limit={limit}) …")
    print("  → Trakt trending movies …", end=" ", flush=True)
    trakt_list = trakt_get_trending_movies(limit=limit)
    print("done")

    print("  → TMDB discover (popular, last 7 days) …", end=" ", flush=True)
    tmdb_disc = tmdb_discover_popular_recent(limit=limit)
    print("done")

    print("  → TMDB trending this week …", end=" ", flush=True)
    tmdb_trend = tmdb_trending_week(limit=limit)
    print("done")

    print_comparison(trakt_list, tmdb_disc, tmdb_trend, limit)


if __name__ == "__main__":
    main()
