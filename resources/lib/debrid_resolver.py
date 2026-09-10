import re
import json
import time
import requests
from urllib.parse import unquote, urlencode
from concurrent.futures import ThreadPoolExecutor
from resources.lib.log_utils import log, LOGINFO, LOGERROR, LOGWARNING, LOGDEBUG

VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v', '.ts', '.m2ts', '.mpg', '.mpeg')
EXTRAS = ('sample', 'extra', 'deleted', 'trailer', 'featurette', 'short', 'preview')

DEFAULT_PRIORITIES = {
    'TorBox': 1,
    'Real-Debrid': 2,
    'Premiumize.me': 3,
    'AllDebrid': 4,
    'Offcloud': 5,
    'EasyDebrid': 6,
    'EasyNews': 7
}

QUALITY_RANKS = {
    '4K': 4,
    '2160p': 4,
    '1080p': 3,
    '720p': 2,
    'SD': 1
}

def get_quality_rank(q):
    if not q:
        return 1
    q_str = str(q).lower()
    if any(k in q_str for k in ('4k', '2160', 'uhd')):
        return 4
    if any(k in q_str for k in ('1080', 'fhd')):
        return 3
    if any(k in q_str for k in ('720', 'hd')):
        return 2
    return 1

def seas_ep_filter(season, episode, release_title):
    """Filters release_title to check if it matches the specified season and episode."""
    if season is None or episode is None:
        return True
    try:
        s_int, e_int = int(season), int(episode)
    except (ValueError, TypeError):
        return True

    str_season, str_episode = str(s_int), str(e_int)
    season_fill, episode_fill = str_season.zfill(2), str_episode.zfill(2)
    str_ep_plus_1, str_ep_minus_1 = str(e_int + 1), str(e_int - 1)
    title_clean = re.sub(r'[^A-Za-z0-9-]+', '.', unquote(release_title).replace("'", "")).lower()

    patterns = [
        rf'(s{season_fill}[.-]?e[p]?{episode_fill}[.-])',
        rf'(s{str_season}[.-]?e[p]?{episode_fill}[.-])',
        rf'(s{season_fill}[.-]?e[p]?{str_episode}[.-])',
        rf'(s{str_season}[.-]?e[p]?{str_episode}[.-])',
        rf'(season[.-]?{season_fill}[.-]?episode[.-]?{episode_fill}[.-])|([s]?{season_fill}[x.]{episode_fill}[.-])',
        rf'(season[.-]?{str_season}[.-]?episode[.-]?{episode_fill}[.-])|([s]?{str_season}[x.]{episode_fill}[.-])',
        rf'(season[.-]?{season_fill}[.-]?episode[.-]?{str_episode}[.-])|([s]?{season_fill}[x.]{str_episode}[.-])',
        rf'(season[.-]?{str_season}[.-]?episode[.-]?{str_episode}[.-])|([s]?{str_season}[x.]{str_episode}[.-])',
        rf'(s{season_fill}e{str_ep_minus_1.zfill(2)}[.-]?e?{episode_fill}[.-])',
        rf'(s{season_fill}e{episode_fill}[.-]?e?{str_ep_plus_1.zfill(2)}[.-])',
        rf'([.-]{season_fill}[.-]?{episode_fill}[.-])',
        rf'([.-]{str_season}[.-]?{episode_fill}[.-])',
        rf'(episode[.-]?{str_episode}[.-])',
        rf'([.-]e[p]?{episode_fill}[.-])',
        rf'(^(?=.*\.e?0*{episode_fill}\.)(?:(?!((?:s|season)[.-]?\d+[.-x]?(?:ep?|episode)[.-]?\d+)|\d+x\d+).)*$)'
    ]
    regex = re.compile('|'.join(patterns), re.IGNORECASE)
    return bool(regex.search(title_clean))


# =====================================================================
# CACHE CHECKERS
# =====================================================================

