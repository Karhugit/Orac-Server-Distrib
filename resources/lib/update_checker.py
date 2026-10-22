"""
update_checker.py
-----------------
Background update-check and self-update logic for Orac Server.

Checks the public Orac-Server-Distrib GitHub repository (both raw version.py /
CHANGELOG.md on `main` and the GitHub Releases API) and caches the result in a
module-level dict. Also provides `perform_self_update()` and
`trigger_server_restart()` so users can update Orac directly from the web
dashboard without losing databases, configs, or virtual environments.
"""

import os
import re
import sys
import time
import shutil
import zipfile
import tempfile
import threading
import subprocess
from pathlib import Path
from datetime import datetime as _dt

import requests

from .version import __version__
from .log_utils import log, LOGINFO, LOGWARNING, LOGERROR

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_API_URL = (
    "https://api.github.com/repos/Karhugit/Orac-Server-Distrib/releases/latest"
)
_GITHUB_RAW_VERSION_URL = (
    "https://raw.githubusercontent.com/Karhugit/Orac-Server-Distrib/main/resources/lib/version.py"
)
_GITHUB_RAW_CHANGELOG_URL = (
    "https://raw.githubusercontent.com/Karhugit/Orac-Server-Distrib/main/CHANGELOG.md"
)
_GITHUB_ZIP_URL = (
    "https://github.com/Karhugit/Orac-Server-Distrib/archive/refs/heads/main.zip"
)
_GITHUB_REPO_PAGE = (
    "https://github.com/Karhugit/Orac-Server-Distrib"
)
_GITHUB_RELEASES_PAGE = (
    "https://github.com/Karhugit/Orac-Server-Distrib/releases"
)
_REQUEST_TIMEOUT = 10  # seconds
_DOWNLOAD_TIMEOUT = 90  # seconds

# Files and directories that must NEVER be overwritten during a self-update
_PROTECTED_EXACT_NAMES = {
    "config.json",
    "tokens.json",
    "tzen_tokens.json",
    "orac.log",
    "orac.log.old",
    "anime_overrides.json",
    "anime_overrides.yaml",
    "anime_overrides.yml",
}
_PROTECTED_DIR_NAMES = {
    "venv",
    ".venv",
    ".git",
    "__pycache__",
}
_PROTECTED_SUFFIXES = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".pyc",
)

