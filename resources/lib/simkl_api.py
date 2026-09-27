"""
simkl_api.py
------------
Helper module to build standardized URL parameters and HTTP headers
for Simkl API calls, conforming to https://api.simkl.org/conventions/headers.
Includes temporary debug logging for outgoing calls and responses.
"""

import os
import time
import requests
from resources.lib.version import __version__
from resources.lib.log_utils import log, LOGINFO, LOGERROR, LOGWARNING

SIMKL_APP_NAME = "orac-server"

# Default AUTH V2 Client ID (registered under TV, devices & command line)
DEFAULT_SIMKL_CLIENT_ID = "57ab588efc99ea43687f8635dc7b883a8d45d624dc141970fddf503efa6f88c7"

# Known legacy V1 Client IDs that are not enabled for OAuth 2.0
LEGACY_SIMKL_CLIENT_IDS = {
    "8cdf2298c78dd4ff8cb8039faecd1b9f11cf108fac2b88092abd15c22cfe2cc2",
    "4c920ba05273be800e843c0a2a4c148e1a17adbbba14c441bc3861214088a296",
}

# Set to True to enable Simkl call logging, or set ORAC_DEBUG_SIMKL=TRUE in environment
DEBUG_SIMKL_CALLS = os.environ.get("ORAC_DEBUG_SIMKL", "FALSE").upper() == "TRUE"


def get_effective_simkl_client_id(config_db_path: str = None) -> str:
    """
    Returns the configured Simkl client_id from config.db, or environment variable,
    or falls back to DEFAULT_SIMKL_CLIENT_ID.
    Automatically filters out and migrates legacy V1 client IDs that do not support OAuth 2.0.
    """
    if config_db_path:
        from resources.lib.config_handler import get_config_value, update_config_values
        cid = get_config_value("simkl.client", config_db_path) or get_config_value("simkl_client", config_db_path)
        if cid and cid not in ("empty_setting", ""):
            if cid in LEGACY_SIMKL_CLIENT_IDS:
                log(f"[Simkl] Detected legacy V1 client ID '{cid[:8]}...', migrating config to V2 client ID", level=LOGINFO)
                update_config_values({
                    "simkl.client": DEFAULT_SIMKL_CLIENT_ID,
                    "simkl_client": DEFAULT_SIMKL_CLIENT_ID
                }, config_db_path)
                return DEFAULT_SIMKL_CLIENT_ID
            return cid
    return os.environ.get("SIMKL_CLIENT_ID") or DEFAULT_SIMKL_CLIENT_ID


def get_simkl_params(client_id: str = None, extra_params: dict = None) -> dict:
    """
    Build required URL query parameters for Simkl API requests:
    client_id, app-name, app-version.
    """
    params = {
        "app-name": SIMKL_APP_NAME,
        "app-version": __version__,
    }
    if client_id:
        params["client_id"] = client_id
    if extra_params:
        params.update(extra_params)
    return params


def get_simkl_headers(token: str = None, client_id: str = None, extra_headers: dict = None) -> dict:
    """
    Build required HTTP headers for Simkl API requests:
    User-Agent, Content-Type, Authorization, simkl-api-key.
    """
    headers = {
        "User-Agent": f"{SIMKL_APP_NAME}/{__version__}",
        "Content-Type": "application/json",
    }
    if client_id:
        headers["simkl-api-key"] = client_id
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    return headers


def simkl_request(method: str, url: str, **kwargs):
    """
    Execute an HTTP request to the Simkl API.
    When DEBUG_SIMKL_CALLS is True, logs request details (URL, params, headers, body)
    and response details (status, response snippet) to Orac Server log.
    """
    if DEBUG_SIMKL_CALLS:
        params = kwargs.get("params")
        headers = kwargs.get("headers")
        json_body = kwargs.get("json")
        body_info = f" | Body: {json_body}" if json_body is not None else ""
        log(f"[Simkl API] >>> {method.upper()} {url} | Params: {params} | Headers: {headers}{body_info}", level=LOGINFO)

    resp = requests.request(method, url, **kwargs)

    if DEBUG_SIMKL_CALLS:
        snippet = resp.text[:400] + ("..." if len(resp.text) > 400 else "")
        log(f"[Simkl API] <<< {resp.status_code} {url} | Response ({len(resp.content)} bytes): {snippet}", level=LOGINFO)

    return resp


