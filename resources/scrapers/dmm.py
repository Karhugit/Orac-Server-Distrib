# -*- coding: utf-8 -*-
import ctypes
import math
import random
import threading
from time import time
import requests

from resources.scrapers.modules import source_utils
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

class source(ConcurrentScraperBase):
	priority = 3
	pack_capable = True
	hasMovies = True
	hasEpisodes = True
	
	def __init__(self):
		super().__init__('dmm')
		self._results_lock = threading.Lock()
		self.language = ['en']
		self.base_link = "https://debridmediamanager.com"
		self.movieSearch_link = '/api/torrents/movie?imdbId=%s'
		# DMM TV search link
		self.tvSearch_link = '/api/torrents/tv?imdbId=%s&seasonNum=%s'
		self.min_seeders = 0
		self.timeout = 7
		self.headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.123 Safari/537.36'}
		self.reset_results()

	def reset_results(self):
		with self._results_lock:
			self.source_results = []
			self.item_totals = {'4K': 0, '1080p': 0, '720p': 0, 'SD': 0, 'CAM': 0}

	def _get_api_auth(self):
		dmmProblemKey, solution = get_secret()
		return {'dmmProblemKey': dmmProblemKey, 'solution': solution}

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
			
			# IDs from Orac
			self.imdb = data.get('show_imdb') or data.get('imdb')
			self.episode_imdb = data.get('episode_imdb')
			self.season = data.get('season')
			self.episode = data.get('episode')
			
			if self.season and self.episode:
				self.hdlr = 'S%02dE%02d' % (int(self.season), int(self.episode))
			else:
				self.hdlr = self.year
			
			log(f"DMM search: IMDB={self.imdb}, EP_IMDB={self.episode_imdb}, S={self.season}, E={self.episode}", LOGDEBUG)
			
			# Refresh auth per request
			api_params = self._get_api_auth()
			self.undesirables = source_utils.get_undesirables()
			self.check_foreign_audio = source_utils.check_foreign_audio()

			urls = []
			if self.season and self.episode:
				# Use episodeNum for a surgical search.
				# We search for the show IMDB + S + E
				url = '%s%s&episodeNum=%s&page=0' % (self.base_link, self.tvSearch_link % (self.imdb, self.season), self.episode)
				urls.append(url)
				
				# If we have a distinct episode IMDB, DMM might find it directly without season info on some endpoints
				# but for /api/torrents/tv it usually wants seasonNum. 
				# Let's stick to the S+E search which is highly reliable if episodeNum is supported.
			else: 
				# Movie search
				urls.append('%s%s&page=0' % (self.base_link, self.movieSearch_link % self.imdb))
				urls.append('%s%s&page=1' % (self.base_link, self.movieSearch_link % self.imdb))

			def worker_func(url):
				self.get_sources_worker(url, api_params)

			self.thread_manager.scrape_urls_optimized(urls, worker_func, timeout=self.timeout + 3)
			
			self.log_results_thread_safe(start_time)
			return self.source_results
		except Exception as e:
			log(f"DMM setup error: {e}", LOGERROR)
			return self.source_results

	def get_sources_worker(self, url, api_params):
		try:
			results = requests.get(url, params=api_params, headers=self.headers, timeout=self.timeout)
			if results.status_code != 200:
				log(f"DMM API error ({results.status_code}): {results.text[:100]}", LOGDEBUG)
				return
				
			json_data = results.json()
			files = json_data.get('results', [])
			if not files:
				return
			
			local_sources = []
			local_totals = {}
			
			# If we used episodeNum, we know the API filtered for us.
			# We should be extremely lenient to ensure we don't filter out valid results.
			is_targeted_ep = "episodeNum=" in url
			
			for file in files:
				try:
					hash = file['hash']
					name = source_utils.clean_name(file['title'])

					# Lenient matching for DMM: trust the IMDB results more
					if not source_utils.check_title(self.title, self.aliases, name, self.hdlr, self.year): 
						# If it's a targeted episode search, we trust the API.
						s_hdlr = self.hdlr if self.season and self.episode else ('S%02d' % int(self.season) if self.season else self.year)
						if is_targeted_ep and s_hdlr.lower() in name.lower().replace('.', '').replace('_', '').replace(' ', ''):
							pass
						else:
							continue

					name_info = source_utils.info_from_name(name, self.title, self.year, self.hdlr, self.episode_title)
					if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
					if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

					magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
					quality, info = source_utils.get_release_quality(name_info, magnet_url)
					try:
						dsize = float(file['fileSize']) / 1024
						isize = f"{dsize:.2f} GB"
						info.insert(0, isize)
					except: dsize = 0
					
					item = {
						'provider': 'dmm', 'source': 'torrent', 'seeders': 0, 'hash': hash, 
						'name': name, 'name_info': name_info, 'quality': quality, 'language': 'en', 'url': magnet_url, 
						'info': ' | '.join(info), 'direct': False, 'debridonly': True, 'size': dsize
					}
					
					local_sources.append(item)
					local_totals[quality] = local_totals.get(quality, 0) + 1
				except: continue
				
			self.add_sources_thread_safe(local_sources, local_totals)
		except Exception as e:
			log(f"DMM worker error: {e}", LOGDEBUG)

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
			self.imdb = data.get('show_imdb') or data.get('imdb')
			self.year = data['year']
			self.season = data['season']
			
			api_params = self._get_api_auth()
			
			urls = []
			# Pack searches don't use episodeNum
			for page in range(2):
				url = '%s%s&page=%s' % (self.base_link, self.tvSearch_link % (self.imdb, self.season), page)
				urls.append(url)
			
			self.undesirables = source_utils.get_undesirables()
			self.check_foreign_audio = source_utils.check_foreign_audio()

			def worker_func(url):
				self.get_sources_packs_worker(url, api_params)

			self.thread_manager.scrape_urls_optimized(urls, worker_func, timeout=self.timeout + 3)
			
			self.log_results_thread_safe(start_time, suffix='(pack)')
			return self.source_results
		except Exception as e:
			log(f"DMM pack setup error: {e}", LOGERROR)
			return self.source_results

	def get_sources_packs_worker(self, url, api_params):
		try:
			results = requests.get(url, params=api_params, headers=self.headers, timeout=self.timeout)
			if results.status_code != 200:
				return
				
			json_data = results.json()
			files = json_data.get('results', [])
			if not files: return
			
			local_sources = []
			local_totals = {}
			
			for file in files:
				try:
					hash = file['hash']
					name = source_utils.clean_name(file['title'])

					episode_start, episode_end = 0, 0
					if not self.search_series:
						if not self.bypass_filter:
							valid, episode_start, episode_end = source_utils.filter_season_pack(self.title, self.aliases, self.year, self.season, name)
							if not valid: continue
						package = 'season'
					else:
						if not self.bypass_filter:
							valid, last_season = source_utils.filter_show_pack(self.title, self.aliases, self.imdb, self.year, self.season, name, self.total_seasons)
							if not valid: continue
						else: last_season = self.total_seasons
						package = 'show'

					name_info = source_utils.info_from_name(name, self.title, self.year, season=self.season, pack=package)
					if source_utils.remove_lang(name_info, self.check_foreign_audio): continue
					if self.undesirables and source_utils.remove_undesirables(name_info, self.undesirables): continue

					magnet_url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
					quality, info = source_utils.get_release_quality(name_info, magnet_url)
					try:
						dsize = float(file['fileSize']) / 1024
						isize = f"{dsize:.2f} GB"
						info.insert(0, isize)
					except: dsize = 0
					
					item = {
						'provider': 'dmm', 'source': 'torrent', 'seeders': 0, 'hash': hash, 
						'name': name, 'name_info': name_info, 'quality': quality, 'language': 'en', 'url': magnet_url, 
						'info': ' | '.join(info), 'direct': False, 'debridonly': True, 'size': dsize, 'package': package
					}
					
					if self.search_series: item.update({'last_season': last_season})
					elif episode_start: item.update({'episode_start': episode_start, 'episode_end': episode_end})
					
					local_sources.append(item)
					local_totals[quality] = local_totals.get(quality, 0) + 1
				except: continue
				
			self.add_sources_thread_safe(local_sources, local_totals)
		except Exception as e:
			log(f"DMM pack worker error: {e}", LOGDEBUG)

