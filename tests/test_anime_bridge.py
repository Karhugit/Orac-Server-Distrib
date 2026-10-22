# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "stubs"))
sys.path.insert(0, str(ROOT_DIR))

from resources.lib.db_init import init_anime_db
from resources.lib.anime_service import (
    AnimeCoordinateService,
    compose_range_maps,
    evaluate_range_map,
    evaluate_reverse_range_map,
    ingest_anibridge_mappings,
    ingest_manami_database,
    sync_custom_overrides_file,
    upsert_custom_override,
)
from resources.scrapers import source_utils as scraper_source_utils
from resources.scrapers.modules import source_utils as mod_source_utils


class TestAnimeBridge(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.temp_dir.name, "test_anime_mappings.db")
        init_anime_db(self.db_path)

        # Create synthetic Manami JSONL dataset
        self.manami_path = os.path.join(self.temp_dir.name, "manami.jsonl")
        manami_lines = [
            json.dumps({"$schema": "https://example.com/schema.json", "lastUpdate": "2026-10-01"}),
            # Cour 1 of a split-cour show
            json.dumps({
                "sources": [
                    "https://anilist.co/anime/154587",
                    "https://myanimelist.net/anime/52991",
                    "https://anidb.net/anime/17617",
                    "https://kitsu.app/anime/46474",
                ],
                "title": "Sousou no Frieren",
                "type": "TV",
                "episodes": 16,
                "status": "FINISHED",
                "animeSeason": {"season": "FALL", "year": 2023},
                "synonyms": [
                    "Frieren: Beyond Journey's End",
                    "葬送のフリーレン",
                    "Frieren at the Funeral",
                ],
            }),
            # Cour 2 / Part 2 tracked as a separate AniList entry
            json.dumps({
                "sources": [
                    "https://anilist.co/anime/154588",
                    "https://myanimelist.net/anime/52992",
                    "https://anidb.net/anime/17618",
                ],
                "title": "Sousou no Frieren Part 2",
                "type": "TV",
                "episodes": 12,
                "status": "FINISHED",
                "animeSeason": {"season": "WINTER", "year": 2024},
                "synonyms": [
                    "Frieren: Beyond Journey's End Part 2",
                    "葬送のフリーレン 第2クール",
                ],
            }),
            # Canon Movie entry
            json.dumps({
                "sources": [
                    "https://anilist.co/anime/131573",
                    "https://myanimelist.net/anime/48561",
                    "https://anidb.net/anime/16138",
                ],
                "title": "Jujutsu Kaisen 0 Movie",
                "type": "MOVIE",
                "episodes": 1,
                "status": "FINISHED",
                "animeSeason": {"season": "WINTER", "year": 2021},
                "synonyms": ["Gekijouban Jujutsu Kaisen 0", "劇場版 呪術廻戦 0"],
            }),
            # OVA entry mapped to Season 0 Specials
            json.dumps({
                "sources": [
                    "https://anilist.co/anime/99901",
                    "https://myanimelist.net/anime/99901",
                ],
                "title": "Sousou no Frieren OVA",
                "type": "OVA",
                "episodes": 2,
                "status": "FINISHED",
                "animeSeason": {"season": "SPRING", "year": 2024},
                "synonyms": ["Frieren Specials"],
            }),
            # Unmapped anime entry (has no AniBridge rule, tests 1:1 fallback)
            json.dumps({
                "sources": [
                    "https://anilist.co/anime/88888",
                    "https://myanimelist.net/anime/88888",
                ],
                "title": "Michi no Anime",
                "type": "TV",
                "episodes": 12,
                "status": "ONGOING",
                "animeSeason": {"season": "FALL", "year": 2026},
                "synonyms": ["Unknown Anime Adventures"],
            }),
        ]
        with open(self.manami_path, "w", encoding="utf-8") as f:
            f.write("\n".join(manami_lines) + "\n")

        # Create synthetic AniBridge v3 + v2 mappings JSON
        self.anibridge_path = os.path.join(self.temp_dir.name, "anibridge.json")
        anibridge_data = {
            "$schema": "https://raw.githubusercontent.com/anibridge/anibridge-mappings/main/schema.json",
            # Pivot via anidb:17617:R -> anilist:154587 and tmdb_show:209867:s1 (Cour 1: eps 1-16)
            "anidb:17617:R": {
                "anilist:154587": {"1-16": "1-16"},
                "tmdb_show:209867:s1": {"1-16": "1-16"},
                "tvdb_show:424536:s1": {"1-16": "1-16"},
                "imdb:tt22248376": {"1-16": "1-16"},
            },
            # Split cour 2: anilist:154588 eps 1-12 -> tmdb_show:209867:s1 eps 17-28
            "anilist:154588": {
                "tmdb_show:209867:s1": {"1-12": "17-28"},
                "tvdb_show:424536:s1": {"1-12": "17-28"},
            },
            # Movie mapping: anilist:131573 -> tmdb_movie:810693
            "anilist:131573": {
                "tmdb_movie:810693": {"1": "1"},
            },
            # OVA mapping to Season 0 specials: anilist:99901 -> tmdb_show:209867:s0
            "anilist:99901": {
                "tmdb_show:209867:s0": {"1-2": "1-2"},
            },
        }
        with open(self.anibridge_path, "w", encoding="utf-8") as f:
            json.dump(anibridge_data, f)

        ingest_manami_database(db_path=self.db_path, source_path_or_url=self.manami_path)
        ingest_anibridge_mappings(db_path=self.db_path, source_path_or_url=self.anibridge_path)
        self.service = AnimeCoordinateService(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_range_engine_expressions(self):
        # Standard offset range
        rmap = {"1-12": "13-24"}
        self.assertEqual(evaluate_range_map(rmap, 1), 13)
        self.assertEqual(evaluate_range_map(rmap, 12), 24)
        self.assertEqual(evaluate_reverse_range_map(rmap, 13), 1)
        self.assertEqual(evaluate_reverse_range_map(rmap, 24), 12)

        # Open-ended range
        rmap_open = {"1-": "29-"}
        self.assertEqual(evaluate_range_map(rmap_open, 5), 33)
        self.assertEqual(evaluate_reverse_range_map(rmap_open, 33), 5)

        # Comma-separated non-contiguous target segments
        rmap_comma = {"1-4": "1-2,5-6"}
        self.assertEqual(evaluate_range_map(rmap_comma, 1), 1)
        self.assertEqual(evaluate_range_map(rmap_comma, 2), 2)
        self.assertEqual(evaluate_range_map(rmap_comma, 3), 5)
        self.assertEqual(evaluate_range_map(rmap_comma, 4), 6)
        self.assertEqual(evaluate_reverse_range_map(rmap_comma, 5), 3)

        # Ratio multiplier (1 source ep = 2 target eps)
        rmap_ratio = {"1-2": "1-4|2"}
        self.assertEqual(evaluate_range_map(rmap_ratio, 1), 1)
        self.assertEqual(evaluate_range_map(rmap_ratio, 2), 3)
        self.assertEqual(evaluate_reverse_range_map(rmap_ratio, 4), 2)

        # Composition across pivot
        composed = compose_range_maps({"1-12": "1-12"}, {"1-12": "13-24"})
        self.assertEqual(evaluate_range_map(composed, 1), 13)
        self.assertEqual(evaluate_range_map(composed, 12), 24)

    def test_split_cour_resolution_bidirectional(self):
        # Cour 1: anilist 154587 ep 5 -> tmdb 209867 S01E05
        res1 = self.service.resolve_anime_to_western(154587, 5)
        self.assertTrue(res1["success"])
        self.assertEqual(res1["tmdb_id"], 209867)
        self.assertEqual(res1["season"], 1)
        self.assertEqual(res1["episode"], 5)

        # Cour 2: anilist 154588 ep 3 -> tmdb 209867 S01E19
        res2 = self.service.resolve_anime_to_western(154588, 3)
        self.assertTrue(res2["success"])
        self.assertEqual(res2["tmdb_id"], 209867)
        self.assertEqual(res2["season"], 1)
        self.assertEqual(res2["episode"], 19)

        # Reverse: tmdb 209867 S01E19 -> anilist 154588 ep 3
        rev2 = self.service.resolve_western_to_anime(209867, 1, 19)
        self.assertTrue(rev2["success"])
        self.assertEqual(rev2["anilist_id"], 154588)
        self.assertEqual(rev2["absolute_episode"], 3)

        # Reverse: tmdb 209867 S01E05 -> anilist 154587 ep 5
        rev1 = self.service.resolve_western_to_anime(209867, 1, 5)
        self.assertTrue(rev1["success"])
        self.assertEqual(rev1["anilist_id"], 154587)
        self.assertEqual(rev1["absolute_episode"], 5)

    def test_movies_and_ovas(self):
        # Movie forward
        m_fwd = self.service.resolve_anime_to_western(131573, 1)
        self.assertTrue(m_fwd["success"])
        self.assertEqual(m_fwd["tmdb_id"], 810693)
        self.assertIsNone(m_fwd["season"])
        self.assertEqual(m_fwd["media_type"], "movie")

        # Movie reverse
        m_rev = self.service.resolve_western_to_anime(810693, None, 1, media_type="movie")
        self.assertTrue(m_rev["success"])
        self.assertEqual(m_rev["anilist_id"], 131573)
        self.assertEqual(m_rev["absolute_episode"], 1)

        # OVA forward (maps to Season 0 Special S00E02)
        ova_fwd = self.service.resolve_anime_to_western(99901, 2)
        self.assertTrue(ova_fwd["success"])
        self.assertEqual(ova_fwd["tmdb_id"], 209867)
        self.assertEqual(ova_fwd["season"], 0)
        self.assertEqual(ova_fwd["episode"], 2)

        # OVA reverse (S00E02 -> anilist 99901 ep 2)
        ova_rev = self.service.resolve_western_to_anime(209867, 0, 2)
        self.assertTrue(ova_rev["success"])
        self.assertEqual(ova_rev["anilist_id"], 99901)
        self.assertEqual(ova_rev["absolute_episode"], 2)

    def test_missing_mappings_graceful_fallback(self):
        # Unmapped anime 88888 falls back to Season 1, Episode 7 without raising an error
        fb = self.service.resolve_anime_to_western(88888, 7)
        self.assertEqual(fb["season"], 1)
        self.assertEqual(fb["episode"], 7)
        self.assertTrue(fb["fallback_used"])

    def test_custom_overrides_priority(self):
        # Override Cour 2 (anilist 154588) to map to Season 2 (e.g. local override preference)
        upsert_custom_override(
            {
                "anilist_id": 154588,
                "tmdb_id": 209867,
                "target_provider": "tmdb_show",
                "target_season": 2,
                "range_mapping": {"1-12": "1-12"},
                "notes": "Custom Season 2 override",
            },
            db_path=self.db_path,
        )
        res = self.service.resolve_anime_to_western(154588, 4)
        self.assertTrue(res["success"])
        self.assertEqual(res["mapping_source"], "custom_override")
        self.assertEqual(res["season"], 2)
        self.assertEqual(res["episode"], 4)

        rev = self.service.resolve_western_to_anime(209867, 2, 4)
        self.assertTrue(rev["success"])
        self.assertEqual(rev["mapping_source"], "custom_override")
        self.assertEqual(rev["anilist_id"], 154588)
        self.assertEqual(rev["absolute_episode"], 4)

    def test_get_search_aliases_and_scraper_matching(self):
        aliases_data = self.service.get_search_aliases(154587, episode_num=5)
        self.assertIn("Sousou no Frieren", aliases_data["romaji"])
        self.assertIn("Frieren: Beyond Journey's End", aliases_data["english"])
        self.assertIn("葬送のフリーレン", aliases_data["native"])
        self.assertTrue(any("Sousou no Frieren 05" in q for q in aliases_data["search_queries"]))

        # Verify source_utils.check_title matches SubsPlease / Erai-raws Romaji + absolute episode releases
        release_raw = "[SubsPlease] Sousou no Frieren - 19 (1080p) [A1B2C3D4].mkv"
        cleaned = scraper_source_utils.clean_name(release_raw)
        anime_aliases = [
            {"title": "Sousou no Frieren", "country": "jp", "is_anime": True, "abs_ep": 19},
            {"title": "Frieren: Beyond Journey's End", "country": "us", "is_anime": True, "abs_ep": 19},
        ]
        self.assertTrue(
            scraper_source_utils.check_title(
                "Frieren: Beyond Journey's End",
                anime_aliases,
                cleaned,
                "S02E03",
                "2023",
            )
        )
        self.assertTrue(
            mod_source_utils.check_title(
                "Frieren: Beyond Journey's End",
                anime_aliases,
                cleaned,
                "S02E03",
                "2023",
            )
        )

    def test_custom_overrides_file_ingestion(self):
        ov_file = os.path.join(self.temp_dir.name, "anime_overrides.json")
        with open(ov_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "overrides": [
                        {
                            "anilist_id": 88888,
                            "tmdb_id": 555123,
                            "season": 3,
                            "range_map": {"1-12": "25-36"},
                            "notes": "File-based custom override test",
                        }
                    ]
                },
                f,
            )
        sync_res = sync_custom_overrides_file(db_path=self.db_path, overrides_file_path=ov_file)
        self.assertEqual(sync_res["overrides_loaded"], 1)

        fwd = self.service.resolve_anime_to_western(88888, 2)
        self.assertTrue(fwd["success"])
        self.assertEqual(fwd["tmdb_id"], 555123)
        self.assertEqual(fwd["season"], 3)
        self.assertEqual(fwd["episode"], 26)
        self.assertEqual(fwd["mapping_source"], "custom_override")


if __name__ == "__main__":
    unittest.main()
