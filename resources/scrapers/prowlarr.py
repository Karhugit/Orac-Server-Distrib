# -*- coding: utf-8 -*-
import hashlib
import requests
import threading
from time import time

from resources.scrapers.modules import source_utils
from resources.scrapers.modules.control import setting as getSetting, addonInfo
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

class source(ConcurrentScraperBase):
	priority = 1
	pack_capable = True
	hasMovies = True
	hasEpisodes = True
	
	def __init__(self):
		super().__init__('prowlarr')
		self._results_lock = threading.Lock()
		self.token = getSetting('prowlarr.token')
		self.language = ['en']
		self.headers = {'user-agent': f"Orac/{addonInfo('version')}", 'x-api-key': self.token}
		self.base_link = getSetting('prowlarr.url').rstrip('/')
		self.movieSearch_link = '/api/v1/search'
		self.tvSearch_link = '/api/v1/search'
		self.min_seeders = 0
		self.timeout = 10
		self.reset_results()

	def reset_results(self):
		with self._results_lock:
			self.source_results = []
			self.item_totals = {'4K': 0, '1080p': 0, '720p': 0, 'SD': 0, 'CAM': 0}

	def sources(self, data, hostDict):
		self.reset_results()
		if not (data and self.token): return self.source_results
		
		try:
			start_time = time()
			self.title = data['tvshowtitle'] if 'tvshowtitle' in data else data['title']
			self.title = self.title.replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ')
			self.aliases = data['aliases']
			self.episode_title = data['title'] if 'tvshowtitle' in data else None
			self.year = data['year']
			self.imdb = data['imdb']
			
			if 'tvshowtitle' in data:
				self.season = data['season']
				self.episode = data['episode']
				self.hdlr = 'S%02dE%02d' % (int(self.season), int(self.episode))
				url = '%s%s' % (self.base_link, self.tvSearch_link)
				params = {'type': 'search', 'limit': 100, 'categories': 5000, 'query': '%s %s' % (self.title.lower(), self.hdlr)}
			else:
				self.hdlr = self.year
				url = '%s%s' % (self.base_link, self.movieSearch_link)
				params = {'type': 'search', 'limit': 100, 'categories': 2000, 'query': self.title.lower()}
			
			self.undesirables = source_utils.get_undesirables()
			self.check_foreign_audio = source_utils.check_foreign_audio()
			
			# Prowlarr is a single API call, but we use thread_manager for timing and consistency
			def worker_func(dummy_url):
				try:
					results = requests.get(url, params=params, headers=self.headers, timeout=self.timeout)
					files = results.json()
					if not files: return
					self._process_files(files)
				except Exception as e:
					log(f"PROWLARR request error: {e}", LOGERROR)

			self.thread_manager.scrape_urls_optimized(['dummy'], worker_func, timeout=self.timeout + 5)
			
			self.log_results_thread_safe(start_time)
			return self.source_results
		except Exception as e:
			log(f"PROWLARR setup error: {e}", LOGERROR)
			return self.source_results

	def _process_files(self, files):
		local_sources = []
		local_totals = {}
		
		for file in files:
			try:
				name = source_utils.clean_name(file['title'])

				if not source_utils.check_title(self.title, self.aliases, name.replace('.(Archie.Bunker', ''), self.hdlr, self.year): continue
				name_info = source_utils.info_from_name(name, self.title, self.year, self.hdlr, self.episode_title)
				if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
				if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

				if file['protocol'] == 'usenet':
					url = file['downloadUrl']
					seeders = int(file.get('age', 0)) # Using age as seeders for usenet in Orac logic often
					info_hash = file.get('infoHash') or hashlib.md5(file['fileName'].encode()).hexdigest()
				elif 'infoHash' in file:
					url = 'magnet:?xt=urn:btih:%s&dn=%s' % (file['infoHash'], name)
					info_hash = file['infoHash']
					try:
						seeders = int(file['seeders'])
						if self.min_seeders > seeders: continue
					except: seeders = 0
				else: continue

				quality, info = source_utils.get_release_quality(name_info, url)
				try:
					dsize = float(file['size']) / 1073741824
					isize = f"{dsize:.2f} GB"
					info.insert(0, isize)
				except: dsize = 0
				
				item = {
					'provider': file.get('indexer', 'prowlarr'), 'source': file['protocol'], 'seeders': seeders, 'hash': info_hash, 
					'name': name, 'name_info': name_info, 'quality': quality, 'language': 'en', 'url': url, 
					'info': ' | '.join(info), 'direct': False, 'debridonly': True, 'size': dsize
				}
				
				local_sources.append(item)
				local_totals[quality] = local_totals.get(quality, 0) + 1
			except: continue
			
		self.add_sources_thread_safe(local_sources, local_totals)

	def sources_packs(self, data, hostDict, search_series=False, total_seasons=None, bypass_filter=False):
		self.reset_results()
		if not (data and self.token): return self.source_results
		
		try:
			start_time = time()
			self.search_series = search_series
			self.total_seasons = total_seasons
			self.bypass_filter = bypass_filter
			self.title = data['tvshowtitle'].replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ')
			self.aliases = data['aliases']
			self.imdb = data['imdb']
			self.year = data['year']
			self.season = data['season']
			
			url = '%s%s' % (self.base_link, self.tvSearch_link)
			# For packs we search for the specific show/season
			if search_series:
				params = {'type': 'search', 'limit': 100, 'categories': 5000, 'query': '%s' % self.title.lower()}
			else:
				params = {'type': 'search', 'limit': 100, 'categories': 5000, 'query': '%s Season %s' % (self.title.lower(), self.season)}

			self.undesirables = source_utils.get_undesirables()
			self.check_foreign_audio = source_utils.check_foreign_audio()

			def worker_func(dummy_url):
				try:
					results = requests.get(url, params=params, headers=self.headers, timeout=self.timeout)
					files = results.json()
					if not files: return
					self._process_packs_files(files)
				except Exception as e:
					log(f"PROWLARR pack request error: {e}", LOGERROR)

			self.thread_manager.scrape_urls_optimized(['dummy'], worker_func, timeout=self.timeout + 5)
			
			self.log_results_thread_safe(start_time, suffix='(pack)')
			return self.source_results
		except Exception as e:
			log(f"PROWLARR pack setup error: {e}", LOGERROR)
			return self.source_results

	def _process_packs_files(self, files):
		local_sources = []
		local_totals = {}
		
		for file in files:
			try:
				name = source_utils.clean_name(file['title'])

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
					else: last_season = self.total_seasons
					package = 'show'

				if file['protocol'] == 'usenet':
					url = file['downloadUrl']
					seeders = int(file.get('age', 0))
					info_hash = file.get('infoHash') or hashlib.md5(file['fileName'].encode()).hexdigest()
				elif 'infoHash' in file:
					url = 'magnet:?xt=urn:btih:%s&dn=%s' % (file['infoHash'], name)
					info_hash = file['infoHash']
					try:
						seeders = int(file['seeders'])
						if self.min_seeders > seeders: continue
					except: seeders = 0
				else: continue

				name_info = source_utils.info_from_name(name, self.title, self.year, season=self.season, pack=package)
				if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
				if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

				quality, info = source_utils.get_release_quality(name_info, url)
				try:
					dsize = float(file['size']) / 1073741824
					isize = f"{dsize:.2f} GB"
					info.insert(0, isize)
				except: dsize = 0
				
				item = {
					'provider': file.get('indexer', 'prowlarr'), 'source': file['protocol'], 'seeders': seeders, 'hash': info_hash, 
					'name': name, 'name_info': name_info, 'quality': quality, 'language': 'en', 'url': url, 
					'info': ' | '.join(info), 'direct': False, 'debridonly': True, 'size': dsize, 'package': package
				}
				
				if self.search_series: item.update({'last_season': last_season})
				elif episode_start: item.update({'episode_start': episode_start, 'episode_end': episode_end})
				
				local_sources.append(item)
				local_totals[quality] = local_totals.get(quality, 0) + 1
			except: continue
			
		self.add_sources_thread_safe(local_sources, local_totals)

class ProwlarrService(ConcurrentScraperBase):
	def __init__(self):
		super().__init__('prowlarr')
		self.scraper = source()

	def scrape_sources(self, data):
		return self.scraper.sources(data, hostDict={})

	def scrape_packs(self, data, search_series=False, total_seasons=None, bypass_filter=False):
		return self.scraper.sources_packs(data, hostDict={}, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)

	async def scrape_sources_async(self, data):
		return self.scrape_sources(data)

	async def scrape_packs_async(self, data, search_series=False, total_seasons=None, bypass_filter=False):
		return self.scrape_packs(data, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)
