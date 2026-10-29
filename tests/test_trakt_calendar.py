import unittest
from unittest.mock import MagicMock
import json
from datetime import datetime, timezone

from resources.lib.trakt_calendar_handler import (
    TRAKT_CALENDAR_PRESETS,
    normalize_preset_key,
    convert_rating_to_trakt_scale,
    handle_trakt_calendar_request,
    get_trakt_genres
)
from resources.lib.discover_handler import handle_discover_request


class TestTraktCalendar(unittest.TestCase):

    def test_presets_coverage(self):
        """Ensure all 11 presets are present in TRAKT_CALENDAR_PRESETS."""
        expected_presets = [
            'new_shows', 'season_premieres', 'finales', 'movies',
            'streaming_releases', 'dvd_releases', 'media',
            'hot_releases', 'hot_premieres', 'hot_finales', 'hot_new_shows'
        ]
        for preset in expected_presets:
            self.assertIn(preset, TRAKT_CALENDAR_PRESETS)

    def test_normalize_preset_key(self):
        self.assertEqual(normalize_preset_key('new shows'), 'new_shows')
        self.assertEqual(normalize_preset_key('season-premieres'), 'season_premieres')
        self.assertEqual(normalize_preset_key('hot finales'), 'hot_finales')
        self.assertEqual(normalize_preset_key('streaming releases'), 'streaming_releases')
        self.assertEqual(normalize_preset_key(''), 'hot_releases')

    def test_convert_rating_to_trakt_scale(self):
        self.assertEqual(convert_rating_to_trakt_scale('7.5'), '75-100')
        self.assertEqual(convert_rating_to_trakt_scale('8'), '80-100')
        self.assertEqual(convert_rating_to_trakt_scale(7.0), '70-100')
        self.assertEqual(convert_rating_to_trakt_scale('70-90'), '70-90')
        self.assertIsNone(convert_rating_to_trakt_scale(''))

    def test_handle_trakt_calendar_shows(self):
        mock_trakt = MagicMock()
        mock_trakt.is_authenticated.return_value = True

        fake_api_response = [
            {
                "first_aired": "2026-10-27T20:00:00.000Z",
                "episode": {
                    "season": 3,
                    "number": 1,
                    "title": "Season 3 Premiere",
                    "rating": 8.4,
                    "votes": 520,
                    "overview": "Exciting new episode"
                },
                "show": {
                    "title": "Cosmic Voyage",
                    "year": 2024,
                    "ids": {
                        "trakt": 12345,
                        "tmdb": 67890,
                        "imdb": "tt9999999"
                    },
                    "images": {
                        "poster": ["media.trakt.tv/images/shows/poster1.jpg.webp"],
                        "fanart": ["media.trakt.tv/images/shows/fanart1.jpg.webp"]
                    },
                    "genres": ["science-fiction", "drama"]
                }
            },
            # Second episode of same show in same window -> should be deduplicated
            {
                "first_aired": "2026-10-28T20:00:00.000Z",
                "episode": {
                    "season": 3,
                    "number": 2,
                    "title": "Episode 2"
                },
                "show": {
                    "title": "Cosmic Voyage",
                    "year": 2024,
                    "ids": {
                        "trakt": 12345,
                        "tmdb": 67890
                    }
                }
            }
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_api_response
        mock_trakt.get.return_value = mock_resp
        mock_trakt._get.return_value = mock_resp

        status, body, content_type = handle_trakt_calendar_request(
            {
                'preset': 'season_premieres',
                'target': 'all',
                'start_date': 'T-0',
                'days': '14',
                'ratings': '7.5'
            },
            mock_trakt
        )

        self.assertEqual(status, 200)
        self.assertEqual(content_type, 'application/json')
        items = json.loads(body)
        self.assertEqual(len(items), 1)  # Deduplicated

        item = items[0]
        self.assertEqual(item['title'], 'Cosmic Voyage')
        self.assertEqual(item['media_type'], 'tv')
        self.assertEqual(item['tmdb_id'], 67890)
        self.assertEqual(item['trakt_id'], 12345)
        self.assertEqual(item['poster_path'], 'https://media.trakt.tv/images/shows/poster1.jpg.webp')
        self.assertEqual(item['episode']['season'], 3)
        self.assertEqual(item['episode']['number'], 1)
        self.assertEqual(item['rating'], 8.4)
        self.assertEqual(item['source'], 'trakt')

    def test_handle_trakt_calendar_movies(self):
        mock_trakt = MagicMock()
        mock_trakt.is_authenticated.return_value = True

        fake_api_response = [
            {
                "released": "2026-10-27",
                "movie": {
                    "title": "Galaxy Quest II",
                    "year": 2026,
                    "rating": 7.9,
                    "votes": 1200,
                    "overview": "Never surrender!",
                    "ids": {
                        "trakt": 54321,
                        "tmdb": 98765
                    },
                    "images": {
                        "poster": ["media.trakt.tv/images/movies/poster.jpg.webp"]
                    },
                    "genres": ["comedy", "adventure"]
                }
            }
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_api_response
        mock_trakt.get.return_value = mock_resp
        mock_trakt._get.return_value = mock_resp

        status, body, content_type = handle_trakt_calendar_request(
            {
                'preset': 'streaming_releases',
                'target': 'all',
                'days': '7'
            },
            mock_trakt
        )

        self.assertEqual(status, 200)
        items = json.loads(body)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['media_type'], 'movie')
        self.assertEqual(items[0]['title'], 'Galaxy Quest II')
        self.assertEqual(items[0]['tmdb_id'], 98765)
        self.assertEqual(items[0]['trakt_id'], 54321)
        self.assertEqual(items[0]['source'], 'trakt')

    def test_my_calendar_unauthenticated_guard(self):
        mock_trakt = MagicMock()
        mock_trakt.is_authenticated.return_value = False

        status, body, _ = handle_trakt_calendar_request(
            {
                'preset': 'season_premieres',
                'target': 'my'
            },
            mock_trakt
        )
        self.assertEqual(status, 401)
        data = json.loads(body)
        self.assertIn('Authentication required', data['error'])

    def test_discover_handler_routing(self):
        mock_trakt = MagicMock()
        mock_trakt.is_authenticated.return_value = True

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_trakt.get.return_value = mock_resp
        mock_trakt._get.return_value = mock_resp

        # Calling with item_type='trakt'
        status, body, _ = handle_discover_request(
            'trakt',
            {'preset': 'hot_releases'},
            MagicMock(),
            None,
            trakt_handler=mock_trakt
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, '[]')

    def test_target_media_type_filter(self):
        mock_trakt = MagicMock()
        mock_trakt.is_authenticated.return_value = True

        fake_mixed_response = [
            {
                "show": {"title": "Show A", "ids": {"trakt": 1, "tmdb": 11}},
                "episode": {"season": 1, "number": 1, "title": "Pilot"}
            },
            {
                "movie": {"title": "Movie B", "ids": {"trakt": 2, "tmdb": 22}}
            }
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_mixed_response
        mock_trakt.get.return_value = mock_resp
        mock_trakt._get.return_value = mock_resp

        # Filter tv
        _, body_tv, _ = handle_trakt_calendar_request(
            {'preset': 'hot_releases'}, mock_trakt, target_media_type='tv'
        )
        items_tv = json.loads(body_tv)
        self.assertEqual(len(items_tv), 1)
        self.assertEqual(items_tv[0]['media_type'], 'tv')

        # Filter movie
        _, body_movie, _ = handle_trakt_calendar_request(
            {'preset': 'hot_releases'}, mock_trakt, target_media_type='movie'
        )
        items_movie = json.loads(body_movie)
        self.assertEqual(len(items_movie), 1)
        self.assertEqual(items_movie[0]['media_type'], 'movie')


if __name__ == '__main__':
    unittest.main()