def simkl_get(url: str, **kwargs):
    """Convenience wrapper for GET requests via simkl_request."""
    return simkl_request("GET", url, **kwargs)


def simkl_post(url: str, **kwargs):
    """Convenience wrapper for POST requests via simkl_request."""
    return simkl_request("POST", url, **kwargs)


def refresh_simkl_token(config_db_path: str) -> str | None:
    """
    Refresh a Simkl AUTH V2 token using grant_type=refresh_token.
    Updates simkl.token, simkl.refresh_token, and simkl.expires_at in config.db.
    Returns the new access_token on success, or None on failure.
    """
    if not config_db_path:
        return None
    from resources.lib.config_handler import get_config_value, update_config_values
    refresh_token = get_config_value("simkl.refresh_token", config_db_path) or get_config_value("simkl_refresh_token", config_db_path)
    if not refresh_token or refresh_token == "empty_setting":
        return None

    client_id = get_effective_simkl_client_id(config_db_path)
    url = "https://api.simkl.com/oauth2/token"
    headers = {
        "User-Agent": f"{SIMKL_APP_NAME}/{__version__}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token
    }

    try:
        resp = simkl_post(url, headers=headers, data=data, timeout=15)
        if resp.status_code == 200:
            token_data = resp.json()
            new_access_token = token_data.get("access_token")
            new_refresh_token = token_data.get("refresh_token") or refresh_token
            expires_in = token_data.get("expires_in", 604800)  # default 7 days
            new_expires_at = time.time() + expires_in

            update_config_values({
                "simkl.token": new_access_token,
                "simkl_token": new_access_token,
                "simkl.refresh_token": new_refresh_token,
                "simkl_refresh_token": new_refresh_token,
                "simkl.expires_at": str(new_expires_at),
                "simkl.client": client_id
            }, config_db_path)
            log(f"[Simkl API] Successfully refreshed V2 access token (expires in {expires_in}s)", level=LOGINFO)
            return new_access_token
        else:
            log(f"[Simkl API] Token refresh failed (HTTP {resp.status_code}): {resp.text}", level=LOGERROR)
            return None
    except Exception as e:
        log(f"[Simkl API] Error refreshing Simkl token: {e}", level=LOGERROR)
        return None


def ensure_valid_simkl_token(config_db_path: str) -> str | None:
    """
    Checks the stored Simkl token in config.db.
    If it's an AUTH V2 token (starts with 'simkl_at_'), checks expiration and
    proactively refreshes if it expires within 24 hours (or is expired).
    If it's a legacy V1 token (64 hex characters), returns it as-is.
    """
    if not config_db_path:
        return None
    from resources.lib.config_handler import get_config_value
    token = get_config_value("simkl.token", config_db_path) or get_config_value("simkl_token", config_db_path)
    if not token or token == "empty_setting":
        return None

    # Only V2 tokens start with simkl_at_
    if token.startswith("simkl_at_"):
        expires_at_str = get_config_value("simkl.expires_at", config_db_path, "")
        try:
            expires_at = float(expires_at_str) if expires_at_str else 0
        except ValueError:
            expires_at = 0

        # If token expires within 24 hours (86400s) or is expired, refresh now
        if expires_at and time.time() >= (expires_at - 86400):
            log("[Simkl API] V2 access token is near expiry or expired — refreshing...", level=LOGINFO)
            new_token = refresh_simkl_token(config_db_path)
            if new_token:
                return new_token

    return token

