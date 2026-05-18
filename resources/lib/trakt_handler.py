import requests
import xbmcgui
import xbmcaddon
import xbmc
import time
import json
import os
import threading
from urllib.parse import parse_qs
import xbmcvfs
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING
import asyncio
from .config_handler import get_trakt_access_token, get_trakt_refresh_token, update_config_values, clear_trakt_config, get_trakt_client_id, get_trakt_client_secret, get_config_value


class TraktAuth:
    refresh_lock = threading.Lock()
    last_refresh_check = 0
    
    def __init__(self, addon, config_db_path, client_id = None, client_secret = None):
        self.addon = addon
        self.client_id = get_trakt_client_id(config_db_path) or client_id
        self.client_secret = get_trakt_client_secret(config_db_path) or client_secret
        self.config_db_path = config_db_path
        self.base_url = "https://api.trakt.tv"
        self.username = None
        self.access_token = None


    async def fetch_username(self):
        """Fetches the Trakt username from the API and saves it to the config database."""
        try:
            resp = await self.get("/users/me")
            if resp and resp.status_code == 200:
                user_data = resp.json()
                username = user_data.get("ids", {}).get("slug")
                if username:
                    log(f"[Orac] Fetched Trakt username from API: {username}", level=LOGINFO)
                    update_config_values({'trakt_user': username}, self.config_db_path)
                    self.username = username
                    return username
            else:
                log(f"[Orac] Failed to fetch Trakt username from API: {resp.status_code if resp else 'No response'}", level=LOGWARNING)
        except Exception as e:
            log(f"[Orac] Error fetching Trakt username from API: {e}", level=LOGERROR)
        return None

    def get_username(self):
        """Fetches the Trakt username from the config database."""
        from .config_handler import get_trakt_user
        return get_trakt_user(config_db_path=self.config_db_path)



    def reload_credentials(self):
        """Reloads client_id and client_secret from config DB."""
        from .config_handler import get_trakt_client_id, get_trakt_client_secret
        self.client_id = get_trakt_client_id(self.config_db_path) or self.client_id
        self.client_secret = get_trakt_client_secret(self.config_db_path) or self.client_secret
        # Also clear cached user to force re-fetch if needed
        self.username = None 
        self.username = None 
        log(f"[TraktAuth] Reloaded credentials. Client ID: {self.client_id[:4]}...", LOGINFO)

    def _ensure_token_fresh(self):
        """Checks if token needs refresh and does so safely with locking."""
        # Check memory cache first to reduce DB reads
        if time.time() - TraktAuth.last_refresh_check < 60:
            return

        try:
            last_refreshed = int(get_config_value('trakt_token_refreshed', self.config_db_path, '0'))
        except (ValueError, TypeError):
            last_refreshed = 0

        # Trakt tokens last 3 months, 12 hours is a safety refresh interval. 
        # If we are slightly over 12 hours, the token is still valid.
        if time.time() - last_refreshed > 43200: # 12 hours
            # Try to acquire lock non-blocking. If locked, someone else is refreshing, so we can skip.
            if TraktAuth.refresh_lock.acquire(blocking=False):
                try:
                    # Double check inside lock
                    try:
                        last_refreshed = int(get_config_value('trakt_token_refreshed', self.config_db_path, '0'))
                    except: 
                        last_refreshed = 0
                        
                    if time.time() - last_refreshed > 43200:
                        log("[Orac] Trakt token older than 12 hours, refreshing.", level=LOGINFO)
                        result = self.refresh_token()
                        # Update cache regardless of result to allow backoff
                        TraktAuth.last_refresh_check = time.time()
                        if not result:
                            log("[Orac] Token refresh failed or skipped. Backing off for 60s.", level=LOGWARNING)
                finally:
                    TraktAuth.refresh_lock.release()
            else:
                log("[Orac] Trakt token refresh already in progress, skipping.", level=LOGINFO)
        else:
            TraktAuth.last_refresh_check = time.time()

    def get_show_seasons_with_episodes(self, show_trakt_id):
        """Fetches all seasons and their episodes for a show from Trakt, with extended data including all IDs."""
        # Ensure your _get method handles rate limiting, error checking, etc.
        url = f"/shows/{show_trakt_id}/seasons?extended=episodes"
        response = self._get(url) # Assuming _get returns the JSON directly or handles errors
        response.raise_for_status() # Raise an exception for bad status codes
        return response.json()

    def refresh_token(self):
        """Refresh expired access token using refresh_token"""
        refresh_token = get_trakt_refresh_token(self.config_db_path)
        refresh_token = get_trakt_refresh_token(self.config_db_path)
        if not refresh_token:
            log("[Orac] data missing: Cannot refresh token (no refresh_token found in DB).", level=LOGWARNING)
            return False

        trakt_headers = {  
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id
        }

        trakt_payload = {
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token"
        }

        trakt_token_url = "https://api.trakt.tv/oauth/token"

        try:
            resp = requests.post(
                trakt_token_url,
                json=trakt_payload,
                headers=trakt_headers,
                timeout=10
            )

            try:
                response = resp.json()
            except ValueError:
                log(f"[Orac] Failed to parse JSON from Trakt: {resp.status_code} {resp.text}", level=LOGERROR)
                return False

            if "error" in response:
                if response["error"] == "invalid_grant":
                    log("[Orac] Refresh token invalid, clearing trakt token data", level=LOGWARNING)
                    clear_trakt_config(self.config_db_path)
                    return False
                else:
                    log(f"[Orac] Error during token refresh: {response['error']}", level=LOGERROR)
                    clear_trakt_config(self.config_db_path)
                    return False

            if "access_token" in response:
                log("[Orac] Trakt token refresh successful", level=LOGINFO)
                update_config_values({
                    'trakt_token': response["access_token"],
                    'trakt_refresh': response["refresh_token"],
                    'trakt_token_refreshed': str(int(time.time()))
                }, self.config_db_path)
                return True

        except Exception as e:
            log(f"[Orac] Token refresh request failed: {str(e)}", level=LOGERROR)

        return False


    async def get(self, endpoint, extended=None, params=None, authenticated=False):
        self._ensure_token_fresh()

        access_token = get_trakt_access_token(self.config_db_path)
        if authenticated and not access_token:
            log("[Orac] Authentication required but no saved Trakt tokens", level=LOGERROR)
            return None

        trakt_headers = {  
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id
        }

        if access_token:
            trakt_headers["Authorization"] = f"Bearer {access_token}"

        url = f"https://api.trakt.tv{endpoint}"
        
        # Build parameters dict
        request_params = {}
        if extended:
            request_params["extended"] = extended
        
        # Add any additional parameters passed in
        if params:
            request_params.update(params)

        # Run the blocking requests.get inside a thread
        def _sync_get():
            return requests.get(url, headers=trakt_headers, params=request_params)

        response = await asyncio.to_thread(_sync_get)
        if response.status_code == 401 and access_token:
            # Token is invalid, try refreshing
            if not self.refresh_token():
                log("[Orac] Failed to refresh Trakt token", level=LOGERROR)
                return None
            else:
                # Update headers with new token
                access_token = get_trakt_access_token(self.config_db_path)
                trakt_headers["Authorization"] = f"Bearer {access_token}"

                # Retry the request
                def _sync_get_retry():
                    return requests.get(url, headers=trakt_headers, params=request_params)

                response = await asyncio.to_thread(_sync_get_retry)
                response.raise_for_status()
        return response

    def _get(self, endpoint, extended=None, x_headers=None, params=None, authenticated=False):
        self._ensure_token_fresh()

        access_token = get_trakt_access_token(self.config_db_path)
        if authenticated and not access_token:
            raise Exception("Authentication required but no saved Trakt tokens")

        trakt_headers = {  
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id
        }

        if access_token:
            trakt_headers["Authorization"] = f"Bearer {access_token}"

        if x_headers:
            trakt_headers.update(x_headers)

        url = f"https://api.trakt.tv{endpoint}"
        
        # Build parameters dict
        request_params = {}
        if extended:
            request_params["extended"] = extended
        
        # Add any additional parameters passed in
        if params:
            request_params.update(params)

        response = requests.get(url, headers=trakt_headers, params=request_params)
        if response.status_code == 401 and access_token:
            # Token is invalid, try refreshing
            if not self.refresh_token():
                log("[Orac] Failed to refresh Trakt token", level=LOGERROR)
                return None
            else:
                # Update headers with new token
                access_token = get_trakt_access_token(self.config_db_path)
                trakt_headers["Authorization"] = f"Bearer {access_token}"
                response = requests.get(url, headers=trakt_headers, params=request_params)
                response.raise_for_status()

        return response

    def post(self, endpoint, json=None, extended=None):
        self._ensure_token_fresh()

        access_token = get_trakt_access_token(self.config_db_path)
        if not access_token:
            raise Exception("No saved Trakt tokens")

        trakt_headers = {  
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
            "Authorization": f"Bearer {access_token}"
        }

        url = f"https://api.trakt.tv{endpoint}"
        params = {}
        if extended:
            params["extended"] = extended

        response = requests.post(url, headers=trakt_headers, json=json, params=params)
        return response

    def delete(self, endpoint, params=None):
        self._ensure_token_fresh()

        access_token = get_trakt_access_token(self.config_db_path)
        if not access_token:
            raise Exception("No saved Trakt tokens")

        trakt_headers = {  
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
            "Authorization": f"Bearer {access_token}"
        }

        url = f"https://api.trakt.tv{endpoint}"
        
        response = requests.delete(url, headers=trakt_headers, params=params)
        if response.status_code == 401:
            # Token is invalid, try refreshing
            if not self.refresh_token():
                log("[Orac] Failed to refresh Trakt token", level=LOGERROR)
                return None
            else:
                # Update headers with new token
                access_token = get_trakt_access_token(self.config_db_path)
                trakt_headers["Authorization"] = f"Bearer {access_token}"
                response = requests.delete(url, headers=trakt_headers, params=params)
        
        return response
