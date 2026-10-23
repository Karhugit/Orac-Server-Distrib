import unittest
import os
import sys

# Ensure root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from resources.scrapers.source_utils import _match_episode_hdlr, check_title
from resources.scrapers.modules.source_utils import _match_episode_hdlr as mod_match_episode_hdlr, check_title as mod_check_title
from resources.lib.debrid_resolver import seas_ep_filter


class TestDateBasedScraping(unittest.TestCase):

    def test_match_episode_hdlr_date_dot(self):
        hdlr = 'S43E27'
        rel = 'Jeopardy.2026.10.20.1080p.WEB.h264-MuTT.mkv'
        match = _match_episode_hdlr(rel, hdlr, air_date='2026-10-20')
        self.assertTrue(bool(match))

    def test_match_episode_hdlr_date_dash(self):
        hdlr = 'S43E27'
        rel = 'Jeopardy.2026-10-20.720p.HDTV.mkv'
        match = _match_episode_hdlr(rel, hdlr, air_date='2026-10-20')
        self.assertTrue(bool(match))

    def test_match_episode_hdlr_date_mismatch(self):
        hdlr = 'S43E27'
        rel = 'Jeopardy.2026.10.19.1080p.WEB.h264-MuTT.mkv'
        match = _match_episode_hdlr(rel, hdlr, air_date='2026-10-20')
        self.assertFalse(bool(match))

    def test_match_episode_hdlr_show_number(self):
        hdlr = 'S43E27'
        rel = 'Jeopardy.Show.9642.720p.HDTV.mkv'
        match = _match_episode_hdlr(rel, hdlr, air_date='2026-10-20', show_number='9642')
        self.assertTrue(bool(match))

    def test_check_title_with_air_date(self):
        title = "Jeopardy!"
        aliases = [{"title": "Jeopardy"}]
        hdlr = 'S43E27'
        air_date = '2026-10-20'

        rel1 = 'Jeopardy.2026.10.20.1080p.PCOK.WEB-DL.AAC2.0.H.264-MuTT.mkv'
        self.assertTrue(check_title(title, aliases, rel1, hdlr, '1984', air_date=air_date))

        rel2 = 'Jeopardy.2026.10.19.1080p.PCOK.WEB-DL.AAC2.0.H.264-MuTT.mkv'
        self.assertFalse(check_title(title, aliases, rel2, hdlr, '1984', air_date=air_date))

        # Standard SxxExx should still work
        rel3 = 'Jeopardy.S43E27.1080p.WEB.H264.mkv'
        self.assertTrue(check_title(title, aliases, rel3, hdlr, '1984', air_date=air_date))

        # Check modules/source_utils implementation behaves identically
        self.assertTrue(mod_check_title(title, aliases, rel1, hdlr, '1984', air_date=air_date))
        self.assertFalse(mod_check_title(title, aliases, rel2, hdlr, '1984', air_date=air_date))
        self.assertTrue(mod_check_title(title, aliases, rel3, hdlr, '1984', air_date=air_date))
        self.assertTrue(bool(mod_match_episode_hdlr(rel1, hdlr, air_date=air_date)))

    def test_seas_ep_filter_date(self):
        season = '43'
        episode = '27'
        air_date = '2026-10-20'

        file1 = 'Jeopardy.2026.10.20.1080p.PCOK.WEB-DL.AAC2.0.H.264-MuTT.mkv'
        file2 = 'Jeopardy.2026.10.19.1080p.PCOK.WEB-DL.AAC2.0.H.264-MuTT.mkv'
        file3 = 'Jeopardy.S43E27.1080p.WEB-DL.mkv'

        self.assertTrue(seas_ep_filter(season, episode, file1, air_date=air_date))
        self.assertFalse(seas_ep_filter(season, episode, file2, air_date=air_date))
        self.assertTrue(seas_ep_filter(season, episode, file3, air_date=air_date))

    def test_standard_episodic_show_regression(self):
        title = "Breaking Bad"
        aliases = []
        hdlr = 'S05E14'
        rel = 'Breaking.Bad.S05E14.Ozymandias.1080p.BluRay.x264.mkv'
        self.assertTrue(check_title(title, aliases, rel, hdlr, '2008'))

        rel_wrong_ep = 'Breaking.Bad.S05E15.Granite.State.1080p.BluRay.x264.mkv'
        self.assertFalse(check_title(title, aliases, rel_wrong_ep, hdlr, '2008'))


if __name__ == '__main__':
    unittest.main()