# ---------------------------------------------------------------------------
# Shared state (module-level, protected by a lock)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_update_in_progress = False
_state: dict = {
    "current_version": __version__,
    "latest_version": None,
    "update_available": False,
    "release_url": _GITHUB_REPO_PAGE,
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


def _extract_changelog_section(changelog_text: str, target_version: str) -> str:
    """
    Extract the markdown section for `target_version` (or the top-most version
    section) from CHANGELOG.md.
    """
    if not changelog_text:
        return ""
    lines = changelog_text.splitlines()
    capturing = False
    captured = []
    header_re = re.compile(r"^##\s+\[?v?([0-9]+\.[0-9]+\.[0-9]+)\]?")

    for line in lines:
        m = header_re.match(line.strip())
        if m:
            ver = m.group(1)
            if not capturing:
                if not target_version or ver == target_version.lstrip("v"):
                    capturing = True
                    captured.append(line)
            else:
                break
        elif capturing:
            captured.append(line)

    return "\n".join(captured).strip()


def _is_protected_path(rel_path: Path) -> bool:
    """Return True if `rel_path` (relative to server root) must not be overwritten."""
    parts = rel_path.parts
    if not parts:
        return True
    if parts[0] in _PROTECTED_DIR_NAMES:
        return True
    name = rel_path.name
    if name in _PROTECTED_EXACT_NAMES:
        return True
    if any(name.endswith(sfx) for sfx in _PROTECTED_SUFFIXES):
        return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_update_state() -> dict:
    """Return a snapshot of the current update state (thread-safe)."""
    with _lock:
        return dict(_state)


def check_for_update() -> dict:
    """
    Fetch the latest version from Orac-Server-Distrib (checking both raw
    `version.py` on `main` and GitHub Releases) and update the cached state.
    Blocking — run this in a daemon thread or via `asyncio.to_thread`.
    Never raises; errors are logged as warnings.
    """
    log("[Orac] Checking for Orac Server updates...", level=LOGINFO)
    headers = {
        "User-Agent": f"orac-server/{__version__}",
        "Cache-Control": "no-cache",
    }

    best_version_str = None
    best_tuple = (0, 0, 0)
    release_url = _GITHUB_REPO_PAGE
    release_notes = ""

    # 1. Check raw resources/lib/version.py on main branch of Orac-Server-Distrib
    try:
        cache_bust = int(time.time())
        ver_resp = requests.get(
            f"{_GITHUB_RAW_VERSION_URL}?t={cache_bust}",
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        if ver_resp.status_code == 200:
            m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', ver_resp.text)
            if m:
                raw_ver = m.group(1).strip().lstrip("v")
                raw_tuple = _parse_version(raw_ver)
                if raw_tuple > best_tuple:
                    best_tuple = raw_tuple
                    best_version_str = raw_ver
                    release_url = f"{_GITHUB_REPO_PAGE}/blob/main/CHANGELOG.md"
    except Exception as exc:
        log(f"[Orac] Raw version check warning: {exc}", level=LOGWARNING)

    # 2. Also check GitHub Releases API (in case a formal release with body exists)
    try:
        rel_resp = requests.get(
            _GITHUB_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                **headers,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        if rel_resp.status_code == 200:
            data = rel_resp.json()
            latest_tag = (data.get("tag_name") or "").lstrip("v").strip()
            rel_tuple = _parse_version(latest_tag)
            if rel_tuple >= best_tuple and rel_tuple > (0, 0, 0):
                best_tuple = rel_tuple
                best_version_str = latest_tag
                release_url = data.get("html_url") or _GITHUB_RELEASES_PAGE
                release_notes = (data.get("body") or "").strip()
    except Exception as exc:
        log(f"[Orac] GitHub Releases check warning: {exc}", level=LOGWARNING)

    # 3. If we found a version but don't have release notes yet, pull from CHANGELOG.md
    if best_version_str and not release_notes:
        try:
            cl_resp = requests.get(
                f"{_GITHUB_RAW_CHANGELOG_URL}?t={int(time.time())}",
                headers=headers,
                timeout=_REQUEST_TIMEOUT,
            )
            if cl_resp.status_code == 200:
                release_notes = _extract_changelog_section(cl_resp.text, best_version_str)
        except Exception:
            pass

    if not best_version_str:
        log("[Orac] Update check could not determine remote version.", level=LOGWARNING)
        return get_update_state()

    local_tuple = _parse_version(__version__)
    update_available = best_tuple > local_tuple

    with _lock:
        _state.update(
            {
                "current_version": __version__,
                "latest_version": best_version_str,
                "update_available": update_available,
                "release_url": release_url,
                "release_notes": release_notes,
                "last_checked": _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )

    if update_available:
        log(
            f"[Orac] *** Update available: v{best_version_str} "
            f"(running v{__version__}) — {release_url}",
            level=LOGWARNING,
        )
    else:
        log(
            f"[Orac] Orac Server v{__version__} is up to date (remote: v{best_version_str}).",
            level=LOGINFO,
        )

    return get_update_state()


def perform_self_update() -> dict:
    """
    Download the latest archive from Orac-Server-Distrib (`main` branch),
    extract it to a temporary directory, and copy code/asset files over the
    current Orac Server installation directory while preserving all databases,
    configuration files, logs, and virtual environments.
    """
    global _update_in_progress
    with _lock:
        if _update_in_progress:
            return {"success": False, "error": "An update is already in progress"}
        _update_in_progress = True

    server_root = Path(__file__).resolve().parent.parent.parent
    tmp_dir = None

    try:
        # Guard against accidentally running self-update inside the primary dev repo
        # if it has a .git directory pointing to the private dev repo (unless allowed)
        log(f"[Updater] Downloading latest Orac Server archive from {_GITHUB_ZIP_URL}...", level=LOGINFO)
        tmp_dir = Path(tempfile.mkdtemp(prefix="orac_update_"))
        zip_path = tmp_dir / "orac_update.zip"

        with requests.get(
            _GITHUB_ZIP_URL,
            headers={"User-Agent": f"orac-server/{__version__}"},
            stream=True,
            timeout=_DOWNLOAD_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"Failed to download update archive (HTTP {resp.status_code})",
                }
            with open(zip_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)

        extract_dir = tmp_dir / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        # GitHub zip archives have a single top-level folder like "Orac-Server-Distrib-main"
        subdirs = [p for p in extract_dir.iterdir() if p.is_dir()]
        source_root = subdirs[0] if len(subdirs) == 1 else extract_dir

        if not (source_root / "run_server.py").exists():
            return {
                "success": False,
                "error": "Downloaded archive does not appear to be a valid Orac Server package",
            }

        # Read the new version from the downloaded package
        new_version = None
        new_ver_file = source_root / "resources" / "lib" / "version.py"
        if new_ver_file.exists():
            m = re.search(
                r'__version__\s*=\s*["\']([^"\']+)["\']',
                new_ver_file.read_text(encoding="utf-8", errors="ignore"),
            )
            if m:
                new_version = m.group(1).strip()

        files_copied = 0
        for src_file in source_root.rglob("*"):
            if src_file.is_dir():
                continue
            rel_path = src_file.relative_to(source_root)
            if _is_protected_path(rel_path):
                continue

            dest_file = server_root / rel_path
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)
            files_copied += 1

        log(
            f"[Updater] Successfully updated {files_copied} files to v{new_version or 'latest'} in {server_root}",
            level=LOGINFO,
        )

        # Install any new/updated requirements before restarting
        req_file = server_root / "requirements.txt"
        if req_file.exists():
            try:
                log("[Updater] Ensuring Python dependencies are up to date...", level=LOGINFO)
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file)],
                    cwd=str(server_root),
                    timeout=60,
                    check=False,
                )
            except Exception as pip_err:
                log(f"[Updater] Warning during pip install: {pip_err}", level=LOGWARNING)

        with _lock:
            if new_version:
                _state["current_version"] = new_version
                _state["latest_version"] = new_version
            _state["update_available"] = False

        return {
            "success": True,
            "updated_to": new_version or _state.get("latest_version") or __version__,
            "files_updated": files_copied,
        }

    except Exception as exc:
        log(f"[Updater] Error performing self-update: {exc}", level=LOGERROR)
        return {"success": False, "error": str(exc)}
    finally:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        with _lock:
            _update_in_progress = False


