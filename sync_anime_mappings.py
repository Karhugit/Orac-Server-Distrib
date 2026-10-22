#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI Updater & Diagnostic Utility for Orac Server's Anime Dual-Indexing Bridge.

Usage examples:
  python sync_anime_mappings.py                        # Download & ingest Manami + AniBridge + local overrides
  python sync_anime_mappings.py --status               # Show current database counts & sync timestamps
  python sync_anime_mappings.py --overrides-only       # Reload local anime_overrides.json / .yaml only
  python sync_anime_mappings.py --resolve-anime 154587 1
  python sync_anime_mappings.py --resolve-western 209867 1 1
  python sync_anime_mappings.py --aliases 154587
"""

import argparse
import json
import os
import sys
from pathlib import Path

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(script_dir, "stubs"))
sys.path.insert(0, script_dir)

from resources.lib.config_loader import ConfigLoader
from resources.lib.db_init import init_anime_db
from resources.lib.anime_service import (
    AnimeCoordinateService,
    ingest_manami_database,
    ingest_anibridge_mappings,
    sync_custom_overrides_file,
    sync_all_anime_datasets,
)


def main():
    parser = argparse.ArgumentParser(
        description="Orac Server Anime Dual-Indexing Bridge Ingestion & Diagnostic CLI"
    )
    parser.add_argument("--db", default=None, help="Path to anime_mappings.db")
    parser.add_argument("--status", action="store_true", help="Print dataset sync status and counts")
    parser.add_argument("--manami-only", action="store_true", help="Sync only Layer 1 (Manami offline database)")
    parser.add_argument("--anibridge-only", action="store_true", help="Sync only Layer 2 (AniBridge episode mappings)")
    parser.add_argument("--overrides-only", action="store_true", help="Sync only local custom overrides file")
    parser.add_argument("--overrides-file", default=None, help="Path to custom anime_overrides.json or .yaml")
    parser.add_argument(
        "--resolve-anime",
        nargs=2,
        metavar=("ANILIST_ID", "EPISODE"),
        help="Resolve (anilist_id, episode) -> Western (tmdb_id, season, episode)",
    )
    parser.add_argument(
        "--resolve-western",
        nargs=3,
        metavar=("TMDB_ID", "SEASON", "EPISODE"),
        help="Resolve (tmdb_id, season, episode) -> Anime (anilist_id, absolute_episode)",
    )
    parser.add_argument(
        "--aliases",
        metavar="IDENTIFIER",
        help="Fetch normalized Romaji, English, Native, and synonym aliases for an ID or title",
    )

    args = parser.parse_args()
    cl = ConfigLoader()
    db_path = args.db or cl.db_paths.get("anime_mappings") or str(Path(script_dir) / "anime_mappings.db")
    init_anime_db(db_path)
    service = AnimeCoordinateService(db_path=db_path)

    if args.status:
        print(json.dumps(service.get_sync_status(), indent=2))
        return

    if args.resolve_anime:
        al_id, ep = int(args.resolve_anime[0]), int(args.resolve_anime[1])
        print(json.dumps(service.resolve_anime_to_western(al_id, ep), indent=2, ensure_ascii=False))
        return

    if args.resolve_western:
        tm_id = int(args.resolve_western[0])
        season = None if args.resolve_western[1].lower() == "none" else int(args.resolve_western[1])
        ep = int(args.resolve_western[2])
        mtype = "movie" if season is None else "show"
        print(json.dumps(service.resolve_western_to_anime(tm_id, season, ep, media_type=mtype), indent=2, ensure_ascii=False))
        return

    if args.aliases:
        ident = int(args.aliases) if args.aliases.isdigit() else args.aliases
        print(json.dumps(service.get_search_aliases(ident), indent=2, ensure_ascii=False))
        return

    if args.overrides_only:
        res = sync_custom_overrides_file(db_path=db_path, file_path=args.overrides_file)
        print(json.dumps(res, indent=2))
        return

    if args.manami_only:
        res = ingest_manami_database(db_path=db_path)
        print(json.dumps(res, indent=2))
        return

    if args.anibridge_only:
        res = ingest_anibridge_mappings(db_path=db_path)
        print(json.dumps(res, indent=2))
        return

    print(f"Syncing all Anime Dual-Indexing datasets into {db_path} ...")
    res = sync_all_anime_datasets(db_path=db_path, overrides_path=args.overrides_file)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
