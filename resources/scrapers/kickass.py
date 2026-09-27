# -*- coding: utf-8 -*-
import re
import threading
from urllib.parse import quote_plus, unquote_plus
from time import time

from resources.scrapers.modules import client
from resources.scrapers.modules import source_utils
from resources.scrapers.modules import cache
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

class source(ConcurrentScraperBase):
    priority = 4
    pack_capable = True
    hasMovies = True
    hasEpisodes = True

    def __init__(self):
        super().__init__('kickass')
        self._results_lock = threading.Lock()
        self.language = ['en']
        self.domains = ['kick4ss.com', 'thekat.info', 'kickass.cm', 'kickass.ws', 'kickasst.net',
                        'kickasshydra.dev', 'kickasshydra.net', 'kathydra.com', 'kickass.onl',
                        'kickasstorrents.id', 'thekat.cc', 'kkat.net', 'kickasstorrents.bz']
        self._base_link = None
        self.moviesearch = '/usearch/{0}%20category:movies/?field=size&sorder=desc'
        self.tvsearch = '/usearch/{0}%20category:tv/?field=size&sorder=desc'
        self.min_seeders = 0
        self.reset_results()

    def reset_results(self):
        with self._results_lock:
            self.source_results = []
            self.item_totals = {'4K': 0, '1080p': 0, '720p': 0, 'SD': 0, 'CAM': 0}

    @property
    def base_link(self):
        if not self._base_link:
            self._base_link = cache.get(self.__get_base_url, 120, 'https://' + self.domains[0])
        return self._base_link

    def __get_base_url(self, fallback):
        for domain in self.domains:
            try:
                url = 'https://%s' % domain
                result = client.request(url, limit=1, timeout=5)
                try: result = re.search(r'<title>(.+?)</title>', result, re.I).group(1)
                except: result = None
                if result and 'Kickass' in result: return url
            except:
                pass
        return fallback

    def sources(self, data, hostDict):
        self.reset_results()
        if not data: return self.source_results
        
        try:
            start_time = time()
            self.aliases = data['aliases']
            self.year = data['year']
            
            if 'tvshowtitle' in data:
                self.title = data['tvshowtitle'].replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ').replace('$', 's')
                self.episode_title = data['title']
                self.hdlr = 'S%02dE%02d' % (int(data['season']), int(data['episode']))
                search_link = self.tvsearch
            else:
                self.title = data['title'].replace('&', 'and').replace('/', ' ').replace('$', 's')
                self.episode_title = None
                self.hdlr = self.year
                search_link = self.moviesearch
                
            query = '%s %s' % (re.sub(r'[^A-Za-z0-9\s\.-]+', '', self.title), self.hdlr)
            url = '%s%s' % (self.base_link, search_link.format(quote_plus(query)))
            urls = [url]
            if url.endswith('field=size&sorder=desc'): urls.append(url.rsplit("/", 1)[0] + '/2/')
            else: urls.append(url + '/2/')
            
            self.undesirables = source_utils.get_undesirables()
            self.check_foreign_audio = source_utils.check_foreign_audio()
            
            self.thread_manager.scrape_urls_optimized(urls, self.get_sources_worker, timeout=20)
            
            self.log_results_thread_safe(start_time)
            return self.source_results
        except Exception as e:
            log(f"KICKASS error in setup: {e}", LOGERROR)
            return self.source_results

    def get_sources_worker(self, url):
        try:
            results = client.request(url, timeout=7)
            if not results: return
            rows = client.parseDOM(results, 'tr', attrs={'id': 'torrent_latest_torrents'})
            if not rows: return
            
            local_sources = []
            local_totals = {}
            
            for row in rows:
                try:
                    columns = re.findall(r'<td.*?>(.+?)</td>', row, re.DOTALL)
                    if not columns: continue

                    magnet_raw = unquote_plus(columns[0]).replace('&amp;', '&')
                    magnet_match = re.search(r'(magnet:.+?)&tr=', magnet_raw, re.I)
                    if not magnet_match: continue
                    magnet = magnet_match.group(1).replace(' ', '.')
                    
                    hash = re.search(r'btih:(.*?)&', magnet, re.I).group(1)
                    name = source_utils.clean_name(unquote_plus(magnet.split('&dn=')[1]))

                    if not source_utils.check_title(self.title, self.aliases, name, self.hdlr, self.year): continue
                    name_info = source_utils.info_from_name(name, self.title, self.year, self.hdlr, self.episode_title)
                    if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
                    if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

                    if not self.episode_title:
                        ep_strings = [r'[.-]s\d{2}e\d{2}([.-]?)', r'[.-]s\d{2}([.-]?)', r'[.-]season[.-]?\d{1,2}[.-]?']
                        if any(re.search(item, name.lower()) for item in ep_strings): continue

                    try:
                        seeders = int(columns[3].replace(',', ''))
                        if self.min_seeders > seeders: continue
                    except: seeders = 0

                    quality, info = source_utils.get_release_quality(name_info, magnet)
                    try:
                        size_str = columns[1].split('<')[0]
                        dsize, isize = source_utils._size(size_str)
                        info.insert(0, isize)
                    except: dsize = 0
                    
                    item = {'provider': 'kickass', 'source': 'torrent', 'seeders': seeders, 'hash': hash, 'name': name, 'name_info': name_info,
                           'quality': quality, 'language': 'en', 'url': magnet, 'info': ' | '.join(info), 'direct': False, 'debridonly': True, 'size': dsize}
                    
                    local_sources.append(item)
                    local_totals[quality] = local_totals.get(quality, 0) + 1
                except: continue
                
            self.add_sources_thread_safe(local_sources, local_totals)
        except: pass

    def sources_packs(self, data, hostDict, search_series=False, total_seasons=None, bypass_filter=False):
        self.reset_results()
        if not data: return self.source_results
        
        try:
            start_time = time()
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
                queries = [query + ' Season', query + ' Complete']
            else:
                queries = [query + ' S%s' % self.season_xx, query + ' Season %s' % self.season_x]
            
            urls = ['%s%s' % (self.base_link, self.tvsearch.format(quote_plus(q))) for q in queries]
            
            self.thread_manager.scrape_urls_optimized(urls, self.get_sources_packs_worker, timeout=20)
            
            self.log_results_thread_safe(start_time, suffix='(pack)')
            return self.source_results
        except Exception as e:
            log(f"KICKASS pack error: {e}", LOGERROR)
            return self.source_results

    def get_sources_packs_worker(self, link):
        try:
            results = client.request(link, timeout=7)
            if not results: return
            rows = client.parseDOM(results, 'tr', attrs={'id': 'torrent_latest_torrents'})
            if not rows: return
            
            local_sources = []
            local_totals = {}
            
            for row in rows:
                try:
                    columns = re.findall(r'<td.*?>(.+?)</td>', row, re.DOTALL)
                    if not columns: continue

                    magnet_raw = unquote_plus(columns[0]).replace('&amp;', '&')
                    magnet_match = re.search(r'(magnet:.+?)&tr=', magnet_raw, re.I)
                    if not magnet_match: continue
                    magnet = magnet_match.group(1).replace(' ', '.')
                    
                    hash = re.search(r'btih:(.*?)&', magnet, re.I).group(1)
                    name = source_utils.clean_name(unquote_plus(magnet.split('&dn=')[1]))

                    episode_start, episode_end = 0, 0
                    if not self.search_series:
                        if not self.bypass_filter:
                            valid, episode_start, episode_end = source_utils.filter_season_pack(self.title, self.aliases, self.year, self.season_x, name)
                            if not valid: continue
                        package = 'season'
                    else:
                        if not self.bypass_filter:
                            valid, last_season = source_utils.filter_show_pack(self.title, self.aliases, self.imdb, self.year, self.season_x, name, self.total_seasons)
                            if not valid: continue
                        else: last_season = self.total_seasons
                        package = 'show'

                    name_info = source_utils.info_from_name(name, self.title, self.year, season=self.season_x, pack=package)
                    if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
                    if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

                    try:
                        seeders = int(columns[3].replace(',', ''))
                        if self.min_seeders > seeders: continue
                    except: seeders = 0

                    quality, info = source_utils.get_release_quality(name_info, magnet)
                    try:
                        size_str = columns[1].split('<')[0]
                        dsize, isize = source_utils._size(size_str)
                        info.insert(0, isize)
                    except: dsize = 0
                    
                    info_str = ' | '.join(info)
                    item = {'provider': 'kickass', 'source': 'torrent', 'seeders': seeders, 'hash': hash, 'name': name, 'name_info': name_info, 'quality': quality,
                           'language': 'en', 'url': magnet, 'info': info_str, 'direct': False, 'debridonly': True, 'size': dsize, 'package': package}
                    
                    if self.search_series: item.update({'last_season': last_season})
                    elif episode_start: item.update({'episode_start': episode_start, 'episode_end': episode_end})
                    
                    local_sources.append(item)
                    local_totals[quality] = local_totals.get(quality, 0) + 1
                except: continue
                
            self.add_sources_thread_safe(local_sources, local_totals)
        except: pass

class KickassService(ConcurrentScraperBase):
    def __init__(self):
        super().__init__('kickass')
        self.scraper = source()

    def scrape_sources(self, data):
        return self.scraper.sources(data, hostDict={})

    def scrape_packs(self, data, search_series=False, total_seasons=None, bypass_filter=False):
        return self.scraper.sources_packs(data, hostDict={}, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)

    async def scrape_sources_async(self, data):
        return self.scrape_sources(data)

    async def scrape_packs_async(self, data, search_series=False, total_seasons=None, bypass_filter=False):
        return self.scrape_packs(data, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)
