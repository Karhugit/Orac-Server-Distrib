# -*- coding: utf-8 -*-
import re
import threading
from urllib.parse import quote_plus, unquote_plus
from time import time

from resources.scrapers.modules import client
from resources.scrapers.modules import source_utils
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

class source(ConcurrentScraperBase):
	priority = 3
	pack_capable = True
	hasMovies = True
	hasEpisodes = True
	
	def __init__(self):
		super().__init__('torrentdownload')
		self._results_lock = threading.Lock()
		self.language = ['en']
		self.base_link = "https://www.torrentdownload.info"
		self.search_link = '/search?q=%s'
		self.min_seeders = 0
		self.reset_results()

	def reset_results(self):
		with self._results_lock:
			self.source_results = []
			self.item_totals = {'4K': 0, '1080p': 0, '720p': 0, 'SD': 0, 'CAM': 0}

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
			else:
				self.title = data['title'].replace('&', 'and').replace('/', ' ').replace('$', 's')
				self.episode_title = None
				self.hdlr = self.year
			
			query = '%s %s' % (re.sub(r'[^A-Za-z0-9\s\.-]+', '', self.title), self.hdlr)
			urls = []
			url = '%s%s' % (self.base_link, self.search_link % quote_plus(query))
			urls.append(url)
			urls.append(url + '&p=2')
			
			self.undesirables = source_utils.get_undesirables()
			self.check_foreign_audio = source_utils.check_foreign_audio()
			
			self.thread_manager.scrape_urls_optimized(urls, self.get_sources_worker, timeout=20)
			
			self.log_results_thread_safe(start_time)
			return self.source_results
		except Exception as e:
			log(f"TORRENTDOWNLOAD error in setup: {e}", LOGERROR)
			return self.source_results

	def get_sources_worker(self, url):
		try:
			results = client.request(url, timeout=7)
			if not results: return
			rows = client.parseDOM(results, 'tr')
			if not rows: return
			
			local_sources = []
			local_totals = {}
			
			for row in rows:
				try:
					if any(value in row for value in ('<th', 'nofollow')): continue
					columns = re.findall(r'<td.*?>(.+?)</td>', row, re.DOTALL)
					if not columns: continue

					link_match = re.search(r'href\s*=\s*["\']/(.+?)["\']>', columns[0], re.I)
					if not link_match: continue
					link = link_match.group(1).split('/')
					hash = link[0]
					name = source_utils.clean_name(unquote_plus(link[1]).replace('&amp;', '&'))

					if not source_utils.check_title(self.title, self.aliases, name, self.hdlr, self.year): continue
					name_info = source_utils.info_from_name(name, self.title, self.year, self.hdlr, self.episode_title)
					if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
					if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

					if not self.episode_title:
						ep_strings = [r'[.-]s\d{2}e\d{2}([.-]?)', r'[.-]s\d{2}([.-]?)', r'[.-]season[.-]?\d{1,2}[.-]?']
						if any(re.search(item, name.lower()) for item in ep_strings): continue

					magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
					try:
						seeders = int(columns[3].replace(',', ''))
						if self.min_seeders > seeders: continue
					except: seeders = 0

					quality, info = source_utils.get_release_quality(name_info, magnet_url)
					try:
						dsize, isize = source_utils._size(columns[2])
						info.insert(0, isize)
					except: dsize = 0
					
					item = {'provider': 'torrentdownload', 'source': 'torrent', 'seeders': seeders, 'hash': hash, 'name': name, 'name_info': name_info,
							'quality': quality, 'language': 'en', 'url': magnet_url, 'info': ' | '.join(info), 'direct': False, 'debridonly': True, 'size': dsize}
					
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

			urls = ['%s%s' % (self.base_link, self.search_link % quote_plus(q)) for q in queries]
			
			self.thread_manager.scrape_urls_optimized(urls, self.get_sources_packs_worker, timeout=20)
			
			self.log_results_thread_safe(start_time, suffix='(pack)')
			return self.source_results
		except Exception as e:
			log(f"TORRENTDOWNLOAD pack error: {e}", LOGERROR)
			return self.source_results

	def get_sources_packs_worker(self, link):
		try:
			results = client.request(link, timeout=7)
			if not results: return
			rows = client.parseDOM(results, 'tr')
			if not rows: return
			
			local_sources = []
			local_totals = {}
			
			for row in rows:
				try:
					if any(value in row for value in ('<th', 'nofollow')): continue
					columns = re.findall(r'<td.*?>(.+?)</td>', row, re.DOTALL)
					if not columns: continue

					link_match = re.search(r'href\s*=\s*["\']/(.+?)["\']>', columns[0], re.I)
					if not link_match: continue
					link = link_match.group(1).split('/')
					hash = link[0]
					name = source_utils.clean_name(unquote_plus(link[1]).replace('&amp;', '&'))

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

					magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
					try:
						seeders = int(columns[3].replace(',', ''))
						if self.min_seeders > seeders: continue
					except: seeders = 0

					quality, info = source_utils.get_release_quality(name_info, magnet_url)
					try:
						dsize, isize = source_utils._size(columns[2])
						info.insert(0, isize)
					except: dsize = 0
					
					item = {'provider': 'torrentdownload', 'source': 'torrent', 'seeders': seeders, 'hash': hash, 'name': name, 'name_info': name_info, 'quality': quality,
							'language': 'en', 'url': magnet_url, 'info': ' | '.join(info), 'direct': False, 'debridonly': True, 'size': dsize, 'package': package}
					
					if self.search_series: item.update({'last_season': last_season})
					elif episode_start: item.update({'episode_start': episode_start, 'episode_end': episode_end})
					
					local_sources.append(item)
					local_totals[quality] = local_totals.get(quality, 0) + 1
				except: continue
				
			self.add_sources_thread_safe(local_sources, local_totals)
		except: pass

class TorrentDownloadService(ConcurrentScraperBase):
	def __init__(self):
		super().__init__('torrentdownload')
		self.scraper = source()

	def scrape_sources(self, data):
		return self.scraper.sources(data, hostDict={})

	def scrape_packs(self, data, search_series=False, total_seasons=None, bypass_filter=False):
		return self.scraper.sources_packs(data, hostDict={}, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)

	async def scrape_sources_async(self, data):
		return self.scrape_sources(data)

	async def scrape_packs_async(self, data, search_series=False, total_seasons=None, bypass_filter=False):
		return self.scrape_packs(data, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)