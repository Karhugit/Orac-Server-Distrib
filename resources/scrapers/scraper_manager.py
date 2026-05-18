import importlib
import os
from pathlib import Path
import asyncio
from concurrent.futures import ThreadPoolExecutor
from resources.scrapers.limetorrents import LimeTorrentsService
from resources.scrapers.mediafusion import MediaFusionService
from resources.scrapers.torrentio import TorrentioService
from resources.scrapers.piratebay import PirateBayService
from resources.scrapers.comet import CometService
from resources.scrapers.kickass import KickassService
from resources.scrapers.torrentdownload import TorrentDownloadService
from resources.scrapers.zilean import ZileanService
from resources.scrapers.prowlarr import ProwlarrService
from resources.scrapers.tbtorznab import TBTorZnabService
from resources.scrapers.dmm import DmmService
from resources.scrapers.bitmagnet import BitmagnetService
from resources.scrapers.aiostreams import AIOStreamsService
from resources.scrapers.uindex import UIndexService
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
from resources.lib.scraper_db import ScraperDB

class ScraperManager:
    """Manages scrapers within your existing HTTP service"""

    def __init__(self, scrapers_dir="scrapers"):
        self.scrapers = {}
        self.executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="scraper")
        self.db = ScraperDB()
        self._init_scrapers()
    
    def _init_scrapers(self):
        """Initialize available scrapers and register in DB"""
        self.scrapers['limetorrents'] = LimeTorrentsService()
        self.scrapers['mediafusion'] = MediaFusionService()
        self.scrapers['torrentio'] = TorrentioService()
        self.scrapers['piratebay'] = PirateBayService()
        self.scrapers['comet'] = CometService()
        self.scrapers['kickass'] = KickassService()
        self.scrapers['torrentdownload'] = TorrentDownloadService()
        self.scrapers['zilean'] = ZileanService()
        self.scrapers['prowlarr'] = ProwlarrService()
        self.scrapers['tbtorznab'] = TBTorZnabService()
        self.scrapers['dmm'] = DmmService()
        self.scrapers['bitmagnet'] = BitmagnetService()
        self.scrapers['aiostreams'] = AIOStreamsService()
        self.scrapers['uindex'] = UIndexService()
        
        # Register them in the database
        self.db.register_scrapers(list(self.scrapers.keys()))

    def get_partitioned_providers(self):
        """
        Partitions scrapers into:
        - primary: Top 2 ALWAYS ACTIVE scrapers by score.
        - background: Remaining active scrapers AND all inactive scrapers for exploration.
        Returns tuples of (primary_list, background_list) containing scraper metadata dicts.
        """
        all_data = self.db.get_all_scrapers()
        if not all_data:
            return [], []

        active_scrapers = [s for s in all_data if s['active'] == 1]
        inactive_scrapers = [s for s in all_data if s['active'] == 0]

        # If no active scrapers (shouldn't happen normally), treat all as active for recovery
        if not active_scrapers:
            log("[ScraperManager] No active scrapers found! Using all scrapers as primary for recovery.", level=LOGWARNING)
            return all_data, []

        # Check if all active scores are 0
        all_active_zero = all(s['score'] == 0 for s in active_scrapers)
        
        if all_active_zero:
            # Everyone active is primary
            primary = active_scrapers
            background = inactive_scrapers
        else:
            # Pick top 2 active for primary
            primary = active_scrapers[:2]
            # The rest of active + all inactive are background
            background = active_scrapers[2:] + inactive_scrapers
        
        log(f"[ScraperManager] Partitioned: primary={[s['name'] for s in primary]}, background={[s['name'] for s in background]}", level=LOGDEBUG)
        return primary, background

    def get_active_providers(self):
        """Legacy helper, now returns just the primary set names."""
        primary, _ = self.get_partitioned_providers()
        return [s['name'] for s in primary]
    
    async def scrape_async(self, provider, data, search_type="sources", **kwargs):
        """Main async scraping interface - offloads blocking code to thread pool"""
        if provider not in self.scrapers:
            raise ValueError(f"Unknown provider: {provider}")
        
        scraper_service = self.scrapers[provider]
        loop = asyncio.get_running_loop()
        
        if search_type == "sources":
            # Call the service's sync method via executor
            if hasattr(scraper_service, 'scrape_sources'):
                return await loop.run_in_executor(self.executor, scraper_service.scrape_sources, data)
            elif hasattr(scraper_service, 'scraper') and hasattr(scraper_service.scraper, 'sources'):
                # Fallback to the underlying scraper's sources method if standardized scraper attribute exists
                return await loop.run_in_executor(self.executor, scraper_service.scraper.sources, data, {})
            else:
                # Last resort: try any attribute that might be the scraper
                scraper_obj = next((getattr(scraper_service, attr) for attr in dir(scraper_service) 
                                  if not attr.startswith('__') and hasattr(getattr(scraper_service, attr), 'sources')), None)
                if scraper_obj:
                    return await loop.run_in_executor(self.executor, scraper_obj.sources, data, {})
                
                # If all else fails, log error and return empty
                log(f"ScraperManager: Provider '{provider}' has no valid synchronous 'sources' method", LOGERROR)
                return []
                
        elif search_type in ["packs", "series_packs"]:
            search_series = (search_type == "series_packs")
            total_seasons = kwargs.get('total_seasons')
            bypass_filter = kwargs.get('bypass_filter', False)
            
            if hasattr(scraper_service, 'scrape_packs'):
                return await loop.run_in_executor(self.executor, scraper_service.scrape_packs, data, search_series, total_seasons, bypass_filter)
            elif hasattr(scraper_service, 'scraper') and hasattr(scraper_service.scraper, 'sources_packs'):
                return await loop.run_in_executor(self.executor, scraper_service.scraper.sources_packs, data, {}, search_series, total_seasons, bypass_filter)
            else:
                scraper_obj = next((getattr(scraper_service, attr) for attr in dir(scraper_service) 
                                  if not attr.startswith('__') and hasattr(getattr(scraper_service, attr), 'sources_packs')), None)
                if scraper_obj:
                    return await loop.run_in_executor(self.executor, scraper_obj.sources_packs, data, {}, search_series, total_seasons, bypass_filter)
                
                log(f"ScraperManager: Provider '{provider}' has no valid synchronous 'sources_packs' method", LOGERROR)
                return []
        else:
            raise ValueError(f"Unknown search_type: {search_type}")
    
    async def scrape_all_async(self, data, search_type="sources", **kwargs):
        """Scrapes all registered providers concurrently."""
        tasks = []
        for provider in self.scrapers.keys():
            # Create a task for each scraper without awaiting it immediately
            task = asyncio.create_task(self.scrape_async(provider, data, search_type, **kwargs))
            tasks.append(task)
        
        # Wait for all tasks to complete
        provider_results_list = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten the list of lists into a single list of results
        all_sources = []
        for i, provider_results in enumerate(provider_results_list):
            provider_name = list(self.scrapers.keys())[i]
            if isinstance(provider_results, Exception):
                log(f"Scraper '{provider_name}' failed with an exception: {provider_results}", LOGERROR)
            elif provider_results:
                all_sources.extend(provider_results)
        
        # Deduplicate results based on the info hash
        unique_sources = []
        seen_hashes = set()
        for source in all_sources:
            # Ensure the source is a dictionary and has a 'hash' key
            if isinstance(source, dict) and 'hash' in source:
                if source['hash'] not in seen_hashes:
                    unique_sources.append(source)
                    seen_hashes.add(source['hash'])
            else:
                unique_sources.append(source)  # Keep items that can't be deduplicated

        # Define the quality sort order
        quality_order = {'4K': 0, '1080p': 1, '720p': 2, 'SD': 3, 'SCR': 4, 'CAM': 5}

        # Sort by quality (primary) and seeders (secondary, descending)
        sorted_sources = sorted(
            unique_sources,
            key=lambda s: (quality_order.get(s.get('quality'), 99), -s.get('seeders', 0)) if isinstance(s, dict) else (99, 0)
        )

        return sorted_sources


