# -*- coding: utf-8 -*-
import re
import json
import base64
import requests
from urllib.parse import quote, unquote
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG
from resources.lib.debrid_resolver import DEFAULT_PRIORITIES, QUALITY_RANKS, get_provider_priority, seas_ep_filter, EXTRAS

VIDEO_EXTENSIONS = (
    'm4v', '3g2', '3gp', 'nsv', 'tp', 'ts', 'ty', 'pls', 'rm', 'rmvb', 'mpd', 'ifo', 'mov', 'qt',
    'divx', 'xvid', 'bivx', 'vob', 'nrg', 'img', 'iso', 'udf', 'pva', 'wmv', 'asf', 'asx', 'ogm',
    'm2v', 'avi', 'bin', 'dat', 'mpg', 'mpeg', 'mp4', 'mkv', 'mk3d', 'avc', 'vp3', 'svq3', 'nuv',
    'viv', 'dv', 'fli', 'flv', 'wpl', 'xspf', 'vdr', 'dvr-ms', 'xsp', 'mts', 'm2t', 'm2ts', 'evo',
    'ogv', 'sdp', 'avs', 'rec', 'url', 'pxml', 'vc1', 'h264', 'rcv', 'rss', 'mpls', 'mpl', 'webm',
    'bdmv', 'bdm', 'wtv', 'trp', 'f4v', 'pvr', 'disc'
)
VIDEO_EXT_STR = ', '.join(VIDEO_EXTENSIONS)

