# -*- coding: utf-8 -*-
"""
UIndex.org scraper for Orac Server.
Search URL: https://uindex.org/search.php?search=<query>&c=<cat>
  c=1 Movies, c=2 TV
Detail page: https://uindex.org/details.php?id=<id>
  Contains a magnet link with the info-hash.
"""

import re
import threading
from time import time
from urllib.parse import quote_plus

from resources.scrapers.modules import client
from resources.scrapers import source_utils
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG


class source(ConcurrentScraperBase):
    priority = 5
    pack_capable = True
    hasMovies = True
    hasEpisodes = True

    def __init__(self):
        super().__init__('uindex')
        self._results_lock = threading.Lock()
        self.language = ['en']
        self.base_link = 'https://uindex.org'
        self.search_path = '/search.php'
        self.min_seeders = 0
        self.reset_results()

    def reset_results(self):
        with self._results_lock:
            self.source_results = []
            self.item_totals = {'4K': 0, '1080p': 0, '720p': 0, 'SD': 0, 'CAM': 0}

    # ------------------------------------------------------------------
    # Episode / Movie sources
    # ------------------------------------------------------------------

    def sources(self, data, hostDict):
        self.reset_results()
        if not data:
            return self.source_results

        start_time = time()

        try:
            self.aliases = data.get('aliases', [])
            self.year = data.get('year', '')
            self.undesirables = source_utils.get_undesirables()
            self.check_foreign_audio = source_utils.check_foreign_audio()

            if 'tvshowtitle' in data:
                # Episode
                self.title = data['tvshowtitle'].replace('&', 'and').replace('/', ' ').replace('$', 's')
                self.episode_title = data.get('title')
                self.hdlr = 'S%02dE%02d' % (int(data['season']), int(data['episode']))
                self.years = None
                cat = 2  # TV
            else:
                # Movie
                self.title = data['title'].replace('&', 'and').replace('/', ' ').replace('$', 's')
                self.episode_title = None
                self.hdlr = self.year
                self.years = [str(int(self.year) - 1), str(self.year), str(int(self.year) + 1)]
                cat = 1  # Movies

            query = '%s %s' % (re.sub(r'[^A-Za-z0-9\s\.-]+', '', self.title), self.hdlr)
            urls = [
                '%s%s?search=%s&c=%d' % (self.base_link, self.search_path, quote_plus(query), cat),
                '%s%s?search=%s&c=%d&p=2' % (self.base_link, self.search_path, quote_plus(query), cat),
            ]

            self.thread_manager.scrape_urls_optimized(
                urls=urls,
                scraping_function=self.get_sources_worker,
                timeout=20
            )

            self.log_results_thread_safe(start_time, suffix='')
            return self.source_results

        except Exception as e:
            source_utils.scraper_error('UINDEX')
            return self.source_results

    def get_sources_worker(self, link):
        """Fetch search page, parse detail links, resolve magnets."""
        try:
            html = client.request(link, timeout=10)
            if not html:
                return
        except Exception:
            source_utils.scraper_error('UINDEX')
            return

        # Extract detail links from search results.
        # The anchor has class="sr-torrent-link" and a title attribute with the clean name.
        # Inner text contains <mark> tags so we grab the title attribute instead.
        # Pattern: <a href="/details.php?id=12345" ... title="Torrent Name">
        entries = re.findall(
            r'<a\s[^>]*href=["\'](/details\.php\?id=(\d+))["\'][^>]*title=["\'](.*?)["\']',
            html, re.IGNORECASE
        )
        # Fallback: also try title attr before href
        if not entries:
            entries = re.findall(
                r'<a\s[^>]*title=["\'](.*?)["\'][^>]*href=["\'](/details\.php\?id=(\d+))["\']',
                html, re.IGNORECASE
            )
            # Reorder to (rel_path, id, name) tuple for uniform handling below
            entries = [(p, i, n) for n, p, i in entries]

        local_sources = []
        local_totals = {}

        for rel_path, item_id, raw_name in entries:
            try:
                name = source_utils.clean_name(raw_name.strip())
                if not name:
                    continue

                if not source_utils.check_title(self.title, self.aliases, name, self.hdlr, self.year):
                    continue

                name_info = source_utils.info_from_name(name, self.title, self.year, self.hdlr, self.episode_title)
                if source_utils.remove_lang(name_info, self.check_foreign_audio):
                    continue
                if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables):
                    continue

                # Filter episode strings out of movie results
                if self.years:
                    ep_strings = [r'[.-]s\d{2}e\d{2}([.-]?)', r'[.-]s\d{2}([.-]?)', r'[.-]season[.-]?\d{1,2}[.-]?']
                    name_lower = name.lower()
                    if any(re.search(pat, name_lower) for pat in ep_strings):
                        continue

                # Fetch detail page to get magnet link
                detail_url = self.base_link + rel_path
                magnet, seeders, size_str = self._get_detail(detail_url)
                if not magnet:
                    continue

                hash_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet, re.I)
                if not hash_match:
                    continue
                info_hash = hash_match.group(1).upper()

                url = 'magnet:?xt=urn:btih:%s&dn=%s' % (info_hash, quote_plus(name))
                quality, info = source_utils.get_release_quality(name_info, url)

                if size_str:
                    try:
                        dsize, isize = source_utils._size(size_str)
                        info.insert(0, isize)
                    except Exception:
                        dsize = 0
                else:
                    dsize = 0

                info = ' | '.join(info)

                source_item = {
                    'provider': 'uindex',
                    'source': 'torrent',
                    'seeders': seeders,
                    'hash': info_hash,
                    'name': name,
                    'name_info': name_info,
                    'quality': quality,
                    'language': 'en',
                    'url': url,
                    'info': info,
                    'direct': False,
                    'debridonly': True,
                    'size': dsize,
                }

                local_sources.append(source_item)
                local_totals[quality] = local_totals.get(quality, 0) + 1

            except Exception:
                source_utils.scraper_error('UINDEX')

        self.add_sources_thread_safe(local_sources, local_totals)

    def _get_detail(self, url):
        """Fetch a detail page and return (magnet_url, seeders, size_string)."""
        try:
            html = client.request(url, timeout=10)
            if not html:
                return None, 0, None

            # Magnet link is directly in the page
            magnet_match = re.search(r'href=["\'](magnet:\?xt=urn:btih:[^"\']+)["\']', html, re.I)
            magnet = magnet_match.group(1) if magnet_match else None

            # Seeders — look for a numeric value near "Seeders" text in the page
            seeders = 0
            seed_match = re.search(r'(?i)seeder[s]?\D{0,10}?(\d+)', html)
            if seed_match:
                try:
                    seeders = int(seed_match.group(1))
                except Exception:
                    seeders = 0

            # Size — look for pattern like "7.38 GB" or "1.4 GB"
            size_str = None
            size_match = re.search(r'(\d+(?:\.\d+)?\s*(?:GB|MB|KB))', html, re.I)
            if size_match:
                size_str = size_match.group(1)

            return magnet, seeders, size_str

        except Exception:
            source_utils.scraper_error('UINDEX')
            return None, 0, None

    # ------------------------------------------------------------------
    # Season / series packs
    # ------------------------------------------------------------------

    def sources_packs(self, data, hostDict, search_series=False, total_seasons=None, bypass_filter=False):
        self.reset_results()
        if not data:
            return self.source_results

        start_time = time()

        try:
            self.search_series = search_series
            self.total_seasons = total_seasons
            self.bypass_filter = bypass_filter
            self.title = data['tvshowtitle'].replace('&', 'and').replace('/', ' ').replace('$', 's')
            self.aliases = data.get('aliases', [])
            self.imdb = data.get('imdb', '')
            self.year = data.get('year', '')
            self.season_x = data['season']
            self.season_xx = str(self.season_x).zfill(2)
            self.undesirables = source_utils.get_undesirables()
            self.check_foreign_audio = source_utils.check_foreign_audio()

            clean_title = re.sub(r'[^A-Za-z0-9\s\.-]+', '', self.title)

            if search_series:
                queries = [clean_title + ' Season', clean_title + ' Complete']
            else:
                queries = [
                    '%s S%s' % (clean_title, self.season_xx),
                    '%s Season %s' % (clean_title, self.season_x),
                ]

            urls = [
                '%s%s?search=%s&c=2' % (self.base_link, self.search_path, quote_plus(q))
                for q in queries
            ]

            self.thread_manager.scrape_urls_optimized(
                urls=urls,
                scraping_function=self.get_sources_packs_worker,
                timeout=20
            )

            self.log_results_thread_safe(start_time, suffix='(pack)')
            return self.source_results

        except Exception:
            source_utils.scraper_error('UINDEX')
            return self.source_results

    def get_sources_packs_worker(self, link):
        try:
            html = client.request(link, timeout=10)
            if not html:
                return
        except Exception:
            source_utils.scraper_error('UINDEX')
            return

        entries = re.findall(
            r'<a\s[^>]*href=["\'](/details\.php\?id=(\d+))["\'][^>]*title=["\'](.*?)["\']',
            html, re.IGNORECASE
        )
        if not entries:
            entries = re.findall(
                r'<a\s[^>]*title=["\'](.*?)["\'][^>]*href=["\'](/details\.php\?id=(\d+))["\']',
                html, re.IGNORECASE
            )
            entries = [(p, i, n) for n, p, i in entries]

        local_sources = []
        local_totals = {}

        for rel_path, item_id, raw_name in entries:
            try:
                name = source_utils.clean_name(raw_name.strip())
                if not name:
                    continue

                episode_start, episode_end = 0, 0
                package = None

                if not self.search_series:
                    if not self.bypass_filter:
                        valid, episode_start, episode_end = source_utils.filter_season_pack(
                            self.title, self.aliases, self.year, self.season_x, name
                        )
                        if not valid:
                            continue
                    package = 'season'
                else:
                    if not self.bypass_filter:
                        valid, last_season = source_utils.filter_show_pack(
                            self.title, self.aliases, self.imdb, self.year, self.season_x, name, self.total_seasons
                        )
                        if not valid:
                            continue
                    else:
                        last_season = self.total_seasons
                    package = 'show'

                name_info = source_utils.info_from_name(name, self.title, self.year, season=self.season_x, pack=package)
                if source_utils.remove_lang(name_info, self.check_foreign_audio):
                    continue
                if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables):
                    continue

                detail_url = self.base_link + rel_path
                magnet, seeders, size_str = self._get_detail(detail_url)
                if not magnet:
                    continue

                hash_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet, re.I)
                if not hash_match:
                    continue
                info_hash = hash_match.group(1).upper()

                url = 'magnet:?xt=urn:btih:%s&dn=%s' % (info_hash, quote_plus(name))
                quality, info = source_utils.get_release_quality(name_info, url)

                if size_str:
                    try:
                        dsize, isize = source_utils._size(size_str)
                        info.insert(0, isize)
                    except Exception:
                        dsize = 0
                else:
                    dsize = 0

                info = ' | '.join(info)

                item = {
                    'provider': 'uindex',
                    'source': 'torrent',
                    'seeders': seeders,
                    'hash': info_hash,
                    'name': name,
                    'name_info': name_info,
                    'quality': quality,
                    'language': 'en',
                    'url': url,
                    'info': info,
                    'direct': False,
                    'debridonly': True,
                    'size': dsize,
                    'package': package,
                }

                if self.search_series:
                    item['last_season'] = last_season
                elif episode_start:
                    item.update({'episode_start': episode_start, 'episode_end': episode_end})

                local_sources.append(item)
                local_totals[quality] = local_totals.get(quality, 0) + 1

            except Exception:
                source_utils.scraper_error('UINDEX')

        self.add_sources_thread_safe(local_sources, local_totals)


class UIndexService(ConcurrentScraperBase):
    """Wrapper to make UIndex compatible with Orac's ScraperManager."""

    def __init__(self):
        super().__init__('uindex')
        self.scraper = source()

    def scrape_sources(self, data):
        return self.scraper.sources(data, hostDict={})

    def scrape_packs(self, data, search_series=False, total_seasons=None, bypass_filter=False):
        return self.scraper.sources_packs(
            data,
            hostDict={},
            search_series=search_series,
            total_seasons=total_seasons,
            bypass_filter=bypass_filter,
        )

    async def scrape_sources_async(self, data):
        return self.scrape_sources(data)

    async def scrape_packs_async(self, data, search_series=False, total_seasons=None, bypass_filter=False):
        return self.scrape_packs(
            data,
            search_series=search_series,
            total_seasons=total_seasons,
            bypass_filter=bypass_filter,
        )
