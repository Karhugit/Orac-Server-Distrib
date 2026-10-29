import unittest
from unittest.mock import patch, MagicMock
import json

from resources.lib.anilist_client import fetch_anime_list


class TestAnimeDiscover(unittest.TestCase):

    @patch('resources.lib.anilist_client._graphql_request')
    def test_fetch_anime_list_trending(self, mock_gql):
        mock_gql.return_value = {
            "Page": {
                "pageInfo": {"total": 1, "hasNextPage": False},
                "media": [
                    {
                        "id": 101,
                        "idMal": 201,
                        "title": {"english": "Chainsaw Man", "romaji": "Chainsaw Man"},
                        "format": "TV",
                        "averageScore": 85,
                        "seasonYear": 2022,
                        "description": "Devils and hunters",
                        "coverImage": {"extraLarge": "https://image.jpg"},
                        "bannerImage": "https://banner.jpg",
                        "genres": ["Action", "Supernatural"]
                    }
                ]
            }
        }

        res = fetch_anime_list(list_type="trending", min_rating=8.0, year_gte=2020, year_lte=2025)
        self.assertTrue(res["success"])
        self.assertEqual(res["count"], 1)
        item = res["results"][0]
        self.assertEqual(item["title"], "Chainsaw Man")
        self.assertEqual(item["rating"], 8.5)
        self.assertEqual(item["media_type"], "tvshow")

        # Check variables passed to GraphQL request
        _, kwargs = mock_gql.call_args
        variables = kwargs.get("variables", {})
        self.assertEqual(variables["averageScore_greater"], 80)
        self.assertEqual(variables["startDate_greater"], 20200000)
        self.assertEqual(variables["startDate_lesser"], 20251231)

    @patch('resources.lib.anilist_client._graphql_request')
    def test_fetch_anime_list_movies(self, mock_gql):
        mock_gql.return_value = {
            "Page": {
                "pageInfo": {"total": 1, "hasNextPage": False},
                "media": [
                    {
                        "id": 102,
                        "idMal": 202,
                        "title": {"english": "Spirited Away", "romaji": "Sen to Chihiro no Kamikakushi"},
                        "format": "MOVIE",
                        "averageScore": 90,
                        "seasonYear": 2001,
                        "description": "Spirit world adventure",
                        "coverImage": {"extraLarge": "https://image2.jpg"},
                        "genres": ["Animation", "Fantasy"]
                    }
                ]
            }
        }

        res = fetch_anime_list(list_type="movies")
        self.assertTrue(res["success"])
        item = res["results"][0]
        self.assertEqual(item["format"], "MOVIE")
        self.assertEqual(item["media_type"], "movie")

        _, kwargs = mock_gql.call_args
        variables = kwargs.get("variables", {})
        self.assertEqual(variables["format"], "MOVIE")


if __name__ == '__main__':
    unittest.main()
