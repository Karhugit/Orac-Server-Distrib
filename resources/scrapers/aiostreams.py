# created by kodifitzwell for Fenomscrapers
"""
	Fenomscrapers Project
"""

import requests
import re
from resources.scrapers import source_utils
from resources.scrapers.thread_manager_opt import ConcurrentScraperBase


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
			results = requests.get(url, params=params, auth=self.auth, timeout=self.timeout)
			if not results.ok: results.raise_for_status()
			
			items = results.json()['data']['results']
			undesirables = source_utils.get_undesirables()
			check_foreign_audio = source_utils.check_foreign_audio()
		except Exception as e:
			from resources.lib.log_utils import log, LOGERROR
			log(f"aiostreams scraper error: {e}", level=LOGERROR)
			source_utils.scraper_error('AIOSTREAMS')
			return sources

		for item in items:
			try:
				if 'p2p' in item.get('type', ''): continue
				
				# Flatten parsedFile fields as per client logic
				file = {**item.pop('parsedFile', {})}
				file.update(item)
				
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