def check_torbox_cache(hash_list, token, timeout=4):
    if not token or token == 'empty_setting' or not hash_list:
        return set()
    try:
        url = 'https://api.torbox.app/v1/api/torrents/checkcached'
        res = requests.post(
            url,
            params={'format': 'list'},
            json={'hashes': [h.lower() for h in hash_list]},
            headers={'Authorization': f'Bearer {token}'},
            timeout=timeout
        )
        if res.status_code == 200:
            data = res.json().get('data', [])
            return set(item['hash'].lower() for item in data if 'hash' in item)
    except Exception as e:
        log(f"[DebridResolver] TorBox cache check error: {e}", level=LOGDEBUG)
    return set()

def check_premiumize_cache(hash_list, token, timeout=4):
    if not token or token == 'empty_setting' or not hash_list:
        return set()
    try:
        url = 'https://www.premiumize.me/api/cache/check'
        lowered = [h.lower() for h in hash_list]
        res = requests.post(
            url,
            data={'items[]': lowered},
            headers={'Authorization': f'Bearer {token}'},
            timeout=timeout
        )
        if res.status_code == 200:
            bools = res.json().get('response', [])
            cached = set()
            for h, is_cached in zip(lowered, bools):
                if is_cached is True:
                    cached.add(h)
            return cached
    except Exception as e:
        log(f"[DebridResolver] Premiumize cache check error: {e}", level=LOGDEBUG)
    return set()

def check_realdebrid_cache(hash_list, token, timeout=4):
    if not token or token == 'empty_setting' or not hash_list:
        return set()
    try:
        hash_str = '/'.join(h.lower() for h in hash_list[:100])
        url = f'https://api.real-debrid.com/rest/1.0/torrents/instantAvailability/{hash_str}'
        res = requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=timeout)
        if res.status_code == 200:
            data = res.json()
            cached = set()
            for h, val in data.items():
                if isinstance(val, dict) and len(val.get('rd', [])) > 0:
                    cached.add(h.lower())
            return cached
    except Exception as e:
        log(f"[DebridResolver] Real-Debrid cache check error: {e}", level=LOGDEBUG)
    return set()

def check_offcloud_cache(hash_list, token, timeout=4):
    if not token or token == 'empty_setting' or not hash_list:
        return set()
    try:
        url = f'https://offcloud.com/api/torrent/check?key={token}'
        lowered = [h.lower() for h in hash_list]
        res = requests.post(url, json={'hashes': lowered}, timeout=timeout)
        if res.status_code == 200:
            cached_items = res.json().get('cachedItems', [])
            return set(h.lower() for h in cached_items)
    except Exception as e:
        log(f"[DebridResolver] Offcloud cache check error: {e}", level=LOGDEBUG)
    return set()

def check_easydebrid_cache(hash_list, token, timeout=4):
    if not token or token == 'empty_setting' or not hash_list:
        return set()
    try:
        url = 'https://easydebrid.com/api/v1/link/lookup'
        lowered = [h.lower() for h in hash_list]
        res = requests.post(url, json={'urls': lowered}, headers={'Authorization': f'Bearer {token}'}, timeout=timeout)
        if res.status_code == 200:
            cached_flags = res.json().get('cached', [])
            cached = set()
            for h, is_c in zip(lowered, cached_flags):
                if is_c:
                    cached.add(h)
            return cached
    except Exception as e:
        log(f"[DebridResolver] EasyDebrid cache check error: {e}", level=LOGDEBUG)
    return set()

def check_alldebrid_cache(hash_list, token, timeout=4):
    if not token or token == 'empty_setting' or not hash_list:
        return set()
    try:
        url = f'https://api.alldebrid.com/v4/magnet/instant?agent=Orac&apikey={token}'
        lowered = [h.lower() for h in hash_list]
        res = requests.post(url, data={'magnets[]': lowered}, timeout=timeout)
        if res.status_code == 200:
            magnets = res.json().get('data', {}).get('magnets', [])
            return set(m['hash'].lower() for m in magnets if m.get('instant') is True)
    except Exception as e:
        log(f"[DebridResolver] AllDebrid cache check error: {e}", level=LOGDEBUG)
    return set()


