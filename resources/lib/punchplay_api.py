# -*- coding: utf-8 -*-
"""
punchplay_api.py
----------------
Core API helpers for PunchPlay OAuth device code flow and authenticated requests.

Auth endpoints:
  Start device auth: POST https://punchplay.tv/api/platform/v1/auth/device/code
  Poll for token:    POST https://punchplay.tv/api/platform/v1/auth/device/token
  Refresh token:     POST https://punchplay.tv/api/platform/v1/auth/refresh
  Revoke token:      POST https://punchplay.tv/api/platform/v1/oauth/revoke
  Get user:          GET  https://punchplay.tv/api/platform/v1/me

Credentials stored in config.db:
  punchplay.token         — access token
  punchplay.refresh_token — refresh token
  punchplay.user          — username
  punchplay.expires_at    — unix timestamp when access token expires
  punchplay.client        — client_id used (for forward compatibility)
"""

import time
import requests
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING, LOGDEBUG

PUNCHPLAY_BASE_URL = "https://punchplay.tv"
PUNCHPLAY_CLIENT_ID = "ppc_6cf76fff68b27c08ad70502a"
PUNCHPLAY_APP_NAME = "OracServer"
PUNCHPLAY_DEVICE_CODE_URL = f"{PUNCHPLAY_BASE_URL}/api/platform/v1/auth/device/code"
PUNCHPLAY_DEVICE_TOKEN_URL = f"{PUNCHPLAY_BASE_URL}/api/platform/v1/auth/device/token"
PUNCHPLAY_REFRESH_URL = f"{PUNCHPLAY_BASE_URL}/api/platform/v1/auth/refresh"
PUNCHPLAY_REVOKE_URL = f"{PUNCHPLAY_BASE_URL}/api/platform/v1/oauth/revoke"
PUNCHPLAY_ME_URL = f"{PUNCHPLAY_BASE_URL}/api/platform/v1/me"
PUNCHPLAY_LISTS_URL = f"{PUNCHPLAY_BASE_URL}/api/platform/v1/me/lists"
PUNCHPLAY_TRENDING_URL = f"{PUNCHPLAY_BASE_URL}/api/public/v1/catalog/trending"
PUNCHPLAY_HISTORY_URL = f"{PUNCHPLAY_BASE_URL}/api/platform/v1/me/history"
PUNCHPLAY_SYNC_HISTORY_URL = f"{PUNCHPLAY_BASE_URL}/api/platform/v1/sync/history"

# Required scopes for Orac's use of PunchPlay
PUNCHPLAY_SCOPES = "profile:read lists:read history:read history:write"