def get_secret():
	def calc_value_alg(t, n, const):
		temp = t ^ n
		t = ctypes.c_long((temp * const)).value
		t4 = ctypes.c_long(t << 5).value
		x32 = t & 0xFFFFFFFF
		t5 = ctypes.c_long(x32 >> 27).value
		t6 = t4 | t5
		return t6

	def slice(e, t):
		a = math.floor(len(e) / 2)
		s = e[0:a]
		n = e[a:]
		i = t[0:a]
		o = t[a:]
		l = ""
		for e in range(0, a):
			l += s[e] + i[e]
		temp = l + (o[::-1] + n[::-1])
		return temp

	def generateHash(e):
		t = int(3735928559) ^ int(len(e))
		t = ctypes.c_long(t).value
		a = 1103547991 ^ len(e)
		for s in range(len(e)):
			n = ord(e[s])
			t = calc_value_alg(t, n, 2654435761)
			a = calc_value_alg(a, n, 1597334677)
		t_o = t
		t = ctypes.c_long(t + ctypes.c_long(a * 1566083941).value | 0).value
		a = ctypes.c_long(a + ctypes.c_long(t * 2024237689).value | 0).value
		return (ctypes.c_long(t ^ a).value & 0xFFFFFFFF) >> 0

	ran = random.randrange(10**80)
	myhex = "%064x" % ran
	e = myhex[:8]
	t = int(time())
	a = str(e) + '-' + str(t)
	s = generateHash(a)
	s = hex(s).replace('0x', '')
	n = generateHash("debridmediamanager.com%%fe7#td00rA3vHz%VmI-" + e) # Reverted back to %%
	n = hex(n).replace('0x', '')
	i = slice(s, n)
	dmmProblemKey = a
	solution = i
	return dmmProblemKey, solution

class DmmService(ConcurrentScraperBase):
	def __init__(self):
		super().__init__('dmm')
		self.scraper = source()

	def scrape_sources(self, data):
		return self.scraper.sources(data, hostDict={})

	def scrape_packs(self, data, search_series=False, total_seasons=None, bypass_filter=False):
		return self.scraper.sources_packs(data, hostDict={}, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)

	async def scrape_sources_async(self, data):
		return self.scrape_sources(data)

	async def scrape_packs_async(self, data, search_series=False, total_seasons=None, bypass_filter=False):
		return self.scrape_packs(data, search_series=search_series, total_seasons=total_seasons, bypass_filter=bypass_filter)