def check_all_debrids_parallel(hashes, cfg, timeout=4):
    """
    Checks cache status across all enabled Debrid services concurrently.
    Returns { hash: [provider1, provider2, ...] }
    """
    if not hashes:
        return {}

    unique_hashes = list(set(h.lower() for h in hashes if h))
    runners = []

    tb_tok = cfg.get('tb.token')
    if tb_tok and tb_tok != 'empty_setting' and cfg.get('tb.enabled', 'true').lower() in ('true', '1'):
        runners.append(('TorBox', lambda: check_torbox_cache(unique_hashes, tb_tok, timeout)))

    pm_tok = cfg.get('pm.token')
    if pm_tok and pm_tok != 'empty_setting' and cfg.get('pm.enabled', 'true').lower() in ('true', '1'):
        runners.append(('Premiumize.me', lambda: check_premiumize_cache(unique_hashes, pm_tok, timeout)))

    rd_tok = cfg.get('rd.token')
    if rd_tok and rd_tok != 'empty_setting' and cfg.get('rd.enabled', 'true').lower() in ('true', '1'):
        runners.append(('Real-Debrid', lambda: check_realdebrid_cache(unique_hashes, rd_tok, timeout)))

    oc_tok = cfg.get('oc.token')
    if oc_tok and oc_tok != 'empty_setting' and cfg.get('oc.enabled', 'true').lower() in ('true', '1'):
        runners.append(('Offcloud', lambda: check_offcloud_cache(unique_hashes, oc_tok, timeout)))

    ed_tok = cfg.get('ed.token')
    if ed_tok and ed_tok != 'empty_setting' and cfg.get('ed.enabled', 'true').lower() in ('true', '1'):
        runners.append(('EasyDebrid', lambda: check_easydebrid_cache(unique_hashes, ed_tok, timeout)))

    ad_tok = cfg.get('ad.token')
    if ad_tok and ad_tok != 'empty_setting' and cfg.get('ad.enabled', 'true').lower() in ('true', '1'):
        runners.append(('AllDebrid', lambda: check_alldebrid_cache(unique_hashes, ad_tok, timeout)))

    if not runners:
        return {}

    cache_map = {h: [] for h in unique_hashes}
    t0 = time.perf_counter()

    def run_check(r):
        name, fn = r
        try:
            return name, fn()
        except Exception as err:
            log(f"[DebridResolver] Check error for {name}: {err}", level=LOGWARNING)
            return name, set()

    with ThreadPoolExecutor(max_workers=len(runners)) as ex:
        results = list(ex.map(run_check, runners))

    for name, cached_set in results:
        for h in cached_set:
            if h in cache_map:
                cache_map[h].append(name)

    elapsed = round((time.perf_counter() - t0) * 1000)
    cached_count = sum(1 for v in cache_map.values() if v)
    log(f"[DebridResolver] Debrid cache check completed in {elapsed}ms: {cached_count}/{len(unique_hashes)} hashes cached.", level=LOGINFO)
    return cache_map


# =====================================================================
# STREAM RESOLVERS
# =====================================================================

def resolve_torbox(magnet_url, info_hash, token, title="", season=None, episode=None, timeout=15):
    """Unrestricts a magnet on TorBox and returns direct streaming URL."""
    try:
        headers = {'Authorization': f'Bearer {token}'}
        add_res = requests.post('https://api.torbox.app/v1/api/torrents/createtorrent', json={'magnet': magnet_url}, headers=headers, timeout=timeout)
        data = add_res.json()
        if not data.get('success'):
            return None
        torrent_id = data.get('data', {}).get('torrent_id')
        if not torrent_id:
            return None

        info_res = requests.get(f'https://api.torbox.app/v1/api/torrents/mylist?id={torrent_id}', headers=headers, timeout=timeout)
        info_data = info_res.json().get('data', {})
        files = info_data.get('files', []) if isinstance(info_data, dict) else (info_data[0].get('files', []) if info_data else [])

        candidates = [f for f in files if f.get('short_name', '').lower().endswith(VIDEO_EXTENSIONS)]
        if not candidates:
            return None

        if season is not None and episode is not None:
            candidates = [f for f in candidates if seas_ep_filter(season, episode, f.get('short_name', ''))]
        else:
            candidates.sort(key=lambda x: x.get('size', 0), reverse=True)

        if not candidates:
            return None

        file_id = candidates[0]['id']
        down_res = requests.get(
            f'https://api.torbox.app/v1/api/torrents/requestdl?token={token}&torrent_id={torrent_id}&file_id={file_id}',
            timeout=timeout
        )
        return down_res.json().get('data')
    except Exception as e:
        log(f"[DebridResolver] TorBox resolve error: {e}", level=LOGERROR)
        return None