def get_punchplay_headers(token=None):
    """Returns headers for PunchPlay API requests."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_punchplay_rate_limit(response):
    """
    Parses rate limit information from PunchPlay response headers:
    - X-RateLimit-Limit: total allowed in current window (e.g. 120)
    - X-RateLimit-Remaining: calls remaining in current window
    - X-RateLimit-Reset: Unix timestamp when window resets
    - Retry-After: seconds to wait before retrying (sent on HTTP 429)
    """
    if response is None:
        return {"limit": None, "remaining": None, "reset_at": None, "reset_in": 0.0, "retry_after": None}

    headers = getattr(response, "headers", {}) or {}

    limit = None
    try:
        val = headers.get("x-ratelimit-limit") or headers.get("X-RateLimit-Limit")
        if val is not None:
            limit = int(val)
    except (ValueError, TypeError):
        pass

    remaining = None
    try:
        val = headers.get("x-ratelimit-remaining") or headers.get("X-RateLimit-Remaining")
        if val is not None:
            remaining = int(val)
    except (ValueError, TypeError):
        pass

    reset_at = None
    reset_in = 0.0
    try:
        val = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
        if val is not None:
            reset_at = float(val)
            reset_in = max(0.0, reset_at - time.time())
    except (ValueError, TypeError):
        pass

    retry_after = None
    try:
        val = headers.get("retry-after") or headers.get("Retry-After")
        if val is not None:
            retry_after = float(val)
    except (ValueError, TypeError):
        pass

    return {
        "limit": limit,
        "remaining": remaining,
        "reset_at": reset_at,
        "reset_in": reset_in,
        "retry_after": retry_after
    }


def handle_punchplay_rate_limit(response, context="PunchPlay"):
    """
    Inspects response for rate limits.
    - If HTTP 429: sleeps for retry_after or reset_in (defaulting to 5s) and returns True.
    - If remaining requests < 3: sleeps until reset_in to proactively avoid 429.
    Returns True if a rate limit sleep occurred, False otherwise.
    """
    if response is None:
        return False

    rl = parse_punchplay_rate_limit(response)

    if response.status_code == 429:
        wait = rl["retry_after"] if rl["retry_after"] is not None else (rl["reset_in"] + 0.5 if rl["reset_in"] > 0 else 5.0)
        log(f"[{context}] Rate limited (HTTP 429) — waiting {wait:.1f}s before retry...", level=LOGWARNING)
        time.sleep(wait)
        return True

    if rl["remaining"] is not None and rl["remaining"] < 3 and rl["reset_in"] > 0:
        wait = rl["reset_in"] + 0.5
        log(f"[{context}] Rate limit threshold reached (remaining={rl['remaining']}) — pausing {wait:.1f}s until reset...", level=LOGINFO)
        time.sleep(wait)
        return True

    return False


def punchplay_get(url, token=None, params=None, timeout=15, max_retries=2):
    """Performs an authenticated GET request to a PunchPlay API endpoint with rate-limit handling."""
    headers = get_punchplay_headers(token=token)
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code == 429 and attempt < max_retries - 1:
                handle_punchplay_rate_limit(resp, context="PunchPlay GET")
                continue
            return resp
        except Exception as e:
            if attempt >= max_retries - 1:
                log(f"[PunchPlay] GET {url} failed: {e}", level=LOGERROR)
                raise
            time.sleep(1.0)


def punchplay_post(url, json_body=None, token=None, timeout=15, max_retries=2):
    """Performs a POST request to a PunchPlay API endpoint with rate-limit handling."""
    headers = get_punchplay_headers(token=token)
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=json_body or {}, timeout=timeout)
            if resp.status_code == 429 and attempt < max_retries - 1:
                handle_punchplay_rate_limit(resp, context="PunchPlay POST")
                continue
            return resp
        except Exception as e:
            if attempt >= max_retries - 1:
                log(f"[PunchPlay] POST {url} failed: {e}", level=LOGERROR)
                raise
            time.sleep(1.0)


def get_punchplay_token(config_db_path):
    """Reads the stored PunchPlay access token from config.db."""
    from resources.lib.config_handler import get_config_value
    return get_config_value("punchplay.token", config_db_path)


def get_punchplay_refresh_token(config_db_path):
    """Reads the stored PunchPlay refresh token from config.db."""
    from resources.lib.config_handler import get_config_value
    return get_config_value("punchplay.refresh_token", config_db_path)


def refresh_punchplay_token(config_db_path):
    """
    Attempts to refresh the PunchPlay access token using the stored refresh token.
    On success, updates config.db and returns the new access token.
    Returns None on failure.
    """
    from resources.lib.config_handler import get_config_value, update_config_values

    refresh_token = get_config_value("punchplay.refresh_token", config_db_path)
    if not refresh_token:
        log("[PunchPlay] No refresh token available. Cannot refresh.", level=LOGWARNING)
        return None

    try:
        resp = punchplay_post(PUNCHPLAY_REFRESH_URL, json_body={
            "client_id": PUNCHPLAY_CLIENT_ID,
            "refresh_token": refresh_token
        }, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            new_access_token = data.get("access_token")
            new_refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 3600)
            expires_at = time.time() + expires_in

            update_config_values({
                "punchplay.token": new_access_token,
                "punchplay.refresh_token": new_refresh_token or refresh_token,
                "punchplay.expires_at": str(expires_at),
            }, config_db_path)

            log("[PunchPlay] Successfully refreshed access token.", level=LOGINFO)
            return new_access_token
        else:
            log(f"[PunchPlay] Failed to refresh token ({resp.status_code}): {resp.text.strip()}", level=LOGERROR)
            return None
    except Exception as e:
        log(f"[PunchPlay] Exception refreshing token: {e}", level=LOGERROR)
        return None


def ensure_valid_punchplay_token(config_db_path):
    """
    Returns a valid PunchPlay access token, refreshing if necessary.
    Returns None if not authenticated or refresh fails.
    """
    from resources.lib.config_handler import get_config_value

    token = get_config_value("punchplay.token", config_db_path)
    if not token:
        return None

    # Check expiry — refresh 5 minutes early
    expires_at_str = get_config_value("punchplay.expires_at", config_db_path)
    if expires_at_str:
        try:
            expires_at = float(expires_at_str)
            if time.time() >= expires_at - 300:
                log("[PunchPlay] Access token near expiry, refreshing...", level=LOGDEBUG)
                refreshed = refresh_punchplay_token(config_db_path)
                return refreshed or token
        except (ValueError, TypeError):
            pass

    return token
