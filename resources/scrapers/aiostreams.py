# created by kodifitzwell for Fenomscrapers
"""
	Fenomscrapers Project
"""

import requests
import re
from resources.scrapers import source_utils
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase
try:
    from resources.lib.config_loader import OracConfig
    _config = OracConfig()
    _AIOSTREAMS_USER_DATA = _config.config.get('AIOSTREAMS', {}).get('user_data_header', None)
except Exception:
    _AIOSTREAMS_USER_DATA = None

# Fallback hardcoded header – used when config.json has no AIOSTREAMS.user_data_header.
# Run generate_aiostreams_header.py (in the project root) to regenerate and write
# a new value to config.json instead of editing this string by hand.
_AIOSTREAMS_USER_DATA_FALLBACK = (
    'ewogICJwcmVzZXRzIjogWwogICAgewogICAgICAidHlwZSI6ICJ0b3JyZW50aW8iLAogICAgICAiaW5zd'
    'GFuY2VJZCI6ICJ0aW8xIiwKICAgICAgImVuYWJsZWQiOiB0cnVlLAogICAgICAib3B0aW9ucyI6IHsKIC'
    'AgICAgICAibmFtZSI6ICJUb3JyZW50aW8iLAogICAgICAgICJ0aW1lb3V0IjogNjUwMCwKICAgICAgICAi'
    'cmVzb3VyY2VzIjogWyJzdHJlYW0iXSwKICAgICAgICAic29ydCI6ICJxdWFsaXR5IgogICAgICB9CiAgIC'
    'B9LAogICAgewogICAgICAidHlwZSI6ICJjb21ldCIsCiAgICAgICJpbnN0YW5jZUlkIjogImNtdDEiLAog'
    'ICAgICAiZW5hYmxlZCI6IHRydWUsCiAgICAgICJvcHRpb25zIjogewogICAgICAgICJuYW1lIjogIkNvbW'
    'V0IiwKICAgICAgICAidGltZW91dCI6IDY1MDAsCiAgICAgICAgInJlc291cmNlcyI6IFsic3RyZWFtIl0s'
    'CiAgICAgICAgImluY2x1ZGVQMlAiOiB0cnVlLAogICAgICAgICJyZW1vdmVUcmFzaCI6IGZhbHNlCiAgIC'
    'AgIH0KICAgIH0sCiAgICB7CiAgICAgICJ0eXBlIjogIm1lZGlhZnVzaW9uIiwKICAgICAgImluc3RhbmNl'
    'SWQiOiAibWYxIiwKICAgICAgImVuYWJsZWQiOiB0cnVlLAogICAgICAib3B0aW9ucyI6IHsKICAgICAgIC'
    'AibmFtZSI6ICJNZWRpYUZ1c2lvbiIsCiAgICAgICAgInRpbWVvdXQiOiA2NTAwLAogICAgICAgICJyZXNv'
    'dXJjZXMiOiBbInN0cmVhbSJdLAogICAgICAgICJ1c2VDYWNoZWRSZXN1bHRzT25seSI6IGZhbHNlLAogIC'
    'AgICAgICJlbmFibGVXYXRjaGxpc3RDYXRhbG9ncyI6IGZhbHNlLAogICAgICAgICJkb3dubG9hZFZpYUJy'
    'b3dzZXIiOiBmYWxzZSwKICAgICAgICAiY29udHJpYnV0b3JTdHJlYW1zIjogZmFsc2UsCiAgICAgICAgIm'
    'NlcnRpZmljYXRpb25MZXZlbHNGaWx0ZXIiOiBbXSwKICAgICAgICAibnVkaXR5RmlsdGVyIjogW10KICAg'
    'ICAgfQogICAgfQogIF0sAogICJmb3JtYXR0ZXIiOiB7CiAgICAiaWQiOiAidG9ycmVudGlvIiwKICAgICJk'
    'ZWZpbml0aW9uIjogeyJuYW1lIjogIiIsICJkZXNjcmlwdGlvbiI6ICIifQogIH0sCiAgInNvcnRDcml0ZX'
    'JpYSI6IHsiZ2xvYmFsIjogW119LAogICJkZWR1cGxpY2F0b3IiOiB7CiAgICAiZW5hYmxlZCI6IGZhbHNl'
    'LAogICAgImtleXMiOiBbImZpbGVuYW1lIiwgImluZm9IYXNoIl0sCiAgICAibXVsdGlHcm91cEJlaGF2aW91'
    'ciI6ICJhZ2dyZXNzaXZlIiwKICAgICJjYWNoZWQiOiAic2luZ2xlX3Jlc3VsdCIsCiAgICAidW5jYWNoZWQi'
    'OiAicGVyX3NlcnZpY2UiLAogICAgInAycCI6ICJzaW5nbGVfcmVzdWx0IiwKICAgICJleGNsdWRlQWRkb25z'
    'IjogW10KICB9Cn0='
)


