# -*- coding: utf-8 -*-
import threading
import xml.etree.ElementTree as ET
import hashlib
import requests
from time import time

from resources.scrapers.modules import source_utils
from resources.scrapers.modules.control import setting as getSetting
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

class source(ConcurrentScraperBase):
	priority = 3
	pack_capable = True
	hasMovies = True
	hasEpisodes = True
	
	def __init__(self):
		super().__init__('tbtorznab')
		self._results_lock = threading.Lock()
		self.token = getSetting('torbox.token')
		self.language = ['en']
		self.headers = {'user-agent': 'Orac for Kodi'}
		self.base_link = "https://search-api.torbox.app"
		self.movieSearch_link = '/torznab/api'
		self.tvSearch_link = '/torznab/api'
		self.min_seeders = 0
		self.timeout = 10
		self.reset_results()

	def reset_results(self):
		with self._results_lock:
			self.source_results = []
			self.item_totals = {'4K': 0, '1080p': 0, '720p': 0, 'SD': 0, 'CAM': 0}

	def sources(self, data, hostDict):
		self.reset_results()
		if not data or not self.token: return self.source_results
		
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
				params = {'t': 'tvsearch', 'limit': 100, 'imdbid': self.imdb, 'season': self.season, 'ep': self.episode}
			else:
				self.hdlr = self.year
				url = '%s%s' % (self.base_link, self.movieSearch_link)
				params = {'t': 'movie', 'limit': 100, 'imdbid': self.imdb}
			
			params['apikey'] = self.token
			self.undesirables = source_utils.get_undesirables()
			self.check_foreign_audio = source_utils.check_foreign_audio()
			
			def worker_func(dummy_url):
				try:
					results = requests.get(url, params=params, headers=self.headers, timeout=self.timeout)
					if not results.text: return
					files = ET.fromstring(results.text)
					self._process_files(files)
				except Exception as e:
					log(f"TBTORZNAB request error: {e}", LOGERROR)

			self.thread_manager.scrape_urls_optimized(['dummy'], worker_func, timeout=self.timeout + 5)
			
			self.log_results_thread_safe(start_time)
			return self.source_results
		except Exception as e:
			log(f"TBTORZNAB setup error: {e}", LOGERROR)
			return self.source_results

	def _process_files(self, files):
		local_sources = []
		local_totals = {}
		
		for file in files.findall('.//item'):
			try:
				attr_dict = {}
				for attr in file.findall('torznab:attr', {'torznab': 'http://torznab.com/schemas/2015/feed'}):
					key, val = attr.get('name'), attr.get('value')
					if key and val: attr_dict[key] = val
				
				hash = attr_dict.get('infohash', '')
				if not hash: continue
				
				raw_name = file.find('title').text
				name = source_utils.clean_name(raw_name)

				if not source_utils.check_title(self.title, self.aliases, name.replace('.(Archie.Bunker', ''), self.hdlr, self.year): continue
				name_info = source_utils.info_from_name(name, self.title, self.year, self.hdlr, self.episode_title)
				if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
				if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

				url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
				try:
					seeders = int(attr_dict.get('seeders', 0))
					if self.min_seeders > seeders: continue
				except: seeders = 0

				quality, info = source_utils.get_release_quality(name_info, url)
				try:
					size_bytes = float(attr_dict.get('size', '0'))
					dsize = size_bytes / 1073741824
					isize = f"{dsize:.2f} GB"
					info.insert(0, isize)
				except: dsize = 0
				
				item = {
					'provider': 'tbtorznab', 'source': 'torrent', 'seeders': seeders, 'hash': hash, 
					'name': name, 'name_info': name_info, 'quality': quality, 'language': 'en', 'url': url, 
					'info': ' | '.join(info), 'direct': False, 'debridonly': True, 'size': dsize
				}
				
				local_sources.append(item)
				local_totals[quality] = local_totals.get(quality, 0) + 1
			except: continue
			
		self.add_sources_thread_safe(local_sources, local_totals)

	def sources_packs(self, data, hostDict, search_series=False, total_seasons=None, bypass_filter=False):
		self.reset_results()
		if not data or not self.token: return self.source_results
		
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
			# For TorZnab packs, we often search by season if search_series is True we search for show
			# but here we use the specific show/season request
			if search_series:
				params = {'t': 'tvsearch', 'limit': 100, 'imdbid': self.imdb}
			else:
				params = {'t': 'tvsearch', 'limit': 100, 'imdbid': self.imdb, 'season': self.season}
			
			params['apikey'] = self.token
			self.undesirables = source_utils.get_undesirables()
			self.check_foreign_audio = source_utils.check_foreign_audio()

			def worker_func(dummy_url):
				try:
					results = requests.get(url, params=params, headers=self.headers, timeout=self.timeout)
					if not results.text: return
					files = ET.fromstring(results.text)
					self._process_packs_files(files)
				except Exception as e:
					log(f"TBTORZNAB pack request error: {e}", LOGERROR)

			self.thread_manager.scrape_urls_optimized(['dummy'], worker_func, timeout=self.timeout + 5)
			
			self.log_results_thread_safe(start_time, suffix='(pack)')
			return self.source_results
		except Exception as e:
			log(f"TBTORZNAB pack setup error: {e}", LOGERROR)
			return self.source_results

	def _process_packs_files(self, files):
		local_sources = []
		local_totals = {}
		
		for file in files.findall('.//item'):
			try:
				attr_dict = {}
				for attr in file.findall('torznab:attr', {'torznab': 'http://torznab.com/schemas/2015/feed'}):
					key, val = attr.get('name'), attr.get('value')
					if key and val: attr_dict[key] = val
				
				hash = attr_dict.get('infohash', '')
				if not hash: continue
				
				raw_name = file.find('title').text
				name = source_utils.clean_name(raw_name)

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

				name_info = source_utils.info_from_name(name, self.title, self.year, season=self.season, pack=package)
				if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
				if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

				url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
				try:
					seeders = int(attr_dict.get('seeders', 0))
					if self.min_seeders > seeders: continue
				except: seeders = 0

				quality, info = source_utils.get_release_quality(name_info, url)
				try:
					size_bytes = float(attr_dict.get('size', '0'))
					dsize = size_bytes / 1073741824
					isize = f"{dsize:.2f} GB"
					info.insert(0, isize)
				except: dsize = 0
				
				item = {
					'provider': 'tbtorznab', 'source': 'torrent', 'seeders': seeders, 'hash': hash, 
					'name': name, 'name_info': name_info, 'quality': quality, 'language': 'en', 'url': url, 
					'info': ' | '.join(info), 'direct': False, 'debridonly': True, 'size': dsize, 'package': package
				}
				
				if self.search_series: item.update({'last_season': last_season})
				elif episode_start: item.update({'episode_start': episode_start, 'episode_end': episode_end})
				
				local_sources.append(item)
				local_totals[quality] = local_totals.get(quality, 0) + 1
			except: continue
			
		self.add_sources_thread_safe(local_sources, local_totals)

class TBTorZnabService(ConcurrentScraperBase):
	def __init__(self):
		super().__init__('tbtorznab')
		self.scraper = source()

	def scrape_sources(self, data):
		return self.scraper.sources(data, hostDict={})

	def scrape_packs(self, data, search_series=False, total_seasons=None, bypass_filter=False):
		return self.scraper.sources_packs(data, hostDict={}, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)

	async def scrape_sources_async(self, data):
		return self.scrape_sources(data)

	async def scrape_packs_async(self, data, search_series=False, total_seasons=None, bypass_filter=False):
		return self.scrape_packs(data, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)
