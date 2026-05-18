# -*- coding: utf-8 -*-
# modified by Venom for Fenomscrapers (updated 7-19-2022)
import threading
"""
    Fenomscrapers Project
"""

import re
from urllib.parse import quote_plus

from resources.scrapers.modules import client
from resources.scrapers import source_utils
from time import time
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

class source(ConcurrentScraperBase):
    priority = 5
    pack_capable = True
    hasMovies = True
    hasEpisodes = True
    
    def __init__(self):
        super().__init__('limetorrents')
        self._results_lock = threading.Lock()
        self.language = ['en']
        self.base_link = "https://www.limetorrents.lol"
        # self.base_link = "https://limetorrents.proxyninja.org" # if ever needed
        self.tvsearch = '/search/tv/{0}/1/'
        self.moviesearch = '/search/movies/{0}/1/'
        self.min_seeders = 0
        self.reset_results()

    def reset_results(self):
        """Resets the source results and item totals for a new scrape."""
        with self._results_lock:
            self.source_results = []
            self.item_totals = {'4K': 0, '1080p': 0, '720p': 0, 'SD': 0, 'CAM': 0}

    def sources(self, data, hostDict):
        self.reset_results()

        if not data: 
            return self.source_results
        
        start_time = time()
        
        try:
            self.aliases = data['aliases']
            self.year = data['year']
            
            if 'tvshowtitle' in data:
                self.title = data['tvshowtitle'].replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ').replace('$', 's')
                self.episode_title = data['title']
                self.hdlr = 'S%02dE%02d' % (int(data['season']), int(data['episode']))
                self.years = None
                url = (self.base_link + self.tvsearch)
            else:
                self.title = data['title'].replace('&', 'and').replace('/', ' ').replace('$', 's')
                self.episode_title = None
                self.hdlr = self.year
                self.years = [str(int(self.year)-1), str(self.year), str(int(self.year)+1)]
                url = (self.base_link + self.moviesearch)
            
            query = '%s %s' % (re.sub(r'[^A-Za-z0-9\s\.-]+', '', self.title), self.hdlr)
            url = url.format(quote_plus(query))
            urls = [url, url.replace('/1/', '/2/')]
            
            self.undesirables = source_utils.get_undesirables()
            self.check_foreign_audio = source_utils.check_foreign_audio()
            
            # Use optimized threading
            self.thread_manager.scrape_urls_optimized(
                urls=urls, 
                scraping_function=self.get_sources_worker,
                timeout=20
            )
            
            # Thread-safe result logging
            self.log_results_thread_safe(start_time, suffix='')
            
            return self.source_results
            
        except:
            source_utils.scraper_error('LIMETORRENTS')
            return self.source_results

    def get_sources_worker(self, link):
        """Worker method that does the actual scraping for sources"""
        try:
            results = client.request(link, timeout=7)
            if results and '503 Service Temporarily Unavailable' in results:
                log('LIMETORRENTS (Single request failure): 503 Service Temporarily Unavailable', LOGWARNING)
                return
            if not results or '<table' not in results: 
                return
                
            table = client.parseDOM(results, 'table', attrs={'class': 'table2'})
            if not table:
                return
            rows = client.parseDOM(table[0], 'tr')
            if not rows: 
                return
                
        except:
            source_utils.scraper_error('LIMETORRENTS')
            return

        # Process rows and collect results locally first
        local_sources = []
        local_totals = {}

        for row in rows:
            try:
                if '<th' in row: 
                    continue
                columns = re.findall(r'<td.*?>(.+?)</td>', row, re.DOTALL)

                hash = re.search(r'/torrent/(.+?).torrent', columns[0], re.I).group(1)
                name = re.search(r'title\s*=\s*(.+?)["\']', columns[0], re.I).group(1)
                name = source_utils.clean_name(name)

                if not source_utils.check_title(self.title, self.aliases, name, self.hdlr, self.year): 
                    continue
                name_info = source_utils.info_from_name(name, self.title, self.year, self.hdlr, self.episode_title)
                if source_utils.remove_lang(name_info, self.check_foreign_audio): 
                    continue
                if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): 
                    continue

                if self.years: # filter for eps returned in movie query (rare but movie and show exists for Run in 2020)
                    ep_strings = [r'[.-]s\d{2}e\d{2}([.-]?)', r'[.-]s\d{2}([.-]?)', r'[.-]season[.-]?\d{1,2}[.-]?']
                    name_lower = name.lower()
                    if any(re.search(item, name_lower) for item in ep_strings): 
                        continue

                url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
                try:
                    seeders = int(columns[3].replace(',', ''))
                    if self.min_seeders > seeders: 
                        continue
                except: 
                    seeders = 0

                quality, info = source_utils.get_release_quality(name_info, url)
                try:
                    dsize, isize = source_utils._size(columns[2])
                    info.insert(0, isize)
                except: 
                    dsize = 0
                info = ' | '.join(info)

                source_item = {'provider': 'limetorrents', 'source': 'torrent', 'seeders': seeders, 'hash': hash, 'name': name, 'name_info': name_info,
                            'quality': quality, 'language': 'en', 'url': url, 'info': info, 'direct': False, 'debridonly': True, 'size': dsize}
                
                local_sources.append(source_item)
                local_totals[quality] = local_totals.get(quality, 0) + 1
                
            except:
                source_utils.scraper_error('LIMETORRENTS')

        # Thread-safe addition of all results
        self.add_sources_thread_safe(local_sources, local_totals)

    def sources_packs(self, data, hostDict, search_series=False, total_seasons=None, bypass_filter=False):
        self.reset_results()

        if not data: 
            return self.source_results
            
        start_time = time()
        
        try:
            self.search_series = search_series
            self.total_seasons = total_seasons
            self.bypass_filter = bypass_filter

            self.title = data['tvshowtitle'].replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ').replace('$', 's')
            self.aliases = data['aliases']
            self.imdb = data['imdb']
            self.year = data['year']
            self.season_x = data['season']
            self.season_xx = self.season_x.zfill(2)
            self.undesirables = source_utils.get_undesirables()
            self.check_foreign_audio = source_utils.check_foreign_audio()

            query = re.sub(r'[^A-Za-z0-9\s\.-]+', '', self.title)
            if search_series:
                queries = [
                    self.tvsearch.format(quote_plus(query + ' Season')),
                    self.tvsearch.format(quote_plus(query + ' Complete'))
                ]
            else:
                queries = [
                    self.tvsearch.format(quote_plus(query + ' S%s' % self.season_xx)),
                    self.tvsearch.format(quote_plus(query + ' Season %s' % self.season_x))
                ]
                
            links = []
            for url in queries:
                link = ('%s%s' % (self.base_link, url)).replace('+', '-')
                links.append(link)

            if not links:
                return self.source_results
        
            self.thread_manager.scrape_urls_optimized(
                urls=links, 
                scraping_function=self.get_sources_packs_worker,
                timeout=20
            )
            
            # Thread-safe result logging
            self.log_results_thread_safe(start_time, suffix='(pack)')
            
            return self.source_results
            
        except:
            source_utils.scraper_error('LIMETORRENTS')
            return self.source_results

    def get_sources_packs_worker(self, link):
        """Worker method that does the actual scraping for packs"""
        try:
            results = client.request(link, timeout=7)
            if results and '503 Service Temporarily Unavailable' in results:
                log('LIMETORRENTS (Single request failure): 503 Service Temporarily Unavailable', LOGWARNING)
                return
            if not results or '<table' not in results: 
                return
                
            table = client.parseDOM(results, 'table', attrs={'class': 'table2'})
            if not table:
                return
            rows = client.parseDOM(table[0], 'tr')
            if not rows: 
                return
                
        except:
            source_utils.scraper_error('LIMETORRENTS')
            return

        # Process rows and collect results locally first
        local_sources = []
        local_totals = {}

        for row in rows:
            try:
                if '<th' in row: 
                    continue
                columns = re.findall(r'<td.*?>(.+?)</td>', row, re.DOTALL)

                hash = re.search(r'/torrent/(.+?).torrent', columns[0], re.I).group(1)
                name = re.search(r'title\s*=\s*(.+?)["\']', columns[0], re.I).group(1)
                name = source_utils.clean_name(name)

                episode_start, episode_end = 0, 0
                if not self.search_series:
                    if not self.bypass_filter:
                        valid, episode_start, episode_end = source_utils.filter_season_pack(self.title, self.aliases, self.year, self.season_x, name)
                        if not valid: 
                            continue
                    package = 'season'

                elif self.search_series:
                    if not self.bypass_filter:
                        valid, last_season = source_utils.filter_show_pack(self.title, self.aliases, self.imdb, self.year, self.season_x, name, self.total_seasons)
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

                url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
                quality, info = source_utils.get_release_quality(name_info, url)
                try:
                    seeders = int(columns[3].replace(',', ''))
                    if self.min_seeders > seeders: 
                        continue
                except: 
                    seeders = 0
                try:
                    dsize, isize = source_utils._size(columns[2])
                    info.insert(0, isize)
                except: 
                    dsize = 0
                info = ' | '.join(info)

                item = {'provider': 'limetorrents', 'source': 'torrent', 'seeders': seeders, 'hash': hash, 'name': name, 'name_info': name_info, 'quality': quality,
                        'language': 'en', 'url': url, 'info': info, 'direct': False, 'debridonly': True, 'size': dsize, 'package': package}
                if self.search_series: 
                    item.update({'last_season': last_season})
                elif episode_start: 
                    item.update({'episode_start': episode_start, 'episode_end': episode_end}) # for partial season packs
                
                # Add to local collections
                local_sources.append(item)
                local_totals[quality] = local_totals.get(quality, 0) + 1
                
            except:
                source_utils.scraper_error('LIMETORRENTS')

        # Thread-safe addition of all results
        self.add_sources_thread_safe(local_sources, local_totals)

class LimeTorrentsService(ConcurrentScraperBase):
    """
    Wrapper class to make the limetorrents scraper compatible with the Orac server's ScraperManager.
    It inherits from ConcurrentScraperBase to reuse the threading and result handling logic.
    """
    def __init__(self):
        # Initialize the base class with the scraper's name
        super().__init__('limetorrents')
        # Create an instance of the original scraper logic
        self.scraper = source()

    def scrape_sources(self, data):
        return self.scraper.sources(data, hostDict={})

    def scrape_packs(self, data, search_series=False, total_seasons=None, bypass_filter=False):
        return self.scraper.sources_packs(
            data,
            hostDict={},
            search_series=search_series,
            total_seasons=total_seasons,
            bypass_filter=bypass_filter
        )

    async def scrape_sources_async(self, data):
        """Async wrapper for the original 'sources' method."""
        return self.scrape_sources(data)

    async def scrape_packs_async(self, data, search_series=False, total_seasons=None, bypass_filter=False):
        """Async wrapper for the original 'sources_packs' method."""
        return self.scrape_packs(
            data,
            search_series=search_series,
            total_seasons=total_seasons,
            bypass_filter=bypass_filter
        )