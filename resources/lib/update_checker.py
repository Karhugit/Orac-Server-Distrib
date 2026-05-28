"""
update_checker.py
-----------------
Background update-check logic for Orac Server.

Checks the public GitHub Releases API for the distrib repo and caches
the result in a module-level dict.  Safe to call from a daemon thread —
never raises, always logs.

Typical usage (from http_server.py):
    from .update_checker import check_for_update, get_update_state

    # At startup / daily:
    threading.Thread(target=check_for_update, daemon=True).start()

    # In an API endpoint:
    return JSONResponse(content=get_update_state())
"""

import threading
import requests

from .version import __version__
from .log_utils import log, LOGINFO, LOGWARNING

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_API_URL = (
    "https://api.github.com/repos/Karhugit/Orac-Server-Distrib/releases/latest"
)
_GITHUB_RELEASES_PAGE = (
    "https://github.com/Karhugit/Orac-Server-Distrib/releases"
)
_REQUEST_TIMEOUT = 10  # seconds

# ---------------------------------------------------------------------------
# Shared state (module-level, protected by a lock)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state: dict = {
    "current_version": __version__,
    "latest_version": None,
    "update_available": False,
    "release_url": _GITHUB_RELEASES_PAGE,
    "release_notes": None,
    "last_checked": None,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_version(version_str: str) -> tuple:
    """Parse 'v1.2.3' or '1.2.3' into a comparable tuple of ints."""
    clean = (version_str or "").lstrip("v").strip()
    try:
        return tuple(int(x) for x in clean.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_update_state() -> dict:
    """Return a snapshot of the current update state (thread-safe)."""
    with _lock:
        return dict(_state)


def check_for_update() -> None:
    """
    Fetch the latest GitHub release and update the cached state.
    Blocking — run this in a daemon thread, not on the async event loop.
    Never raises; errors are logged as warnings.
    """
    from datetime import datetime as _dt

    log("[Orac] Checking for Orac Server updates...", level=LOGINFO)
    try:
        resp = requests.get(
            _GITHUB_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"orac-server/{__version__}",
            },
            timeout=_REQUEST_TIMEOUT,
        )

        if resp.status_code == 200:
            data = resp.json()
            latest_tag = data.get("tag_name", "")
            release_url = data.get("html_url") or _GITHUB_RELEASES_PAGE
            release_notes = (data.get("body") or "").strip()

            local_tuple = _parse_version(__version__)
            latest_tuple = _parse_version(latest_tag)
            update_available = latest_tuple > local_tuple
            latest_clean = latest_tag.lstrip("v") or __version__

            with _lock:
                _state.update(
                    {
                        "latest_version": latest_clean,
                        "update_available": update_available,
                        "release_url": release_url,
                        "release_notes": release_notes,
                        "last_checked": _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                )

            if update_available:
                log(
                    f"[Orac] *** Update available: v{latest_clean} "
                    f"(running v{__version__}) — {release_url}",
                    level=LOGWARNING,
                )
            else:
                log(
                    f"[Orac] Orac Server v{__version__} is up to date.",
                    level=LOGINFO,
                )

        elif resp.status_code == 404:
            # No release published yet — not an error
            log(
                "[Orac] Update check: no GitHub release found yet.",
                level=LOGINFO,
            )
        else:
            log(
                f"[Orac] Update check failed: GitHub returned HTTP {resp.status_code}",
                level=LOGWARNING,
            )

    except Exception as exc:
        log(f"[Orac] Update check error: {exc}", level=LOGWARNING)