def resolve_premiumize(magnet_url, info_hash, token, title="", season=None, episode=None, timeout=15):
    """Unrestricts a magnet on Premiumize and returns direct streaming URL."""
    try:
        headers = {'Authorization': f'Bearer {token}'}
        res = requests.post('https://www.premiumize.me/api/transfer/directdl', data={'src': magnet_url}, headers=headers, timeout=timeout)
        data = res.json()
        if data.get('status') != 'success':
            return None

        contents = data.get('content', [])
        valid_files = [f for f in contents if f.get('path', '').lower().endswith(VIDEO_EXTENSIONS) and f.get('link')]
        if not valid_files:
            return None

        if season is not None and episode is not None:
            valid_files = [f for f in valid_files if seas_ep_filter(season, episode, f.get('path', ''))]
        else:
            valid_files.sort(key=lambda x: int(x.get('size', 0)), reverse=True)

        if not valid_files:
            return None

        return valid_files[0]['link']
    except Exception as e:
        log(f"[DebridResolver] Premiumize resolve error: {e}", level=LOGERROR)
        return None

def resolve_realdebrid(magnet_url, info_hash, token, title="", season=None, episode=None, timeout=15):
    """Unrestricts a magnet on Real-Debrid and returns direct streaming URL."""
    try:
        headers = {'Authorization': f'Bearer {token}'}
        add_res = requests.post('https://api.real-debrid.com/rest/1.0/torrents/addMagnet', data={'magnet': magnet_url}, headers=headers, timeout=timeout)
        data = add_res.json()
        torrent_id = data.get('id')
        if not torrent_id:
            return None

        requests.post(f'https://api.real-debrid.com/rest/1.0/torrents/selectFiles/{torrent_id}', data={'files': 'all'}, headers=headers, timeout=timeout)

        info_res = requests.get(f'https://api.real-debrid.com/rest/1.0/torrents/info/{torrent_id}', headers=headers, timeout=timeout)
        info = info_res.json()
        links = info.get('links', [])
        if not links:
            return None

        unres = requests.post('https://api.real-debrid.com/rest/1.0/unrestrict/link', data={'link': links[0]}, headers=headers, timeout=timeout)
        return unres.json().get('download')
    except Exception as e:
        log(f"[DebridResolver] Real-Debrid resolve error: {e}", level=LOGERROR)
        return None

def resolve_stream(provider, magnet_url, info_hash, title="", season=None, episode=None, cfg=None):
    """
    Main dispatcher to resolve a magnet URL to a direct CDN stream link.
    """
    if not cfg:
        cfg = {}

    p_lower = (provider or '').lower()
    if 'torbox' in p_lower:
        return resolve_torbox(magnet_url, info_hash, cfg.get('tb.token'), title, season, episode)
    elif 'premiumize' in p_lower or 'pm' in p_lower:
        return resolve_premiumize(magnet_url, info_hash, cfg.get('pm.token'), title, season, episode)
    elif 'easynews' in p_lower or 'en' == p_lower:
        from resources.lib.easynews_client import resolve_easynews
        return resolve_easynews(magnet_url, cfg=cfg)
    else:
        log(f"[DebridResolver] Unsupported provider for direct resolve: {provider}", level=LOGWARNING)
        return None


# =====================================================================
# PRIORITY RANKING & PRE-RESOLVING
# =====================================================================

def get_provider_priority(provider_name, cfg):
    """Retrieves numeric priority rank for provider (lower = higher priority)."""
    p_key = {
        'TorBox': 'tb.priority',
        'Real-Debrid': 'rd.priority',
        'Premiumize.me': 'pm.priority',
        'AllDebrid': 'ad.priority',
        'Offcloud': 'oc.priority',
        'EasyDebrid': 'ed.priority',
        'EasyNews': 'en.priority'
    }.get(provider_name, f"{provider_name.lower()}.priority")

    default = DEFAULT_PRIORITIES.get(provider_name, 10)
    try:
        return int(cfg.get(p_key, str(default)))
    except (ValueError, TypeError):
        return default

