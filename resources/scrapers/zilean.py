# -*- coding: utf-8 -*-
import threading
from json import loads as jsloads
from time import time

from resources.scrapers.modules import client
from resources.scrapers.modules import source_utils
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

class source(ConcurrentScraperBase):
	priority = 1
	pack_capable = True
	hasMovies = True
	hasEpisodes = True
	
	def __init__(self):
		super().__init__('zilean')
		self._results_lock = threading.Lock()
		self.language = ['en']
		self.base_links = [
			"https://zilean.stremio.ru",
			"https://zileanfortheweebs.midnightignite.me"
		]
		self.movieSearch_link = '/dmm/filtered?ImdbId=%s'
		self.tvSearch_link = '/dmm/filtered?ImdbId=%s&Season=%s&Episode=%s'
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
				link = self.tvSearch_link % (self.imdb, self.season, self.episode)
			else:
				self.hdlr = self.year
				link = self.movieSearch_link % self.imdb
			
			self.undesirables = source_utils.get_undesirables()
			self.check_foreign_audio = source_utils.check_foreign_audio()
			
			# Zilean returns a single JSON blob for the ID, so we don't really need a "batch" scrape, 
			# but we use the thread_manager for consistency and timing stats.
			for i, base_link in enumerate(self.base_links):
				url = '%s%s' % (base_link, link)
				self.thread_manager.scrape_urls_optimized([url], self.get_sources_worker, timeout=10)
				if self.source_results:
					if i > 0:
						# Promote working link to top
						self.base_links.insert(0, self.base_links.pop(i))
					break
			
			self.log_results_thread_safe(start_time)
			return self.source_results
		except Exception as e:
			log(f"ZILEAN error in setup: {e}", LOGERROR)
			return self.source_results

	def get_sources_worker(self, url):
		try:
			results = client.request(url, timeout=7)
			if not results: return
			files = jsloads(results)
			if not files: return
			
			local_sources = []
			local_totals = {}
			
			for file in files:
				try:
					hash = file['info_hash']
					name = source_utils.clean_name(file['raw_title'])

					if not source_utils.check_title(self.title, self.aliases, name.replace('.(Archie.Bunker', ''), self.hdlr, self.year): continue
					name_info = source_utils.info_from_name(name, self.title, self.year, self.hdlr, self.episode_title)
					if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
					if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

					magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
					quality, info = source_utils.get_release_quality(name_info, magnet_url)
					try:
						# Zilean size is in bytes usually, converted to GB for Orac if not already
						size_bytes = float(file["size"])
						dsize = size_bytes / (1024**3)
						isize = f"{dsize:.2f} GB"
						info.insert(0, isize)
					except: dsize = 0
					
					item = {'provider': 'zilean', 'source': 'torrent', 'seeders': 0, 'hash': hash, 'name': name, 'name_info': name_info,
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
			self.title = data['tvshowtitle'].replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ')
			self.aliases = data['aliases']
			self.imdb = data['imdb']
			self.year = data['year']
			self.season = data['season']
			
			# Zilean packs come from the same endpoint for the specific IMDB/Season/Episode.
			# Since Orac calls sources() and sources_packs() in parallel tasks, 
			# we just re-fetch here if needed to stay independent.
			link = self.tvSearch_link % (self.imdb, self.season, data['episode'])
			
			self.undesirables = source_utils.get_undesirables()
			self.check_foreign_audio = source_utils.check_foreign_audio()
			
			for i, base_link in enumerate(self.base_links):
				url = '%s%s' % (base_link, link)
				self.thread_manager.scrape_urls_optimized([url], self.get_sources_packs_worker, timeout=10)
				if self.source_results:
					if i > 0:
						# Promote working link to top
						self.base_links.insert(0, self.base_links.pop(i))
					break
			
			self.log_results_thread_safe(start_time, suffix='(pack)')
			return self.source_results
		except Exception as e:
			log(f"ZILEAN pack error: {e}", LOGERROR)
			return self.source_results

	def get_sources_packs_worker(self, url):
		try:
			results = client.request(url, timeout=7)
			if not results: return
			files = jsloads(results)
			if not files: return
			
			local_sources = []
			local_totals = {}
			
			for file in files:
				try:
					hash = file['info_hash']
					name = source_utils.clean_name(file['raw_title'])

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

					magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
					quality, info = source_utils.get_release_quality(name_info, magnet_url)
					try:
						size_bytes = float(file["size"])
						dsize = size_bytes / (1024**3)
						isize = f"{dsize:.2f} GB"
						info.insert(0, isize)
					except: dsize = 0
					
					item = {'provider': 'zilean', 'source': 'torrent', 'seeders': 0, 'hash': hash, 'name': name, 'name_info': name_info, 'quality': quality,
							'language': 'en', 'url': magnet_url, 'info': ' | '.join(info), 'direct': False, 'debridonly': True, 'size': dsize, 'package': package}
					
					if self.search_series: item.update({'last_season': last_season})
					elif episode_start: item.update({'episode_start': episode_start, 'episode_end': episode_end})
					
					local_sources.append(item)
					local_totals[quality] = local_totals.get(quality, 0) + 1
				except: continue
				
			self.add_sources_thread_safe(local_sources, local_totals)
		except: pass

class ZileanService(ConcurrentScraperBase):
	def __init__(self):
		super().__init__('zilean')
		self.scraper = source()

	def scrape_sources(self, data):
		return self.scraper.sources(data, hostDict={})

	def scrape_packs(self, data, search_series=False, total_seasons=None, bypass_filter=False):
		return self.scraper.sources_packs(data, hostDict={}, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)

	async def scrape_sources_async(self, data):
		return self.scrape_sources(data)

	async def scrape_packs_async(self, data, search_series=False, total_seasons=None, bypass_filter=False):
		return self.scrape_packs(data, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)
