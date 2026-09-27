# created by Venom for Fenomscrapers (updated 3-02-2022)
"""
	Cocoscrapers Project
"""

import re, json, threading
from resources.scrapers.modules import client
from resources.scrapers.modules import source_utils
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase

timeout = 10


class source(ConcurrentScraperBase):
	priority = 2
	pack_capable = True
	hasMovies = True
	hasEpisodes = True
	
	def __init__(self):
		super().__init__('comet')
		self._results_lock = threading.Lock()
		self.language = ['en']
		self.base_link = [
			"https://comet.stremio.ru",
			"https://comet.feels.legal",
			"https://cometfortheweebs.midnightignite.me"
		]
		self.movieSearch_link = "/stream/movie/%s.json"
		self.tvSearch_link = "/stream/series/%s:%s:%s.json"
		self._params = 'eyJtYXhSZXN1bHRzUGVyUmVzb2x1dGlvbiI6MCwibWF4U2l6ZSI6MCwiY2FjaGVkT25seSI6ZmFsc2UsInJlbW92ZVRyYXNoIjp0cnVlLCJyZXN1bHRGb3JtYXQiOlsidGl0bGUiLCJtZXRhZGF0YSIsInNpemUiLCJsYW5ndWFnZXMiXSwiZGVicmlkU2VydmljZSI6InRvcnJlbnQiLCJkZWJyaWRBcGlLZXkiOiIiLCJkZWJyaWRTdHJlYW1Qcm94eVBhc3N3b3JkIjoiIiwibGFuZ3VhZ2VzIjp7InJlcXVpcmVkIjpbXSwiZXhjbHVkZSI6W10sInByZWZlcnJlZCI6W119LCJyZXNvbHV0aW9ucyI6e30sIm9wdGlvbnMiOnsicmVtb3ZlX3JhbmtzX3VuZGVyIjotMTAwMDAwMDAwMDAsImFsbG93X2VuZ2xpc2hfaW5fbGFuZ3VhZ2VzIjpmYWxzZSwicmVtb3ZlX3Vua25vd25fbGFuZ3VhZ2VzIjpmYWxzZX19'
		self.min_seeders = 0

	def reset_results(self):
		with self._results_lock:
			self.source_results = []
			self.item_totals = {'4K': 0, '1080p': 0, '720p': 0, 'SD': 0, 'CAM': 0}

	def sources(self, data, hostDict):
		self.reset_results()
		sources = []
		if not data: return sources
		
		try:
			title = data['tvshowtitle'] if 'tvshowtitle' in data else data['title']
			title = title.replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ')
			aliases = data['aliases']
			episode_title = data['title'] if 'tvshowtitle' in data else None
			year = data['year']
			imdb = data.get('imdb') or data.get('imdb_id')
			
			if not imdb:
				log("COMET: No IMDB ID provided, skipping", LOGINFO)
				return sources

			if 'tvshowtitle' in data:
				season = data['season']
				episode = data['episode']
				hdlr = 'S%02dE%02d' % (int(season), int(episode))
				path = self.tvSearch_link % (imdb, season, episode)
			else:
				path = self.movieSearch_link % imdb
				hdlr = year
			
			files = []
			for base in self.base_link:
				url = f"{base}/{self._params}{path}"
				log(f"COMET trying url = {url}", LOGDEBUG)
				try:
					response = client.request(url, timeout=timeout)
					if response:
						files = json.loads(response).get('streams', [])
						if files: break
				except Exception as e:
					log(f"COMET error fetching from {base}: {str(e)}", LOGWARNING)
					continue
			
			if not files:
				log("COMET: No results found from any instance", LOGINFO)
				return sources

			_INFO = re.compile(r'💾.*')
			undesirables = source_utils.get_undesirables()
			check_foreign_audio = source_utils.check_foreign_audio()
		except Exception as e:
			log(f"COMET error in setup: {e}", LOGERROR)
			source_utils.scraper_error('COMET')
			return sources

		local_sources = []
		local_totals = {}

		for file in files:
			try:
				hash = file.get('infoHash')
				if not hash: continue

				file_title = file.get('description', '').replace('┈➤', '\n').split('\n')
				if not file_title or not file_title[0]: continue

				try:
					file_info = [x for x in file_title if _INFO.search(x)][0]
				except IndexError:
					file_info = ""

				name = source_utils.clean_name(file_title[0])

				if not source_utils.check_title(title, aliases, name.replace('.(Archie.Bunker', ''), hdlr, year): continue
				name_info = source_utils.info_from_name(name, title, year, hdlr, episode_title)
				if source_utils.remove_lang(name_info, check_foreign_audio): continue
				if undesirables and source_utils.remove_undesirables(name_info, undesirables): continue

				url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name) 

				try:
					seeders = 0 # int(re.search(r'(\d+)', file_info).group(1))
					if self.min_seeders > seeders: continue
				except: seeders = 0

				quality, info = source_utils.get_release_quality(name_info, url)
				try:
					size_match = re.search(r'((?:\d+\,\d+\.\d+|\d+\.\d+|\d+\,\d+|\d+)\s*(?:GB|GiB|Gb|MB|MiB|Mb))', file_info)
					if size_match:
						size = size_match.group(0)
						dsize, isize = source_utils._size(size)
						info.insert(0, isize)
					else:
						dsize = 0
				except: dsize = 0
				info = ' | '.join(info)

				item = {'provider': 'comet', 'source': 'torrent', 'seeders': seeders, 'hash': hash, 'name': name, 'name_info': name_info, 'quality': quality,
							'language': 'en', 'url': url, 'info': info, 'direct': False, 'debridonly': True, 'size': dsize}
				
				local_sources.append(item)
				local_totals[quality] = local_totals.get(quality, 0) + 1
			except Exception as e:
				log(f"COMET error processing result: {e}", LOGERROR)
		
		self.add_sources_thread_safe(local_sources, local_totals)
		return self.source_results

	def sources_packs(self, data, hostDict, search_series=False, total_seasons=None, bypass_filter=False):
		self.reset_results()
		sources = []
		if not data: return sources
		
		try:
			title = data['tvshowtitle'].replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ')
			aliases = data['aliases']
			imdb = data.get('imdb') or data.get('imdb_id')
			if not imdb:
				log("COMET: No IMDB ID provided for pack, skipping", LOGINFO)
				return sources

			year = data['year']
			season = data['season']
			
			# NOTE: Comet seems to respond with all streams for a query.
			# Re-fetching for sources_packs is safer than queueing.
			path = self.tvSearch_link % (imdb, season, data['episode'])
			
			files = []
			for base in self.base_link:
				url = f"{base}/{self._params}{path}"
				log(f"COMET pack trying url = {url}", LOGDEBUG)
				try:
					response = client.request(url, timeout=timeout)
					if response:
						files = json.loads(response).get('streams', [])
						if files: break
				except Exception as e:
					log(f"COMET pack error fetching from {base}: {str(e)}", LOGWARNING)
					continue

			if not files:
				return sources

			_INFO = re.compile(r'💾.*')
			undesirables = source_utils.get_undesirables()
			check_foreign_audio = source_utils.check_foreign_audio()
		except Exception as e:
			log(f"COMET error in pack setup: {e}", LOGERROR)
			source_utils.scraper_error('COMET')
			return sources

		local_sources = []
		local_totals = {}

		for file in files:
			try:
				hash = file.get('infoHash')
				if not hash: continue

				file_title = file.get('description', '').replace('┈➤', '\n').split('\n')
				if not file_title or not file_title[0]: continue

				try:
					file_info = [x for x in file_title if _INFO.search(x)][0]
				except IndexError:
					file_info = ""

				name = source_utils.clean_name(file_title[0])

				episode_start, episode_end = 0, 0
				if not search_series:
					if not bypass_filter:
						valid, episode_start, episode_end = source_utils.filter_season_pack(title, aliases, year, season, name.replace('.(Archie.Bunker', ''))
						if not valid: continue
					package = 'season'

				elif search_series:
					if not bypass_filter:
						valid, last_season = source_utils.filter_show_pack(title, aliases, imdb, year, season, name.replace('.(Archie.Bunker', ''), total_seasons)
						if not valid: continue
					else: last_season = total_seasons
					package = 'show'

				name_info = source_utils.info_from_name(name, title, year, season=season, pack=package)
				if source_utils.remove_lang(name_info, check_foreign_audio): continue
				if undesirables and source_utils.remove_undesirables(name_info, undesirables): continue

				url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)
				try:
					seeders = 0 # int(re.search(r'(\d+)', file_info).group(1))
					if self.min_seeders > seeders: continue
				except: seeders = 0

				quality, info = source_utils.get_release_quality(name_info, url)
				try:
					size_match = re.search(r'((?:\d+\,\d+\.\d+|\d+\.\d+|\d+\,\d+|\d+)\s*(?:GB|GiB|Gb|MB|MiB|Mb))', file_info)
					if size_match:
						size = size_match.group(0)
						dsize, isize = source_utils._size(size)
						info.insert(0, isize)
					else:
						dsize = 0
				except: dsize = 0
				info = ' | '.join(info)

				item = {'provider': 'comet', 'source': 'torrent', 'seeders': seeders, 'hash': hash, 'name': name, 'name_info': name_info, 'quality': quality,
							'language': 'en', 'url': url, 'info': info, 'direct': False, 'debridonly': True, 'size': dsize, 'package': package}
				if search_series: item.update({'last_season': last_season})
				elif episode_start: item.update({'episode_start': episode_start, 'episode_end': episode_end}) # for partial season packs
				
				local_sources.append(item)
				local_totals[quality] = local_totals.get(quality, 0) + 1
			except Exception as e:
				log(f"COMET error processing pack result: {e}", LOGERROR)
		
		self.add_sources_thread_safe(local_sources, local_totals)
		return self.source_results


class CometService(ConcurrentScraperBase):
    """
    Wrapper class to make the comet scraper compatible with the Orac server's ScraperManager.
    It inherits from ConcurrentScraperBase to reuse the threading and result handling logic.
    """
    def __init__(self):
        # Initialize the base class with the scraper's name
        super().__init__('comet')
        # Create an instance of the original scraper logic
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