def get_sort_key(item, sort_order=0):
    """
    Returns the sort tuple for an item based on sort_order:
    0: 'Quality, Provider, Size'
    1: 'Quality, Size, Provider'
    2: 'Provider, Quality, Size'
    3: 'Provider, Size, Quality'
    4: 'Size, Quality, Provider'
    5: 'Size, Provider, Quality'
    """
    q = -int(item.get('quality_rank', 1) or 1)
    p = int(item.get('provider_rank', 10) or 10)
    s = -float(item.get('size', 0) or 0)

    if sort_order == 1:
        return (q, s, p)
    elif sort_order == 2:
        return (p, q, s)
    elif sort_order == 3:
        return (p, s, q)
    elif sort_order == 4:
        return (s, q, p)
    elif sort_order == 5:
        return (s, p, q)
    else:
        return (q, p, s)

def rank_and_filter_cached_results(results, cache_map, cfg, search_info=None, extra_direct_results=None, pre_resolve_top=True):
    """
    Filters scraped results to only those cached on at least one Debrid provider,
    merges any extra direct stream results (e.g. Easynews),
    deduplicates multi-cached torrents to the highest priority provider,
    and sorts results according to the configured results.sort_order.
    Pre-resolves the #1 top-ranked stream and marks with is_resolved: True.
    """
    cached_results = []
    if results and cache_map:
        for item in results:
            h = item.get('hash', '').lower()
            cached_providers = cache_map.get(h, [])
            if not cached_providers:
                continue

            cached_providers.sort(key=lambda p: get_provider_priority(p, cfg))
            best_provider = cached_providers[0]

            enriched = dict(item)
            enriched['debrid'] = best_provider
            enriched['cache_provider'] = best_provider
            enriched['scrape_provider'] = 'external'
            enriched['provider_rank'] = get_provider_priority(best_provider, cfg)
            q_rank = get_quality_rank(item.get('quality'))
            enriched['quality_rank'] = q_rank
            if q_rank == 4: enriched['quality'] = '4K'
            elif q_rank == 3: enriched['quality'] = '1080p'
            elif q_rank == 2: enriched['quality'] = '720p'
            else: enriched['quality'] = item.get('quality') or 'SD'
            cached_results.append(enriched)

    if extra_direct_results:
        for extra in extra_direct_results:
            q_rank = get_quality_rank(extra.get('quality'))
            extra['quality_rank'] = q_rank
            if q_rank == 4: extra['quality'] = '4K'
            elif q_rank == 3: extra['quality'] = '1080p'
            elif q_rank == 2: extra['quality'] = '720p'
            else: extra['quality'] = extra.get('quality') or 'SD'
        cached_results.extend(extra_direct_results)

    if not cached_results:
        return []

    try:
        sort_order = int(cfg.get('results.sort_order', '0'))
    except (ValueError, TypeError):
        sort_order = 0

    log(f"[DebridResolver] Sorting {len(cached_results)} streams using results.sort_order={sort_order}", level=LOGINFO)
    cached_results.sort(key=lambda x: get_sort_key(x, sort_order))

    if pre_resolve_top:
        top_item = cached_results[0]
        log(f"[DebridResolver] Pre-resolving top stream: {top_item.get('name')} on {top_item.get('debrid')}", level=LOGINFO)
        try:
            title = search_info.get('title', '') if search_info else ''
            season = search_info.get('season') if search_info else None
            episode = search_info.get('episode') if search_info else None
            stream_url = resolve_stream(
                top_item.get('debrid'),
                top_item.get('url'),
                top_item.get('hash'),
                title=title,
                season=season,
                episode=episode,
                cfg=cfg
            )
            if stream_url:
                top_item['stream_url'] = stream_url
                top_item['is_resolved'] = True
                log(f"[DebridResolver] Top stream successfully pre-resolved: {stream_url[:60]}...", level=LOGINFO)
            else:
                top_item['is_resolved'] = False
        except Exception as e:
            log(f"[DebridResolver] Failed to pre-resolve top stream: {e}", level=LOGWARNING)
            top_item['is_resolved'] = False

    return cached_results
