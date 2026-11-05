import os
import re
import string
import mimetypes
from typing import Optional, List, Dict, Any
from resources.lib.db_utils import db_connect
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGWARNING
from resources.lib.config_handler import get_config_value, update_config_values

SUPPORTED_VIDEO_EXTENSIONS = (
    '.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm',
    '.m4v', '.ts', '.m2ts', '.strm', '.iso', '.mpg', '.mpeg'
)


def _normalize_title(text: str) -> str:
    """Normalize a title string by stripping accents/punctuation and collapsing spaces."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r'[\'\"’“”\.\-_:;,/\\&()\[\]{}]', ' ', text)
    return ' '.join(text.split())


def _extract_tokens(text: str) -> List[str]:
    """Extract clean words/tokens from title for matching."""
    norm = _normalize_title(text)
    # Remove common stop words for matching robustness
    tokens = [w for w in norm.split() if len(w) > 0 and w not in ('the', 'a', 'an', 'and', 'of', 'in', 'on', 'to')]
    return tokens if tokens else norm.split()


def get_local_folders_config(config_db_path: str) -> Dict[str, Dict[str, str]]:
    """Fetches configuration for all 5 local folder slots from config.db."""
    folders = {}
    for slot in range(1, 6):
        s = str(slot)
        display_name = get_config_value(f"local_folder.{s}.display_name", config_db_path, default=f"Folder {s}")
        movies_dir = get_config_value(f"local_folder.{s}.movies_directory", config_db_path, default="none")
        tv_shows_dir = get_config_value(f"local_folder.{s}.tv_shows_directory", config_db_path, default="none")
        folders[s] = {
            "display_name": display_name or f"Folder {s}",
            "movies_directory": movies_dir or "none",
            "tv_shows_directory": tv_shows_dir or "none"
        }
    return folders


def save_local_folder_config(config_db_path: str, folder_id: Any, data: Dict[str, Any]) -> bool:
    """Saves configuration for a specific folder slot (1-5) into config.db."""
    slot = str(folder_id).lower().replace("folder", "").strip()
    if slot not in ("1", "2", "3", "4", "5"):
        return False
    updates = {}
    if "display_name" in data:
        updates[f"local_folder.{slot}.display_name"] = data["display_name"].strip() or f"Folder {slot}"
    if "movies_directory" in data:
        val = data["movies_directory"].strip() if data["movies_directory"] else "none"
        updates[f"local_folder.{slot}.movies_directory"] = val or "none"
    if "tv_shows_directory" in data:
        val = data["tv_shows_directory"].strip() if data["tv_shows_directory"] else "none"
        updates[f"local_folder.{slot}.tv_shows_directory"] = val or "none"

    if updates:
        update_config_values(updates, config_db_path)
    return True


def browse_system_dir(path: Optional[str] = None) -> Dict[str, Any]:
    """
    Lists system drives or directories inside the specified path.
    Supports local drives and UNC network paths on Windows.
    """
    clean_path = (path or "").strip()
    
    # Root level (no path specified): enumerate system drives on Windows, or root on Unix
    if not clean_path:
        if os.name == "nt":
            drives = []
            for letter in string.ascii_uppercase:
                drive_root = f"{letter}:\\"
                try:
                    if os.path.exists(drive_root):
                        drives.append({
                            "name": f"Local Disk ({letter}:)",
                            "path": drive_root,
                            "is_dir": True,
                            "is_drive": True
                        })
                except Exception:
                    pass
            return {
                "success": True,
                "current_path": "",
                "parent_path": "",
                "directories": drives,
                "drives": drives
            }
        else:
            clean_path = "/"

    # Normalize the path
    norm_path = os.path.normpath(clean_path)
    # Ensure Windows drive roots have trailing slash
    if re.match(r'^[a-zA-Z]:$', norm_path):
        norm_path += "\\"

    if not os.path.exists(norm_path):
        return {
            "success": False,
            "error": f"Path does not exist: {norm_path}",
            "current_path": norm_path,
            "parent_path": "",
            "directories": []
        }

    if not os.path.isdir(norm_path):
        return {
            "success": False,
            "error": f"Path is not a directory: {norm_path}",
            "current_path": norm_path,
            "parent_path": "",
            "directories": []
        }

    # Determine parent path
    parent_path = os.path.dirname(norm_path)
    if parent_path == norm_path or not parent_path or re.match(r'^[a-zA-Z]:\\?$', norm_path):
        parent_path = ""

    directories = []
    try:
        with os.scandir(norm_path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        name = entry.name
                        # Skip system and hidden metadata directories
                        if name.startswith('$') or name in ("System Volume Information", ".git", ".cache", "AppData"):
                            continue
                        directories.append({
                            "name": name,
                            "path": entry.path,
                            "is_dir": True,
                            "is_drive": False
                        })
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError) as e:
        return {
            "success": False,
            "error": f"Access denied: {e}",
            "current_path": norm_path,
            "parent_path": parent_path,
            "directories": []
        }

    directories.sort(key=lambda x: x["name"].lower())

    return {
        "success": True,
        "current_path": norm_path,
        "parent_path": parent_path,
        "directories": directories
    }


def scan_local_media_folders(
    config_db_path: str,
    folder_id: Optional[str] = None,
    media_type: str = "movie",
    title: str = "",
    year: Optional[int] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    aliases: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Scans configured Orac local media folders for video files matching the requested movie or episode.
    Returns a list of matching file records with paths and sizes.
    """
    if not title:
        return []

    target_slots = []
    if folder_id:
        slot = str(folder_id).lower().replace("folder", "").strip()
        if slot in ("1", "2", "3", "4", "5"):
            target_slots.append(slot)
    if not target_slots:
        target_slots = ["1", "2", "3", "4", "5"]

    # Collect directories to scan
    scan_targets = []
    dir_key_suffix = "movies_directory" if media_type == "movie" else "tv_shows_directory"
    for slot in target_slots:
        dir_val = get_config_value(f"local_folder.{slot}.{dir_key_suffix}", config_db_path, default="none")
        if dir_val and dir_val.lower() not in ("none", "empty_setting", ""):
            norm_dir = os.path.normpath(dir_val)
            if os.path.isdir(norm_dir):
                scan_targets.append((slot, norm_dir))

    if not scan_targets:
        return []

    title_tokens = _extract_tokens(title)
    if not title_tokens:
        return []

    # Prepare alias tokens
    alias_token_sets = []
    if aliases:
        for alias in aliases:
            a_tokens = _extract_tokens(alias)
            if a_tokens:
                alias_token_sets.append(a_tokens)

    results = []
    seen_paths = set()

    for slot, base_dir in scan_targets:
        try:
            if media_type == "movie":
                _scan_movie_dir(
                    base_dir=base_dir,
                    slot=slot,
                    title_tokens=title_tokens,
                    alias_token_sets=alias_token_sets,
                    year=year,
                    results=results,
                    seen_paths=seen_paths
                )
            else:
                _scan_tv_dir(
                    base_dir=base_dir,
                    slot=slot,
                    title_tokens=title_tokens,
                    alias_token_sets=alias_token_sets,
                    season=season,
                    episode=episode,
                    results=results,
                    seen_paths=seen_paths
                )
        except Exception as e:
            log(f"[Orac] Error scanning local folder {slot} ({base_dir}): {e}", LOGERROR)

    return results


