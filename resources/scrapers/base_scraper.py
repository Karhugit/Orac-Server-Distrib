import threading
from abc import ABC, abstractmethod
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING

class AsyncScraperBase(ABC):
    def __init__(self, scraper_name: str):
        self.scraper_name = scraper_name
        self.source_results = []
        self.item_totals = {'4K': 0, '1080p': 0, '720p': 0, 'SD': 0, 'CAM': 0}
        self._results_lock = threading.Lock()
    
    @abstractmethod
    async def scrape_sources_async(self, data):
        pass
    
    def reset_results(self):
        with self._results_lock:
            self.source_results = []
            self.item_totals = {'4K': 0, '1080p': 0, '720p': 0, 'SD': 0, 'CAM': 0}