class source:
	timeout = 30
	priority = 1
	pack_capable = False # packs parsed in sources function
	hasMovies = True
	hasEpisodes = True
	def __init__(self):
		self.language = ['en']
		self.movieSearch_link = '/api/v1/search'
		self.tvSearch_link = '/api/v1/search'
		self.min_seeders = 0
		
		# Load configuration from config.db
		from resources.lib.config_loader import ConfigLoader
		from resources.lib.config_handler import get_config_value
		from resources.lib.scraper_db import ScraperDB
		
		config_loader = ConfigLoader()
		config_db_path = config_loader.db_paths['config']
		
		self.username = get_config_value("aio.username", config_db_path, "empty_setting")
		self.password = get_config_value("aio.password", config_db_path, "empty_setting")
		instance_val = get_config_value("aiostreams_instance", config_db_path, "0")
		self.instance_id = int(instance_val) if instance_val and instance_val.isdigit() else 0
		self.custom_url = get_config_value("aio.custom_url", config_db_path, "empty_setting")
		
		# Validate credentials
		self.is_active = (
			self.username not in (None, "", "empty_setting") and
			self.password not in (None, "", "empty_setting") and
			(self.instance_id != 1 or self.custom_url not in (None, "", "empty_setting"))
		)
		
		# Self-deactivation safeguard
		if not self.is_active:
			try:
				scraper_db = ScraperDB('scrapers.db')
				scraper_db.set_active_status('aiostreams', False)
			except: pass
			self.auth = None
			self.base_link = None
		else:
			self.auth = (self.username, self.password)
			public_instance = (
				'https://aiostreams.stremio.ru',
				'https://',
				'https://aiostreams.viren070.me',
				'https://aiostreams.fortheweak.cloud',
				'https://aiostreamsfortheweebsstable.midnightignite.me'
			)
			if self.instance_id == 1:
				self.base_link = self.custom_url.strip().rstrip('/')
			else:
				self.base_link = public_instance[self.instance_id].strip().rstrip('/')

	def sources(self, data, hostDict):
		sources = []
		if not self.is_active: return sources
		if not data: return sources
		sources_append = sources.append
		try:
			title = data['tvshowtitle'] if 'tvshowtitle' in data else data['title']
			title = title.replace('&', 'and').replace('Special Victims Unit', 'SVU').replace('/', ' ')
			aliases = data['aliases']
			episode_title = data['title'] if 'tvshowtitle' in data else None
			total_seasons = data['total_seasons'] if 'tvshowtitle' in data else None
			year = data['year']
			imdb = data['imdb']
			if 'tvshowtitle' in data:
				season = data['season']
				episode = data['episode']
				hdlr = 'S%02dE%02d' % (int(season), int(episode))
				url = '%s%s' % (self.base_link, self.tvSearch_link)
				params = {'type': 'series', 'id': '%s:%s:%s' % (imdb, season, episode)}
			else:
				hdlr = year
				url = '%s%s' % (self.base_link, self.movieSearch_link)
				params = {'type': 'movie', 'id': '%s' % imdb}
			
			if 'timeout' in data: self.timeout = int(data['timeout'])
			results = requests.get(url, params=params, headers=self._headers(), timeout=self.timeout)
			response_json = results.json()
			response_data = response_json.get('data')
			if not response_data:
				# AIOStreams returns {"data": null} when no configured presets
				# return results for this title (e.g. content not in debrid cache).
				# This is not an error — just no results available.
				import logging
				logging.getLogger('orac').debug(
					'AIOSTREAMS - empty data for %s (status=%s, keys=%s)',
					params, results.status_code, list(response_json.keys())
				)
				return sources
			files = response_data.get('results', [])
			if files is None:
				return sources
			undesirables = source_utils.get_undesirables()
			check_foreign_audio = source_utils.check_foreign_audio()
		except Exception as e:
			from resources.lib.log_utils import log, LOGERROR
			log(f"aiostreams scraper error: {e}", level=LOGERROR)
			source_utils.scraper_error('AIOSTREAMS')
			return sources


		for file in files:
			try:
				if 'p2p' in file.get('type', ''): continue
				
				# Flatten parsedFile fields as per client logic
				parsed = {**file.pop('parsedFile', {})}
				parsed.update(file)
				file = parsed
				
				package, episode_start = None, 0
				hash = file.get('infoHash')
				if not hash: continue
				
				file_title = (file.get('folderName') or file.get('filename') or '').replace('┈➤', '\n').split('\n')
				if not file_title or not file_title[0]: continue

				name = source_utils.clean_name(file_title[0])

				if not source_utils.check_title(title, aliases, name, hdlr, year):
					if total_seasons is None: continue
					valid, last_season = source_utils.filter_show_pack(title, aliases, imdb, year, season, name, total_seasons)
					if not valid:
						valid, episode_start, episode_end = source_utils.filter_season_pack(title, aliases, year, season, name)
						if not valid: continue
						else: package = 'season'
					else: package = 'show'
				name_info = source_utils.info_from_name(name, title, year, hdlr, episode_title)
				if source_utils.remove_lang(name_info, check_foreign_audio): continue
				if undesirables and source_utils.remove_undesirables(name_info, undesirables): continue

				url = 'magnet:?xt=urn:btih:%s&dn=%s' % (hash, name)

				try:
					seeders = file.get('seeders', 0)
					if self.min_seeders > seeders: continue
				except: seeders = 0

				quality, info = source_utils.get_release_quality(name_info, url)
				try:
					size_val = file.get('size')
					if size_val:
						size = f"{float(size_val) / 1073741824:.2f} GB"
						dsize, isize = source_utils._size(size)
						info.insert(0, isize)
					else:
						dsize = 0
				except: dsize = 0
				info = ' | '.join(info)

				item = {
					'source': 'torrent', 'language': 'en', 'direct': False, 'debridonly': True,
					'provider': 'aiostreams', 'hash': hash, 'url': url, 'name': name, 'name_info': name_info,
					'quality': quality, 'info': info, 'size': dsize, 'seeders': seeders
				}
				if package: item['package'] = package
				if package == 'show': item.update({'last_season': last_season})
				if episode_start: item.update({'episode_start': episode_start, 'episode_end': episode_end}) # for partial season packs
				sources_append(item)
			except:
				source_utils.scraper_error('AIOSTREAMS')
		return sources

	def _headers(self):
		value = _AIOSTREAMS_USER_DATA or _AIOSTREAMS_USER_DATA_FALLBACK
		return {'x-aiostreams-user-data': value}

class AIOStreamsService(ConcurrentScraperBase):
    """
    Wrapper class for AIOStreams scraper.
    """
    def __init__(self):
        super().__init__('aiostreams')
        self.scraper = source()

    def scrape_sources(self, data):
        return self.scraper.sources(data, hostDict={})

    async def scrape_sources_async(self, data):
        return self.scrape_sources(data)
