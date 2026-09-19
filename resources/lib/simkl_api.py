"""
simkl_api.py
------------
Helper module to build standardized URL parameters and HTTP headers
for Simkl API calls, conforming to https://api.simkl.org/conventions/headers.
Includes temporary debug logging for outgoing calls and responses.
"""

import os
import requests
from resources.lib.version import __version__
from resources.lib.log_utils import log, LOGINFO

SIMKL_APP_NAME = "orac-server"

# Set to True to enable Simkl call logging, or set ORAC_DEBUG_SIMKL=TRUE in environment
DEBUG_SIMKL_CALLS = os.environ.get("ORAC_DEBUG_SIMKL", "FALSE").upper() == "TRUE"


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