def trigger_server_restart(delay_sec: float = 1.5) -> None:
    """
    Restart the running Orac Server process after `delay_sec` seconds.
    - If started via the updated `start_server.bat` / `start_server.sh` (which
      sets `ORAC_LAUNCHER_LOOP=1`), exits with code 42 so the wrapper script
      loops and starts the new server cleanly.
    - Otherwise, spawns a detached helper process that waits 2 seconds for the
      current process to release the HTTP port and launches `run_server.py`.
    """
    def _restart_worker():
        time.sleep(delay_sec)
        server_root = Path(__file__).resolve().parent.parent.parent
        run_script = server_root / "run_server.py"

        if os.environ.get("ORAC_LAUNCHER_LOOP") == "1":
            log("[Updater] Exiting with code 42 for launcher auto-restart...", level=LOGINFO)
            os._exit(42)

        log("[Updater] Spawning detached restarter for run_server.py...", level=LOGINFO)
        try:
            helper_code = (
                "import time, subprocess, sys; "
                "time.sleep(2.0); "
                f"subprocess.Popen([{sys.executable!r}, '-W', 'ignore::SyntaxWarning', {str(run_script)!r}], "
                f"cwd={str(server_root)!r})"
            )
            creationflags = 0
            if sys.platform == "win32":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(
                [sys.executable, "-c", helper_code],
                cwd=str(server_root),
                creationflags=creationflags,
                close_fds=(sys.platform != "win32"),
            )
        except Exception as exc:
            log(f"[Updater] Failed to spawn detached restarter: {exc}", level=LOGERROR)

        os._exit(42)

    threading.Thread(target=_restart_worker, daemon=True, name="OracRestarter").start()
