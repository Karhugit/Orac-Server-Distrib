"""
Optimized Thread Manager for Kodi Scrapers
Designed for concurrent multi-scraper environments

Usage:
    # Drop-in replacement for existing code:
    from cocoscrapers.modules.thread_manager_opt import migrate_existing_scraper_call
    migrate_existing_scraper_call(self, links, self.get_sources_packs)
    
    # Or use the full optimized approach:
    from cocoscrapers.modules.thread_manager_opt import ConcurrentScraperBase
    class MyScaper(ConcurrentScraperBase):
        # Your scraper implementation
"""

import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
import weakref


class GlobalScraperResourceManager:
    """Global manager to coordinate resources across all scrapers"""
    
    def __init__(self):
        self._lock = threading.RLock()
        self._active_scrapers = weakref.WeakSet()
        self._total_active_requests = 0
        self._per_scraper_limits = {}
        
        # Global limits - adjust these based on your system
        self.MAX_TOTAL_THREADS = 25  # Total threads across ALL scrapers
        self.MAX_PER_SCRAPER_THREADS = 5  # Max threads per individual scraper
        self.MAX_CONCURRENT_REQUESTS = 20  # Max simultaneous network requests
        
    def register_scraper(self, scraper_name):
        """Register a new scraper and get its resource allocation"""
        with self._lock:
            if scraper_name not in self._per_scraper_limits:
                # Each scraper gets its own pool up to MAX_PER_SCRAPER_THREADS
                threads = self.MAX_PER_SCRAPER_THREADS
                self._per_scraper_limits[scraper_name] = threads
                log(f"Registered scraper {scraper_name} with {threads} threads", LOGDEBUG)
            
            return self._per_scraper_limits[scraper_name]
    
    @contextmanager
    def request_slot(self, scraper_name):
        """Context manager to control concurrent requests"""
        acquired = False
        try:
            with self._lock:
                if self._total_active_requests < self.MAX_CONCURRENT_REQUESTS:
                    self._total_active_requests += 1
                    acquired = True
            
            if not acquired:
                # Brief wait if at capacity
                time.sleep(0.05)
                with self._lock:
                    if self._total_active_requests < self.MAX_CONCURRENT_REQUESTS:
                        self._total_active_requests += 1
                        acquired = True
            
            yield acquired
            
        finally:
            if acquired:
                with self._lock:
                    self._total_active_requests = max(0, self._total_active_requests - 1)
    
    def get_stats(self):
        """Get current resource usage stats"""
        with self._lock:
            return {
                'active_scrapers': len(self._active_scrapers),
                'active_requests': self._total_active_requests,
                'scraper_limits': dict(self._per_scraper_limits)
            }


# Global instance
resource_manager = GlobalScraperResourceManager()


class OptimizedScraperThreadManager:
    """Optimized thread manager for individual scrapers"""
    
    def __init__(self, scraper_name, scraper_instance):
        self.scraper_name = scraper_name
        self.scraper = weakref.ref(scraper_instance) if scraper_instance else None
        self.max_workers = resource_manager.register_scraper(scraper_name)
        self._executor = None
        self._lock = threading.Lock()
        
        # Register with global manager
        if scraper_instance:
            resource_manager._active_scrapers.add(scraper_instance)
    
    @property
    def executor(self):
        """Lazy thread pool creation"""
        if self._executor is None:
            with self._lock:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=self.max_workers,
                        thread_name_prefix=f"{self.scraper_name}"
                    )
        return self._executor
    
    def scrape_urls_optimized(self, urls, scraping_function, timeout=25):
        """Optimized URL scraping with resource management"""
        if not urls:
            return
        
        start_time = time.time()
        completed = 0
        failed = 0
        results = []
        
        def safe_scrape_with_resource_management(url):
            nonlocal completed, failed
            
            with resource_manager.request_slot(self.scraper_name) as got_slot:
                if not got_slot:
                    # Skip if resource manager is at capacity
                    log(f"{self.scraper_name}: Resource limit reached, skipping {url}", LOGWARNING)
                    with self._lock:
                        failed += 1
                    return None
                
                try:
                    result = scraping_function(url)
                    with self._lock:
                        completed += 1
                    return result
                    
                except Exception as e:
                    log(f"{self.scraper_name}: Error scraping {url}: {str(e)}", LOGERROR)
                    with self._lock:
                        failed += 1
                    return None
        
        # Submit all tasks
        futures = []
        for url in urls:
            future = self.executor.submit(safe_scrape_with_resource_management, url)
            futures.append(future)
        
        # Process results as they complete
        try:
            for future in as_completed(futures, timeout=timeout):
                try:
                    result = future.result()
                    if result is not None:
                        results.append(result)
                except Exception as e:
                    log(f"{self.scraper_name}: Future result error: {str(e)}", LOGWARNING)
                    
        except Exception as e:
            log(f"{self.scraper_name}: Timeout or error in scraping batch: {str(e)}", LOGERROR)
            # Cancel remaining futures
            for future in futures:
                future.cancel()
        
        elapsed = time.time() - start_time
        log(f"{self.scraper_name}: Completed {completed}/{len(urls)} URLs in {elapsed:.2f}s (failed: {failed})", LOGDEBUG)
        
        return results
    
    def shutdown(self):
        """Clean shutdown of thread pool"""
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None


