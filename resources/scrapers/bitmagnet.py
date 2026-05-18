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
		super().__init__('bitmagnet')
		self._results_lock = threading.Lock()
		self.language = ['en']
		self.base_link = "https://aiostreamsfortheweebs.midnightignite.me"
		self.movieSearch_link = '/api/v1/search?type=movie&id=%s'
		self.tvSearch_link = '/api/v1/search?type=series&id=%s:%s:%s'
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
				url = '%s%s' % (self.base_link, self.tvSearch_link % (self.imdb, self.season, self.episode))
			else:
				self.hdlr = self.year
				url = '%s%s' % (self.base_link, self.movieSearch_link % self.imdb)
			
			self.undesirables = source_utils.get_undesirables()
			self.check_foreign_audio = source_utils.check_foreign_audio()
			
			self.thread_manager.scrape_urls_optimized([url], self.get_sources_worker, timeout=10)
			
			self.log_results_thread_safe(start_time)
			return self.source_results
		except Exception as e:
			log(f"BITMAGNET error in setup: {e}", LOGERROR)
			return self.source_results

	def get_sources_worker(self, url):
		try:
			results = client.request(url, headers=self._headers(), timeout=7)
			if not results: return
			files = jsloads(results).get('data', {}).get('results', [])
			if not files: return
			
			local_sources = []
			local_totals = {}
			
			for file in files:
				try:
					hash = file.get('infoHash')
					if not hash: continue
					file_title = file.get('folderName') or file.get('filename')
					if not file_title: continue
					name = source_utils.clean_name(file_title)

					if not source_utils.check_title(self.title, self.aliases, name.replace('.(Archie.Bunker', ''), self.hdlr, self.year): continue
					name_info = source_utils.info_from_name(name, self.title, self.year, self.hdlr, self.episode_title)
					if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
					if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

					magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
					
					try:
						seeders = file['seeders']
						if self.min_seeders > seeders: continue
					except: seeders = 0

					quality, info = source_utils.get_release_quality(name_info, magnet_url)
					try:
						size = f"{float(file['size']) / 1073741824:.2f} GB"
						dsize, isize = source_utils._size(size)
						info.insert(0, isize)
					except: dsize = 0
					
					item = {
						'provider': 'bitmagnet', 'source': 'torrent', 'seeders': seeders, 'hash': hash, 'name': name, 'name_info': name_info,
						'quality': quality, 'language': 'en', 'url': magnet_url, 'info': ' | '.join(info), 'direct': False, 'debridonly': True, 'size': dsize
					}
					
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
			
			url = '%s%s' % (self.base_link, self.tvSearch_link % (self.imdb, self.season, data['episode']))
			
			self.undesirables = source_utils.get_undesirables()
			self.check_foreign_audio = source_utils.check_foreign_audio()
			
			self.thread_manager.scrape_urls_optimized([url], self.get_sources_packs_worker, timeout=10)
			
			self.log_results_thread_safe(start_time, suffix='(pack)')
			return self.source_results
		except Exception as e:
			log(f"BITMAGNET pack error: {e}", LOGERROR)
			return self.source_results

	def get_sources_packs_worker(self, url):
		try:
			results = client.request(url, headers=self._headers(), timeout=7)
			if not results: return
			files = jsloads(results).get('data', {}).get('results', [])
			if not files: return
			
			local_sources = []
			local_totals = {}
			
			for file in files:
				try:
					hash = file['infoHash']
					file_title = file['folderName'] or file['filename']
					name = source_utils.clean_name(file_title)

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
					
					try:
						seeders = file['seeders']
						if self.min_seeders > seeders: continue
					except: seeders = 0

					quality, info = source_utils.get_release_quality(name_info, magnet_url)
					try:
						size = f"{float(file['size']) / 1073741824:.2f} GB"
						dsize, isize = source_utils._size(size)
						info.insert(0, isize)
					except: dsize = 0
					
					item = {
						'provider': 'bitmagnet', 'source': 'torrent', 'seeders': seeders, 'hash': hash, 'name': name, 'name_info': name_info, 'quality': quality,
						'language': 'en', 'url': magnet_url, 'info': ' | '.join(info), 'direct': False, 'debridonly': True, 'size': dsize, 'package': package
					}
					
					if self.search_series: item.update({'last_season': last_season})
					elif episode_start: item.update({'episode_start': episode_start, 'episode_end': episode_end})
					
					local_sources.append(item)
					local_totals[quality] = local_totals.get(quality, 0) + 1
				except: continue
				
			self.add_sources_thread_safe(local_sources, local_totals)
		except: pass

	def _headers(self):
		return {'x-aiostreams-user-data': (
			'ewogICJzZXJ2aWNlcyI6IFsKICAgIHsKICAgICAgImlkIjogImFsbGRlYnJpZCIsCiAgICAgICJlbmFi'
			'bGVkIjogdHJ1ZSwKICAgICAgImNyZWRlbnRpYWxzIjogewogICAgICAgICJhcGlLZXkiOiAic3RhdGlj'
			'RGVtb0FwaWtleVByZW0iCiAgICAgIH0KICAgIH0KICBdLAogICJwcmVzZXRzIjogWwogICAgewogICAg'
			'ICAidHlwZSI6ICJiaXRtYWduZXQiLAogICAgICAiaW5zdGFuY2VJZCI6ICIzYjMiLAogICAgICAiZW5h'
			'YmxlZCI6IHRydWUsCiAgICAgICJvcHRpb25zIjogewogICAgICAgICJuYW1lIjogIkJpdG1hZ25ldCIs'
			'CiAgICAgICAgInRpbWVvdXQiOiA2NTAwLAogICAgICAgICJtZWRpYVR5cGVzIjogW10KICAgICAgfQog'
			'ICAgfQogIF0sCiAgImZvcm1hdHRlciI6IHsKICAgICJpZCI6ICJ0b3JyZW50aW8iLAogICAgImRlZmlu'
			'aXRpb24iOiB7CiAgICAgICJuYW1lIjogIiIsCiAgICAgICJkZXNjcmlwdGlvbiI6ICIiCiAgICB9CiAg'
			'fSwKICAic29ydENyaXRlcmlhIjogewogICAgImdsb2JhbCI6IFtdCiAgfQp9'
		)}

class BitmagnetService:
	def __init__(self):
		self.scraper = source()

	def scrape_sources(self, data):
		return self.scraper.sources(data, hostDict={})

	def scrape_packs(self, data, search_series=False, total_seasons=None, bypass_filter=False):
		return self.scraper.sources_packs(data, hostDict={}, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)

	async def scrape_sources_async(self, data):
		return self.scrape_sources(data)

	async def scrape_packs_async(self, data, search_series=False, total_seasons=None, bypass_filter=False):
		return self.scrape_packs(data, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)
