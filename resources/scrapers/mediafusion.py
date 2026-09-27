# -*- coding: utf-8 -*-
import re
import threading
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
        super().__init__('mediafusion')
        self._results_lock = threading.Lock()
        self.language = ['en']
        self.base_link = "https://mediafusion.stremio.ru"
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
        if not data: return self.source_results
        
        start_time = time()
        try:
            self.title = data['tvshowtitle'] if 'tvshowtitle' in data else data['title']
            self.title = self.title.replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ')
            self.aliases = data['aliases']
            self.episode_title = data['title'] if 'tvshowtitle' in data else None
            self.year = data['year']
            self.imdb = data.get('imdb') or data.get('imdb_id')
            
            if 'tvshowtitle' in data:
                self.season = data['season']
                self.episode = data['episode']
                self.hdlr = 'S%02dE%02d' % (int(self.season), int(self.episode))
                url = '%s%s' % (self.base_link, self.tvSearch_link % (self.imdb, self.season, self.episode))
            else:
                self.hdlr = self.year
                url = '%s%s' % (self.base_link, self.movieSearch_link % self.imdb)

            self.undesirables = source_utils.get_undesirables()
            self.check_foreign_audio = source_utils.check_foreign_audio()

            # Perform the request
            self.get_sources_worker(url)
            
            self.log_results_thread_safe(start_time, suffix='')
            return self.source_results
        except:
            source_utils.scraper_error('MEDIAFUSION')
            return self.source_results

    def get_sources_worker(self, url):
        try:
            results = client.request(url, headers=self._headers(), timeout=10)
            if not results: return
            files = jsloads(results).get('streams', [])
            if not files: return

            _INFO = re.compile(r'💾.*')
            local_sources = []
            local_totals = {}

            for file in files:
                try:
                    if 'url' in file: 
                        hash_match = re.search(r'\b\w{40}\b', file['url'])
                        if hash_match: hash = hash_match.group()
                        else: continue
                    else: 
                        hash = file.get('infoHash')
                    
                    if not hash: continue

                    file_title = file['description'].replace('┈➤', '\n').split('\n')
                    file_info_match = [x for x in file_title if _INFO.search(x)]
                    if not file_info_match: continue
                    file_info = file_info_match[0]

                    name = source_utils.clean_name(file_title[0])

                    if not source_utils.check_title(self.title, self.aliases, name.replace('.(Archie.Bunker', ''), self.hdlr, self.year): continue
                    name_info = source_utils.info_from_name(name, self.title, self.year, self.hdlr, self.episode_title)
                    if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
                    if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

                    magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)

                    try:
                        seeders = int(re.search(r'👤\s*(\d+)', file_info).group(1))
                        if self.min_seeders > seeders: continue
                    except: seeders = 0

                    quality, info = source_utils.get_release_quality(name_info, magnet_url)
                    try:
                        size_match = re.findall(r'((?:\d+\,\d+\.\d+|\d+\.\d+|\d+\,\d+|\d+)\s*(?:GB|GiB|Gb|MB|MiB|Mb))', file_info)
                        if size_match:
                            size_str = size_match[-1]
                            dsize, isize = source_utils._size(size_str)
                            info.insert(0, isize)
                        else:
                            dsize = 0
                    except: dsize = 0
                    info = ' | '.join(info)

                    local_sources.append({
                        'source': 'torrent', 'language': 'en', 'direct': False, 'debridonly': True,
                        'provider': 'mediafusion', 'hash': hash, 'url': magnet_url, 'name': name, 'name_info': name_info,
                        'quality': quality, 'info': info, 'size': dsize, 'seeders': seeders
                    })
                    local_totals[quality] = local_totals.get(quality, 0) + 1
                except:
                    continue
            
            self.add_sources_thread_safe(local_sources, local_totals)
        except:
            source_utils.scraper_error('MEDIAFUSION')

    def sources_packs(self, data, hostDict, search_series=False, total_seasons=None, bypass_filter=False):
        self.reset_results()
        if not data: return self.source_results
        
        start_time = time()
        try:
            self.title = data['tvshowtitle'].replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ')
            self.aliases = data['aliases']
            self.imdb = data.get('imdb') or data.get('imdb_id')
            self.year = data['year']
            self.season = data['season']
            self.search_series = search_series
            self.total_seasons = total_seasons
            self.bypass_filter = bypass_filter

            self.undesirables = source_utils.get_undesirables()
            self.check_foreign_audio = source_utils.check_foreign_audio()

            # MediaFusion doesn't have a pack-specific endpoint, it returns everything in the episode/show endpoint
            url = '%s%s' % (self.base_link, self.tvSearch_link % (self.imdb, self.season, data['episode']))
            
            self.get_sources_packs_worker(url)
            
            self.log_results_thread_safe(start_time, suffix='(pack)')
            return self.source_results
        except:
            source_utils.scraper_error('MEDIAFUSION')
            return self.source_results

    def get_sources_packs_worker(self, url):
        try:
            results = client.request(url, headers=self._headers(), timeout=10)
            if not results: return
            files = jsloads(results).get('streams', [])
            if not files: return

            _INFO = re.compile(r'💾.*')
            local_sources = []
            local_totals = {}

            for file in files:
                try:
                    if 'url' in file: 
                        hash_match = re.search(r'\b\w{40}\b', file['url'])
                        if hash_match: hash = hash_match.group()
                        else: continue
                    else: 
                        hash = file.get('infoHash')
                    
                    if not hash: continue

                    file_title = file['description'].replace('┈➤', '\n').split('\n')
                    file_info_match = [x for x in file_title if _INFO.search(x)]
                    if not file_info_match: continue
                    file_info = file_info_match[0]

                    name = source_utils.clean_name(file_title[0])

                    episode_start, episode_end = 0, 0
                    if not self.search_series:
                        if not self.bypass_filter:
                            valid, episode_start, episode_end = source_utils.filter_season_pack(self.title, self.aliases, self.year, self.season, name.replace('.(Archie.Bunker', ''))
                            if not valid: continue
                        package = 'season'
                    else:
                        if not self.bypass_filter:
                            valid, last_season = source_utils.filter_show_pack(self.title, self.aliases, self.imdb, self.year, self.season, name.replace('.(Archie.Bunker', ''), self.total_seasons)
                            if not valid: continue
                        else: 
                            last_season = self.total_seasons
                        package = 'show'

                    name_info = source_utils.info_from_name(name, self.title, self.year, season=self.season, pack=package)
                    if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
                    if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

                    magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
                    try:
                        seeders = int(re.search(r'👤\s*(\d+)', file_info).group(1))
                        if self.min_seeders > seeders: continue
                    except: seeders = 0

                    quality, info = source_utils.get_release_quality(name_info, magnet_url)
                    try:
                        size_match = re.findall(r'((?:\d+\,\d+\.\d+|\d+\.\d+|\d+\,\d+|\d+)\s*(?:GB|GiB|Gb|MB|MiB|Mb))', file_info)
                        if size_match:
                            size_str = size_match[-1]
                            dsize, isize = source_utils._size(size_str)
                            info.insert(0, isize)
                        else:
                            dsize = 0
                    except: dsize = 0
                    info = ' | '.join(info)

                    item = {
                        'source': 'torrent', 'language': 'en', 'direct': False, 'debridonly': True,
                        'provider': 'mediafusion', 'hash': hash, 'url': magnet_url, 'name': name, 'name_info': name_info,
                        'quality': quality, 'info': info, 'size': dsize, 'seeders': seeders, 'package': package
                    }
                    if self.search_series: item.update({'last_season': last_season})
                    elif episode_start: item.update({'episode_start': episode_start, 'episode_end': episode_end})
                    
                    local_sources.append(item)
                    local_totals[quality] = local_totals.get(quality, 0) + 1
                except:
                    continue
            
            self.add_sources_thread_safe(local_sources, local_totals)
        except:
            source_utils.scraper_error('MEDIAFUSION')

    def _headers(self):
        return {'encoded_user_data': (
            'eyJlbmFibGVfY2F0YWxvZ3MiOiBmYWxzZSwgIm1heF9zdHJlYW1zX3Blcl9yZXNvbHV0aW9uIjogOTks'
            'ICJ0b3JyZW50X3NvcnRpbmdfcHJpb3JpdHkiOiBbXSwgImNlcnRpZmljYXRpb25fZmlsdGVyIjogWyJE'
            'aXNhYmxlIl0sICJudWRpdHlfZmlsdGVyIjogWyJEaXNhYmxlIl19'
        )}

class MediaFusionService(ConcurrentScraperBase):
    def __init__(self):
        super().__init__('mediafusion')
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
        return self.scrape_sources(data)

    async def scrape_packs_async(self, data, search_series=False, total_seasons=None, bypass_filter=False):
        return self.scrape_packs(
            data,
            search_series=search_series,
            total_seasons=total_seasons,
            bypass_filter=bypass_filter
        )