class ConcurrentScraperBase:
    """Base class for scrapers with optimized concurrent processing"""
    
    def __init__(self, scraper_name):
        self.scraper_name = scraper_name
        self.thread_manager = OptimizedScraperThreadManager(scraper_name, self)
        
        # Thread-safe data structures
        self._sources_lock = threading.Lock()
        self._totals_lock = threading.Lock()
        
        # Initialize if not already present
        if not hasattr(self, 'source_results'):
            self.source_results = []
        if not hasattr(self, 'item_totals'):
            self.item_totals = defaultdict(int)
    
    def add_sources_thread_safe(self, sources, totals):
        """Thread-safe method to add sources and update totals"""
        if sources:
            # Filter out SCR quality results as requested
            filtered_sources = [s for s in sources if s.get('quality') != 'SCR' and s.get('quality') != 'CAM' and s.get('quality') != 'TELE']
            if filtered_sources:
                with self._sources_lock:
                    self.source_results.extend(filtered_sources)

        if totals:
            with self._totals_lock:
                for quality, count in totals.items():
                    # Skip SCR quality in totals as well
                    if quality == 'SCR' or quality == 'CAM' or quality == 'TELE':
                        continue
                    # Ensure the quality exists in item_totals to prevent KeyError
                    # Some scrapers override self.item_totals with a standard dict in reset_results()
                    if quality not in self.item_totals:
                        self.item_totals[quality] = 0
                    self.item_totals[quality] += count
    
    def log_results_thread_safe(self, start_time, suffix=''):
        """Thread-safe result logging"""
        logged = False
        with self._totals_lock:
            for quality in self.item_totals:
                if self.item_totals[quality] > 0:
                    logged = True
                    log(f'#STATS - {self.scraper_name}{suffix} found {self.item_totals[quality]:2.0f} {quality}')

        if not logged:
            log(f'#STATS - {self.scraper_name}{suffix} found nothing')

        elapsed = time.time() - start_time
        log(f'#STATS - {self.scraper_name}{suffix} took {elapsed:.2f} seconds')

    def __del__(self):
        """Cleanup when scraper is destroyed"""
        if hasattr(self, 'thread_manager'):
            self.thread_manager.shutdown()


# Migration helpers for existing code
def migrate_existing_scraper_call(scraper_instance, links, scraping_function, timeout=25):
    """
    Drop-in replacement for existing thread_manager calls
    
    Usage:
        # OLD:
        from cocoscrapers.modules.Thread_pool import run_and_wait
        run_and_wait(self.get_sources_packs, links)
        
        # NEW:
        from cocoscrapers.modules.thread_manager_opt import migrate_existing_scraper_call
        migrate_existing_scraper_call(self, links, self.get_sources_packs)
    """
    if not links:
        return
    
    # Create temporary thread manager for the scraper
    scraper_name = scraper_instance.__class__.__name__
    thread_manager = OptimizedScraperThreadManager(scraper_name, scraper_instance)
    
    try:
        def wrapper(url):
            try:
                return scraping_function(url)
            except Exception as e:
                log(f"Error in {scraper_name}: {str(e)}", LOGERROR)
                return None
        
        results = thread_manager.scrape_urls_optimized(links, wrapper, timeout)
        return results
        
    finally:
        thread_manager.shutdown()


def run_and_wait_optimized(scraper_name, func, iterable, timeout=25):
    """
    Optimized version of the original run_and_wait function
    
    Usage:
        from cocoscrapers.modules.thread_manager_opt import run_and_wait_optimized
        run_and_wait_optimized("MYSCRAPER", self.get_sources_packs, links)
    """
    if not iterable:
        return
    
    thread_manager = OptimizedScraperThreadManager(scraper_name, None)
    
    try:
        def safe_wrapper(item):
            try:
                return func(item)
            except Exception as e:
                log(f"Error in {scraper_name}: {str(e)}", LOGERROR)
                return None
        
        results = thread_manager.scrape_urls_optimized(list(iterable), safe_wrapper, timeout)
        return results
        
    finally:
        thread_manager.shutdown()


def run_and_wait_multi_optimized(scraper_name, func, iterable, timeout=25):
    """
    Optimized version of the original run_and_wait_multi function
    
    Usage:
        from cocoscrapers.modules.thread_manager_opt import run_and_wait_multi_optimized
        results = run_and_wait_multi_optimized("MYSCRAPER", func, arg_tuples)
    """
    if not iterable:
        return []
    
    thread_manager = OptimizedScraperThreadManager(scraper_name, None)
    
    try:
        def multi_wrapper(args):
            try:
                if isinstance(args, (list, tuple)):
                    return func(*args)
                else:
                    return func(args)
            except Exception as e:
                log(f"Error in {scraper_name}: {str(e)}", LOGERROR)
                return None
        
        results = thread_manager.scrape_urls_optimized(list(iterable), multi_wrapper, timeout)
        return [r for r in results if r is not None]
        
    finally:
        thread_manager.shutdown()


# Backward compatibility functions (use existing thread pool but with limits)
def run_and_wait_compat(func, iterable):
    """Backward compatible version with resource management"""
    return run_and_wait_optimized("UNKNOWN_SCRAPER", func, iterable)


def run_and_wait_multi_compat(func, iterable):
    """Backward compatible version with resource management"""
    return run_and_wait_multi_optimized("UNKNOWN_SCRAPER", func, iterable)


def get_resource_stats():
    """Get current resource usage statistics"""
    return resource_manager.get_stats()


def configure_global_limits(max_total_threads=25, max_per_scraper=5, max_concurrent_requests=20):
    """Configure global resource limits"""
    resource_manager.MAX_TOTAL_THREADS = max_total_threads
    resource_manager.MAX_PER_SCRAPER_THREADS = max_per_scraper
    resource_manager.MAX_CONCURRENT_REQUESTS = max_concurrent_requests
    log(f"Updated global limits: {max_total_threads} total threads, {max_per_scraper} per scraper, {max_concurrent_requests} concurrent requests", LOGINFO)