def clean_file_name(title):
    """Normalizes title for clean search and display."""
    if not title:
        return ""
    t = unquote(title)
    # Replace common entities and punctuation
    t = t.replace('&amp;', '&').replace('&#x27;', "'").replace('&', 'and')
    t = re.sub(r'[\.:/?\\*|<>"!;,()_]+', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def estimate_quality(width, filename):
    """Estimates quality from video width and filename tags."""
    f_lower = filename.lower()
    if any(tag in f_lower for tag in ('.4k', '4k', '2160', '2160p', 'uhd')):
        return '4K'
    if any(tag in f_lower for tag in ('1080', '1080p', 'fhd')):
        return '1080p'
    if any(tag in f_lower for tag in ('720', '720p', 'hd.')):
        return '720p'
    
    if width > 1920:
        return '4K'
    elif 1280 < width <= 1920:
        return '1080p'
    elif 720 < width <= 1280:
        return '720p'
    return 'SD'

def extract_extra_info(filename):
    """Extracts codec, audio, and visual details from release name."""
    f_upper = filename.upper()
    info = []
    if '2160P' in f_upper or '4K' in f_upper: info.append('4K')
    elif '1080P' in f_upper: info.append('1080p')
    elif '720P' in f_upper: info.append('720p')

    if 'HDR' in f_upper: info.append('HDR')
    if 'DV' in f_upper or 'DOVI' in f_upper or 'DOLBY VISION' in f_upper: info.append('Dovi')
    if 'REMUX' in f_upper: info.append('REMUX')
    if 'HEVC' in f_upper or 'H.265' in f_upper or 'X265' in f_upper: info.append('HEVC')
    elif 'H.264' in f_upper or 'X264' in f_upper or 'AVC' in f_upper: info.append('H.264')

    if 'ATMOS' in f_upper: info.append('ATMOS')
    if 'TRUEHD' in f_upper: info.append('TRUEHD')
    if 'DTS-HD' in f_upper or 'DTSHD' in f_upper: info.append('DTS-HD')
    elif 'DTS' in f_upper: info.append('DTS')
    if 'DDP' in f_upper or 'EAC3' in f_upper: info.append('DDP')
    elif 'AC3' in f_upper or 'DD5.1' in f_upper: info.append('DD5.1')

    return ' | '.join(info)

def search_easynews(scrape_data, cfg, timeout=7):
    """
    Queries Easynews Solr API and formats streams to match Orac's unified cached streams format.
    """
    username = cfg.get('easynews_user')
    password = cfg.get('easynews_password')
    if not username or not password or username == 'empty_setting' or password == 'empty_setting':
        return []

    is_enabled = cfg.get('provider.easynews', 'false').lower() in ('true', '1')
    if not is_enabled:
        return []

    is_episode = (scrape_data.get('item_type') == 'episode' or bool(scrape_data.get('season') and scrape_data.get('episode')))
    title = scrape_data.get('tvshowtitle') if is_episode else (scrape_data.get('title') or '')
    if not title:
        title = scrape_data.get('name') or ''

    year = scrape_data.get('year') or ''
    season = scrape_data.get('season')
    episode = scrape_data.get('episode')

    search_title = clean_file_name(title)
    if is_episode and season is not None and episode is not None:
        try:
            s_int, e_int = int(season), int(episode)
            query = f"{search_title} S{s_int:02d}E{e_int:02d}"
        except (ValueError, TypeError):
            query = f"{search_title}"
    else:
        query = f"{search_title} {year}".strip()

    log(f"[EasyNews] Querying Solr search for: '{query}'", level=LOGINFO)

    user_info = f"{username}:{password}".encode('utf-8')
    auth_header = 'Basic ' + base64.b64encode(user_info).decode('utf-8')

    params = {
        'st': 'adv',
        'sb': 1,
        'fex': VIDEO_EXT_STR,
        'fty[]': 'VIDEO',
        'spamf': 1,
        'u': 1,
        'gx': 1,
        'pno': 1,
        'sS': 3,
        's1': 'relevance',
        's1d': '-',
        'pby': 1000,
        'safeO': 0,
        'gps': query
    }

    url = 'https://members.easynews.com/2.0/search/solr-search/advanced'
    headers = {
        'Authorization': auth_header,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            log(f"[EasyNews] HTTP {resp.status_code} from search API", level=LOGWARNING)
            return []
        data = resp.json()
    except Exception as e:
        log(f"[EasyNews] Search exception: {e}", level=LOGERROR)
        return []

    dl_farm = data.get('dlFarm')
    dl_port = data.get('dlPort')
    files = data.get('data', [])
    if not files or not dl_farm or not dl_port:
        log(f"[EasyNews] No results returned for '{query}'", level=LOGINFO)
        return []

    streaming_base = f"https://{quote(username)}:{quote(password)}@members.easynews.com/dl"
    provider_rank = get_provider_priority('EasyNews', cfg)

    results = []

    for item in files:
        try:
            if item.get('virus'):
                continue
            if item.get('type') and item['type'].upper() != 'VIDEO':
                continue

            duration = str(item.get('14', ''))
            if re.match(r'^\d+s', duration) or re.match(r'^[0-5]m', duration):
                # Short video / sample / trailer
                continue

            post_hash = item.get('0')
            raw_size = int(item.get('rawSize') or 0)
            post_title = item.get('10') or ''
            ext = item.get('11') or ''
            width = int(item.get('width') or 0)

            if not post_hash or not post_title or raw_size <= 0:
                continue

            post_title_lower = post_title.lower()
            if any(ex in post_title_lower for ex in EXTRAS):
                continue

            # Check episode filter if episode search
            if is_episode and season is not None and episode is not None:
                if not seas_ep_filter(season, episode, post_title):
                    continue

            # Estimate quality & size
            quality = estimate_quality(width, post_title)
            quality_rank = QUALITY_RANKS.get(quality, 1)
            size_gb = round(float(raw_size) / (1024 ** 3), 2)

            # Construct stream URL
            url_add = quote(f"/{dl_farm}/{dl_port}/{post_hash}{ext}/{post_title}{ext}")
            stream_url = streaming_base + url_add

            display_name = clean_file_name(post_title).replace('html', ' ').replace('+', ' ').replace('-', ' ')
            extra_info = extract_extra_info(post_title)

            results.append({
                'name': post_title,
                'display_name': display_name,
                'quality': quality,
                'quality_rank': quality_rank,
                'size': size_gb,
                'size_label': f"{size_gb:.2f} GB",
                'url': stream_url,
                'url_dl': stream_url,
                'hash': post_hash,
                'source': 'easynews',
                'scrape_provider': 'external',
                'debrid': 'EasyNews',
                'cache_provider': 'EasyNews',
                'provider_rank': provider_rank,
                'direct': True,
                'seeders': 100,
                'extraInfo': extra_info
            })
        except Exception as err:
            log(f"[EasyNews] Error processing item: {err}", level=LOGDEBUG)

    log(f"[EasyNews] Found {len(results)} valid video streams for '{query}' (Rank {provider_rank})", level=LOGINFO)
    return results

def resolve_easynews(stream_url, cfg=None, timeout=10):
    """
    Resolves an Easynews streaming URL.
    Easynews URLs constructed from search results (https://user:pass@members.easynews.com/dl/...)
    are already direct, authenticated streaming links that Kodi plays natively.
    Returning the direct stream URL avoids slow/hanging HTTP redirects and timeout delays.
    """
    if not stream_url:
        return None
    return stream_url