def _match_tokens(name_tokens: List[str], required_tokens: List[str]) -> bool:
    """Checks if all required tokens appear in name_tokens in sequence or set."""
    if not required_tokens:
        return False
    name_set = set(name_tokens)
    return all(tok in name_set for tok in required_tokens)


def _scan_movie_dir(
    base_dir: str,
    slot: str,
    title_tokens: List[str],
    alias_token_sets: List[List[str]],
    year: Optional[int],
    results: List[Dict[str, Any]],
    seen_paths: set
):
    """Walks the movie directory searching for matching video files."""
    for root, dirs, files in os.walk(base_dir, followlinks=False):
        # Check files in current directory
        for fname in files:
            ext = os.path.splitext(fname)[-1].lower()
            if ext not in SUPPORTED_VIDEO_EXTENSIONS:
                continue

            full_path = os.path.join(root, fname)
            if full_path in seen_paths:
                continue

            file_norm = _normalize_title(fname)
            file_tokens = _extract_tokens(fname)
            parent_norm = _normalize_title(os.path.basename(root))
            parent_tokens = _extract_tokens(os.path.basename(root))

            combined_tokens = file_tokens + parent_tokens

            # Title match check
            matched = _match_tokens(combined_tokens, title_tokens)
            if not matched:
                for a_tokens in alias_token_sets:
                    if _match_tokens(combined_tokens, a_tokens):
                        matched = True
                        break

            if not matched:
                continue

            # Year check if specified
            if year:
                # Find all 4-digit years in filename/parent
                years_found = [int(y) for y in re.findall(r'\b(19\d\d|20\d\d)\b', f"{fname} {os.path.basename(root)}")]
                if years_found:
                    # Year must match +/- 1 year
                    if not any(abs(y - year) <= 1 for y in years_found):
                        continue

            try:
                size_bytes = os.path.getsize(full_path)
            except OSError:
                size_bytes = 0

            seen_paths.add(full_path)
            results.append({
                "name": fname,
                "path": full_path,
                "size": size_bytes,
                "size_gb": round(size_bytes / (1024 ** 3), 2),
                "slot": f"folder{slot}",
                "folder_slot": slot
            })


def _scan_tv_dir(
    base_dir: str,
    slot: str,
    title_tokens: List[str],
    alias_token_sets: List[List[str]],
    season: Optional[int],
    episode: Optional[int],
    results: List[Dict[str, Any]],
    seen_paths: set
):
    """Walks the TV directory searching for matching episode video files."""
    if season is None or episode is None:
        return

    # Season and episode pattern regex
    # Matches S01E02, 1x02, S1E2, Ep02, Episode 02, etc.
    s_num = int(season)
    e_num = int(episode)
    ep_pattern = re.compile(
        rf'(?:s0*{s_num}[\.\-_ ]*e0*{e_num}\b|\b{s_num}x0*{e_num}\b|\bs0*{s_num}[\.\-_ ]*ep0*{e_num}\b)',
        re.IGNORECASE
    )

    for root, dirs, files in os.walk(base_dir, followlinks=False):
        for fname in files:
            ext = os.path.splitext(fname)[-1].lower()
            if ext not in SUPPORTED_VIDEO_EXTENSIONS:
                continue

            full_path = os.path.join(root, fname)
            if full_path in seen_paths:
                continue

            # Check season & episode match
            if not ep_pattern.search(fname):
                continue

            # Check show title in file name or directory ancestors
            rel_path = os.path.relpath(full_path, base_dir)
            path_tokens = _extract_tokens(rel_path)

            matched = _match_tokens(path_tokens, title_tokens)
            if not matched:
                for a_tokens in alias_token_sets:
                    if _match_tokens(path_tokens, a_tokens):
                        matched = True
                        break

            if not matched:
                continue

            try:
                size_bytes = os.path.getsize(full_path)
            except OSError:
                size_bytes = 0

            seen_paths.add(full_path)
            results.append({
                "name": fname,
                "path": full_path,
                "size": size_bytes,
                "size_gb": round(size_bytes / (1024 ** 3), 2),
                "slot": f"folder{slot}",
                "folder_slot": slot
            })
