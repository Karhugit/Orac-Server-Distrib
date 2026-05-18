# -*- coding: utf-8 -*-
import threading
import re
from json import loads as jsloads
from time import time
from urllib.parse import quote

from resources.scrapers.modules import client
from resources.scrapers import source_utils
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

class source(ConcurrentScraperBase):
    priority = 2
    pack_capable = True
    hasMovies = True
    hasEpisodes = True
    
    def __init__(self):
        super().__init__('piratebay')
        self._results_lock = threading.Lock()
        self.language = ['en']
        self.base_link = "https://apibay.org"
        self.search_link = '/q.php?q=%s&cat=0'
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
                self.title = data['tvshowtitle'].replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ').replace('$', 's').replace('·', '-')
                self.episode_title = data['title']
                self.hdlr = 'S%02dE%02d' % (int(data['season']), int(data['episode']))
                self.years = None
            else:
                self.title = data['title'].replace('&', 'and').replace('/', ' ').replace('$', 's').replace('·', '-')
                self.episode_title = None
                self.hdlr = self.year
                self.years = [str(int(self.year)-1), str(self.year), str(int(self.year)+1)]
            
            query = '%s %s' % (re.sub(r'[^A-Za-z0-9\s\.-]+', '', self.title), self.hdlr)
            url = '%s%s' % (self.base_link, self.search_link % quote(query))
            
            self.undesirables = source_utils.get_undesirables()
            self.check_foreign_audio = source_utils.check_foreign_audio()

            # Use optimized threading
            self.thread_manager.scrape_urls_optimized(
                urls=[url], 
                scraping_function=self.get_sources_worker,
                timeout=15
            )
            
            # Thread-safe result logging
            self.log_results_thread_safe(start_time, suffix='')
            
            return self.source_results
            
        except:
            source_utils.scraper_error('PIRATEBAY')
            return self.source_results

    def get_sources_worker(self, url):
        """Worker method that does the actual scraping for sources"""
        try:
            # apibay results are usually JSON
            results = client.request(url, output='extended', timeout=7)
            if not results:
                return
            
            if results[1] in ('200', '201'):
                files = jsloads(results[0])
            else:
                log(f'PIRATEBAY: Failed query for ({url}) : {results}', LOGWARNING)
                return

            if not files or not isinstance(files, list) or files[0].get('name') == 'No results found':
                log(f'PIRATEBAY: No results found for {url}', LOGDEBUG)
                return

            log(f'PIRATEBAY: Found {len(files)} potential results from API for {url}', LOGDEBUG)

            # Collect results locally first
            local_sources = []
            local_totals = {}

            for file in files:
                try:
                    info_hash = file['info_hash']
                    name = source_utils.clean_name(file['name'])

                    check = source_utils.check_title(self.title, self.aliases, name, self.hdlr, self.year, self.years)
                    if not check:
                        log(f'PIRATEBAY: Failed check_title for {name} (Title: {self.title}, Hdlr: {self.hdlr}, Year: {self.year})', LOGDEBUG)
                        continue
                    
                    name_info = source_utils.info_from_name(name, self.title, self.year, self.hdlr, self.episode_title)
                    if self.years: # filter for eps returned in movie query
                        ep_strings = [r'[.-]s\d{2}e\d{2}([.-]?)', r'[.-]s\d{2}([.-]?)', r'[.-]season[.-]?\d{1,2}[.-]?']
                        name_lower = name.lower()
                        if any(re.search(item, name_lower) for item in ep_strings): 
                            continue

                    magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (info_hash, name) 
                    try:
                        seeders = int(file['seeders'])
                        if self.min_seeders > seeders: 
                            continue
                    except: 
                        seeders = 0

                    quality, info = source_utils.get_release_quality(name_info, magnet_url)
                    
                    try:
                        # APiBay size is in bytes normally
                        file_size_bytes = float(file["size"])
                        dsize, isize = source_utils.convert_size(file_size_bytes, to='GB')
                        info.insert(0, isize)
                    except: 
                        dsize = 0
                    
                    info = ' | '.join(info)
                    source_item = {
                        'provider': 'piratebay', 'source': 'torrent', 'seeders': seeders, 'hash': info_hash, 'name': name, 'name_info': name_info,
                        'quality': quality, 'language': 'en', 'url': magnet_url, 'info': info, 'direct': False, 'debridonly': True, 'size': dsize
                    }
                    
                    local_sources.append(source_item)
                    local_totals[quality] = local_totals.get(quality, 0) + 1
                    
                except Exception as e:
                    source_utils.scraper_error('PIRATEBAY')

            # Thread-safe addition of all results
            self.add_sources_thread_safe(local_sources, local_totals)
            log(f'PIRATEBAY: Added {len(local_sources)} sources from {url}', LOGDEBUG)

        except:
            source_utils.scraper_error('PIRATEBAY')

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
            self.season_x = data['season']
            self.season_xx = self.season_x.zfill(2)
            self.undesirables = source_utils.get_undesirables()
            self.check_foreign_audio = source_utils.check_foreign_audio()

            query = re.sub(r'[^A-Za-z0-9\s\.-]+', '', self.title)
            if search_series:
                queries = [
                    self.search_link % quote(query + ' Season'),
                    self.search_link % quote(query + ' Complete')
                ]
            else:
                queries = [
                    self.search_link % quote(query + ' S%s' % self.season_xx),
                    self.search_link % quote(query + ' Season %s' % self.season_x)
                ]
                
            links = ['%s%s' % (self.base_link, url) for url in queries]

            # Use optimized threading
            self.thread_manager.scrape_urls_optimized(
                urls=links, 
                scraping_function=self.get_sources_packs_worker,
                timeout=20
            )
            
            # Thread-safe result logging
            self.log_results_thread_safe(start_time, suffix='(pack)')
            
            return self.source_results
            
        except:
            source_utils.scraper_error('PIRATEBAY')
            return self.source_results

    def get_sources_packs_worker(self, link):
        """Worker method that does the actual scraping for packs"""
        try:
            results = client.request(link, output='extended', timeout=7)
            if not results:
                return
            
            if results[1] in ('200', '201'):
                files = jsloads(results[0])
            else:
                return

            if not files or not isinstance(files, list) or files[0].get('name') == 'No results found':
                log(f'PIRATEBAY: No results found for {link}', LOGDEBUG)
                return

            log(f'PIRATEBAY: Found {len(files)} potential results from API for {link}', LOGDEBUG)

            # Process rows and collect results locally first
            local_sources = []
            local_totals = {}

            for file in files:
                try:
                    info_hash = file['info_hash']
                    name = source_utils.clean_name(file['name'])

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

                    magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (info_hash, name)
                    try:
                        seeders = int(file['seeders'])
                        if self.min_seeders > seeders: 
                            continue
                    except: 
                        seeders = 0

                    quality, info = source_utils.get_release_quality(name_info, magnet_url)
                    try:
                        file_size_bytes = float(file["size"])
                        dsize, isize = source_utils.convert_size(file_size_bytes, to='GB')
                        info.insert(0, isize)
                    except: 
                        dsize = 0
                    info = ' | '.join(info)

                    item = {
                        'provider': 'piratebay', 'source': 'torrent', 'seeders': seeders, 'hash': info_hash, 'name': name, 'name_info': name_info, 'quality': quality,
                        'language': 'en', 'url': magnet_url, 'info': info, 'direct': False, 'debridonly': True, 'size': dsize, 'package': package
                    }
                    if self.search_series: 
                        item.update({'last_season': last_season})
                    elif episode_start: 
                        item.update({'episode_start': episode_start, 'episode_end': episode_end})
                    
                    local_sources.append(item)
                    local_totals[quality] = local_totals.get(quality, 0) + 1
                    
                except:
                    source_utils.scraper_error('PIRATEBAY')

            # Thread-safe addition of all results
            self.add_sources_thread_safe(local_sources, local_totals)
            log(f'PIRATEBAY: Added {len(local_sources)} sources from {link}', LOGDEBUG)

        except:
            source_utils.scraper_error('PIRATEBAY')

class PirateBayService(ConcurrentScraperBase):
    """
    Wrapper class for PirateBay compatible with ScraperManager.
    """
    def __init__(self):
        super().__init__('piratebay')
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