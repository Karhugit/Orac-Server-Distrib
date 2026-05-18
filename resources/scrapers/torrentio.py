# -*- coding: utf-8 -*-
import threading
import re
from json import loads as jsloads
from time import time

from resources.scrapers.modules import client
from resources.scrapers import source_utils
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

class source(ConcurrentScraperBase):
    priority = 1
    pack_capable = True
    hasMovies = True
    hasEpisodes = True
    
    def __init__(self):
        super().__init__('torrentio')
        self._results_lock = threading.Lock()
        self.language = ['en']
        self.base_link = "https://torrentio.strem.fun"
        self.movieSearch_link = '/stream/movie/%s.json'
        self.tvSearch_link = '/stream/series/%s:%s:%s.json'
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
            self.imdb = data.get('imdb') or data.get('imdb_id')
            
            if 'tvshowtitle' in data:
                self.title = data['tvshowtitle'].replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ').replace('$', 's')
                self.episode_title = data['title']
                self.season = data['season']
                self.episode = data['episode']
                self.hdlr = 'S%02dE%02d' % (int(self.season), int(self.episode))
                self.years = None
                url = '%s%s' % (self.base_link, self.tvSearch_link % (self.imdb, self.season, self.episode))
            else:
                self.title = data['title'].replace('&', 'and').replace('/', ' ').replace('$', 's')
                self.episode_title = None
                self.hdlr = self.year
                self.years = [str(int(self.year)-1), str(self.year), str(int(self.year)+1)]
                url = '%s%s' % (self.base_link, self.movieSearch_link % self.imdb)
            
            self.undesirables = source_utils.get_undesirables()
            self.check_foreign_audio = source_utils.check_foreign_audio()
            # self.bypass_filter is handled in worker if needed, but normally False
            self.bypass_filter = False

            # Use optimized threading
            self.thread_manager.scrape_urls_optimized(
                urls=[url], 
                scraping_function=self.get_sources_worker,
                timeout=20
            )
            
            # Thread-safe result logging
            self.log_results_thread_safe(start_time, suffix='')
            
            return self.source_results
            
        except:
            source_utils.scraper_error('TORRENTIO')
            return self.source_results

    def get_sources_worker(self, url):
        """Worker method that does the actual scraping for sources"""
        try:
            results = client.request(url, timeout=10)
            if not results:
                return
            
            try:
                files = jsloads(results)['streams']
            except:
                files = []
            
            if not files:
                return

            _INFO = re.compile(r'👤.*')
            
            # Process rows and collect results locally first
            local_sources = []
            local_totals = {}

            for file in files:
                try:
                    info_hash = file.get('infoHash')
                    if not info_hash: continue
                    file_title = file.get('title', '').split('\n')
                    
                    try:
                        file_info = [x for x in file_title if _INFO.match(x)][0]
                    except IndexError:
                        file_info = ''
                        
                    name = source_utils.clean_name(file_title[0])
                    
                    # check_title for single episodes/movies
                    if not self.bypass_filter:
                        if not source_utils.check_title(self.title, self.aliases, name.replace('.(Archie.Bunker', ''), self.hdlr, self.year, self.years): 
                            continue
                            
                    name_info = source_utils.info_from_name(name, self.title, self.year, self.hdlr, self.episode_title)
                    if source_utils.remove_lang(name_info, self.check_foreign_audio): 
                        continue
                    if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): 
                        continue

                    magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (info_hash, name) 
                    try:
                        seeders = int(re.search(r'(\d+)', file_info).group(1))
                        if self.min_seeders > seeders: 
                            continue
                    except: 
                        seeders = 0

                    quality, info = source_utils.get_release_quality(name_info, magnet_url)
                    try:
                        size_match = re.search(r'((?:\d+\,\d+\.\d+|\d+\.\d+|\d+\,\d+|\d+)\s*(?:GB|GiB|Gb|MB|MiB|Mb))', file_info)
                        if size_match:
                            size_str = size_match.group(0)
                            dsize, isize = source_utils._size(size_str)
                            info.insert(0, isize)
                        else:
                            dsize = 0
                    except: 
                        dsize = 0
                    info = ' | '.join(info)

                    source_item = {
                        'provider': 'torrentio', 'source': 'torrent', 'seeders': seeders, 'hash': info_hash, 'name': name, 'name_info': name_info,
                        'quality': quality, 'language': 'en', 'url': magnet_url, 'info': info, 'direct': False, 'debridonly': True, 'size': dsize
                    }
                    
                    local_sources.append(source_item)
                    local_totals[quality] = local_totals.get(quality, 0) + 1
                    
                except:
                    source_utils.scraper_error('TORRENTIO')

            # Thread-safe addition of all results
            self.add_sources_thread_safe(local_sources, local_totals)

        except:
            source_utils.scraper_error('TORRENTIO')

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
            self.imdb = data.get('imdb') or data.get('imdb_id')
            self.year = data['year']
            self.season = data['season']
            self.undesirables = source_utils.get_undesirables()
            self.check_foreign_audio = source_utils.check_foreign_audio()

            # Torrentio doesn't have a specific pack endpoint, it returns everything for the episode
            # We use the same URL but filter for packs
            url = '%s%s' % (self.base_link, self.tvSearch_link % (self.imdb, self.season, data['episode']))

            # Use optimized threading
            self.thread_manager.scrape_urls_optimized(
                urls=[url], 
                scraping_function=self.get_sources_packs_worker,
                timeout=20
            )
            
            # Thread-safe result logging
            self.log_results_thread_safe(start_time, suffix='(pack)')
            
            return self.source_results
            
        except:
            source_utils.scraper_error('TORRENTIO')
            return self.source_results

    def get_sources_packs_worker(self, url):
        """Worker method that does the actual scraping for packs"""
        try:
            results = client.request(url, timeout=10)
            if not results:
                return
            
            try:
                files = jsloads(results)['streams']
            except:
                files = []
            
            if not files:
                return

            _INFO = re.compile(r'👤.*')
            
            # Process rows and collect results locally first
            local_sources = []
            local_totals = {}

            for file in files:
                try:
                    info_hash = file['infoHash']
                    file_title = file['title'].split('\n')
                    
                    try:
                        file_info = [x for x in file_title if _INFO.match(x)][0]
                    except IndexError:
                        file_info = ''
                        
                    name = source_utils.clean_name(file_title[0])

                    episode_start, episode_end = 0, 0
                    if not self.search_series:
                        if not self.bypass_filter:
                            valid, episode_start, episode_end = source_utils.filter_season_pack(self.title, self.aliases, self.year, self.season, name.replace('.(Archie.Bunker', ''))
                            if not valid: 
                                continue
                        package = 'season'

                    elif self.search_series:
                        if not self.bypass_filter:
                            valid, last_season = source_utils.filter_show_pack(self.title, self.aliases, self.imdb, self.year, self.season, name.replace('.(Archie.Bunker', ''), self.total_seasons)
                            if not valid: 
                                continue
                        else: 
                            last_season = self.total_seasons
                        package = 'show'

                    name_info = source_utils.info_from_name(name, self.title, self.year, season=self.season, pack=package)
                    if source_utils.remove_lang(name_info, self.check_foreign_audio): 
                        continue
                    if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): 
                        continue

                    magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (info_hash, name)
                    quality, info = source_utils.get_release_quality(name_info, magnet_url)
                    try:
                        seeders = int(re.search(r'(\d+)', file_info).group(1))
                        if self.min_seeders > seeders: 
                            continue
                    except: 
                        seeders = 0
                    try:
                        size_match = re.search(r'((?:\d+\,\d+\.\d+|\d+\.\d+|\d+\,\d+|\d+)\s*(?:GB|GiB|Gb|MB|MiB|Mb))', file_info)
                        if size_match:
                            size_str = size_match.group(0)
                            dsize, isize = source_utils._size(size_str)
                            info.insert(0, isize)
                        else:
                            dsize = 0
                    except: 
                        dsize = 0
                    info = ' | '.join(info)

                    item = {
                        'provider': 'torrentio', 'source': 'torrent', 'seeders': seeders, 'hash': info_hash, 'name': name, 'name_info': name_info, 'quality': quality,
                        'language': 'en', 'url': magnet_url, 'info': info, 'direct': False, 'debridonly': True, 'size': dsize, 'package': package
                    }
                    if self.search_series: 
                        item.update({'last_season': last_season})
                    elif episode_start: 
                        item.update({'episode_start': episode_start, 'episode_end': episode_end}) # for partial season packs
                    
                    local_sources.append(item)
                    local_totals[quality] = local_totals.get(quality, 0) + 1
                    
                except:
                    source_utils.scraper_error('TORRENTIO')

            # Thread-safe addition of all results
            self.add_sources_thread_safe(local_sources, local_totals)

        except:
            source_utils.scraper_error('TORRENTIO')

class TorrentioService(ConcurrentScraperBase):
    """
    Wrapper class to make the torrentio scraper compatible with the Orac server's ScraperManager.
    """
    def __init__(self):
        super().__init__('torrentio')
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