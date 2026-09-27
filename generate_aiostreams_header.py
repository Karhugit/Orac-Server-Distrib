"""
generate_aiostreams_header.py
=============================
Generates the x-aiostreams-user-data header value for the Orac Server
AIOStreams scraper and writes it into config.json.

The header is a base64-encoded UTF-8 JSON blob describing which addon
presets AIOStreams should query, how to format results, sort criteria,
and deduplication behaviour.

HOW TO USE
----------
1. Edit the configuration sections below (PRESETS, FORMATTER, etc.)
2. Run:  python generate_aiostreams_header.py
3. The script prints the generated base64 value and writes it to config.json
   under the key  AIOSTREAMS -> user_data_header

The Orac Server's aiostreams.py scraper will then read this value at
runtime instead of using a hardcoded string.
"""

import base64
import json
import os

# ──────────────────────────────────────────────────────────────────────────────
# PRESET CONFIGURATION
# Each entry in PRESETS represents one addon that AIOStreams will query.
# Fields common to every preset:
#   type        (str)  – addon identifier used by AIOStreams
#   instanceId  (str)  – arbitrary unique ID (any short string you like)
#   enabled     (bool) – whether this preset is active
#   options     (dict) – preset-specific settings (see per-preset comments)
# ──────────────────────────────────────────────────────────────────────────────

PRESETS = [

    # ── Torrentio ────────────────────────────────────────────────────────────
    # Public torrent scraper.  No auth required.
    # options:
    #   name      (str)  – display label
    #   timeout   (int)  – request timeout in ms
    #   resources (list) – which resource types to fetch; typically ["stream"]
    #   sort      (str)  – result ordering: "quality" | "size" | "seeders"
    {
        "type": "torrentio",
        "instanceId": "tio1",
        "enabled": True,   # Python True → JSON true automatically via json.dumps()
        "options": {
            "name": "Torrentio",
            "timeout": 6500,
            "resources": ["stream"],
            "sort": "quality",
        },
    },

    # ── Comet ────────────────────────────────────────────────────────────────
    # Debrid-aware torrent scraper.
    # options:
    #   name          (str)  – display label
    #   timeout       (int)  – ms
    #   resources     (list) – ["stream"]
    #   includeP2P    (bool) – include non-debrid P2P results
    #   removeTrash   (bool) – filter out low-quality / cam releases
    {
        "type": "comet",
        "instanceId": "f7b",
        "enabled": True,
        "options": {
            "name": "Comet",
            "timeout": 6500,
            "resources": ["stream"],
            "includeP2P": True,
            "removeTrash": False,
        },
    },

    # ── MediaFusion ──────────────────────────────────────────────────────────
    # Requires a REAL instanceId tied to your debrid credentials.
    # A made-up instanceId causes AIOStreams to hit an invalid URL on the
    # MediaFusion server → "Request-Error" in the Orac log.
    #
    # To enable: go to https://mediafusion.elfhosted.com, configure your
    # debrid service (Real-Debrid, AllDebrid, etc.), click Install/Generate,
    # then copy the token from the generated URL into instanceId below and
    # set enabled=True.
    {
        "type": "mediafusion",
        "instanceId": "450",   # get from mediafusion.elfhosted.com
        "enabled": True,
        "options": {
            "name": "MediaFusion",
            "timeout": 6500,
            "resources": ["stream"],
            "useCachedResultsOnly": True,   # only return cached debrid links
            "enableWatchlistCatalogs": False,
            "downloadViaBrowser": False,
            "contributorStreams": False,
            "certificationLevelsFilter": [],
            "nudityFilter": [],
        },
    },

    # ── Anime ────────────────────────────────────────────────────────────────
    # Anime-specific sources.
    # options:
    #   sources (list) – which anime sites to scrape
    #                    e.g. "roro", "gogoanime", "tenshi", "animepahe"
    {
        "type": "anime",
        "instanceId": "ani1",
        "enabled": False,           # set True to enable anime scraping
        "options": {
            "sources": ["roro", "gogoanime", "tenshi", "animepahe"],
        },
    },

    # ── Add more presets here ────────────────────────────────────────────────
    # Example – Knightcrawler (another torrent scraper):
    # {
    #     "type": "knightcrawler",
    #     "instanceId": "kc1",
    #     "enabled": False,
    #     "options": {
    #         "name": "Knightcrawler",
    #         "timeout": 6500,
    #         "resources": ["stream"],
    #         "sort": "quality",
    #     },
    # },
]

# ──────────────────────────────────────────────────────────────────────────────
# FORMATTER
# Controls how AIOStreams displays stream titles.
# id         (str)  – which formatter to use: "torrentio" | "gdrive" | "custom"
# definition (dict) – formatter-specific metadata
#   name        (str) – label shown in results
#   description (str) – subtitle/description shown in results
# ──────────────────────────────────────────────────────────────────────────────

FORMATTER = {
    "id": "torrentio",
    "definition": {
        "name": "Torrentio Formatter",
        "description": "Sort by quality",
    },
}

# ──────────────────────────────────────────────────────────────────────────────
# SORT CRITERIA
# global (list) – ordered list of sort keys applied to the combined result set.
# Available keys (as supported by the AIOStreams version you are using):
#   "quality"  – resolution (4K > 1080p > 720p > …)
#   "size"     – file size descending
#   "seeders"  – torrent seeders descending
#   "cached"   – cached debrid links first
# Use an empty list [] for no global sort (rely on per-preset sort instead).
# ──────────────────────────────────────────────────────────────────────────────

SORT_CRITERIA = {
    "global": [
        {"key": "quality", "direction": "desc"}
    ]
}

# ──────────────────────────────────────────────────────────────────────────────
# DEDUPLICATOR
# Removes duplicate results across presets.
# enabled             (bool) – master switch
# keys                (list) – which fields to deduplicate on:
#                               "filename" | "infoHash" | "url"
# multiGroupBehaviour (str)  – how to handle multiple groups:
#                               "aggressive" | "conservative"
# cached              (str)  – keep how many cached results per group:
#                               "single_result" | "per_service" | "all"
# uncached            (str)  – same for uncached results
# p2p                 (str)  – same for P2P results
# excludeAddons       (list) – instanceId strings to exclude from dedup
# ──────────────────────────────────────────────────────────────────────────────

DEDUPLICATOR = {
    "enabled": True,
    "keys": ["filename", "infoHash"],
    "multiGroupBehaviour": "aggressive",
    "cached": "single_result",
    "uncached": "per_service",
    "p2p": "single_result",
    "excludeAddons": [],
}

# ──────────────────────────────────────────────────────────────────────────────
# BUILDER  –  do not edit below here unless you know what you are doing
# ──────────────────────────────────────────────────────────────────────────────

def build_user_data() -> dict:
    """Assemble the full AIOStreams user-data payload as a Python dict."""
    return {
        "presets": PRESETS,
        "formatter": FORMATTER,
        "sortCriteria": SORT_CRITERIA,
        "deduplicator": DEDUPLICATOR,
    }


def encode_user_data(payload: dict) -> str:
    """Serialise to compact JSON and base64-encode (no padding issues)."""
    json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(json_bytes).decode("ascii")


def update_config_json(header_value: str, config_path: str) -> None:
    """Write the header value into config.json under AIOSTREAMS.user_data_header."""
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = {}

    config.setdefault("AIOSTREAMS", {})["user_data_header"] = header_value

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)
    print("  OK  Written to %s" % config_path)


def preview_payload(payload: dict) -> None:
    """Pretty-print the payload so you can verify it before encoding."""
    print("\n-- Payload preview --------------------------------------------")
    print(json.dumps(payload, indent=2))
    print("---------------------------------------------------------------\n")


if __name__ == "__main__":
    # Locate config.json relative to this script's directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(script_dir, "orac_server")):
        config_path = os.path.join(script_dir, "orac_server", "config.json")
    else:
        config_path = os.path.join(script_dir, "config.json")

    payload = build_user_data()
    preview_payload(payload)

    header = encode_user_data(payload)
    print("Generated header value:\n\n%s\n" % header)

    update_config_json(header, config_path)

    # Also decode & verify round-trip
    decoded = json.loads(base64.b64decode(header).decode("utf-8"))
    assert decoded == payload, "Round-trip verification FAILED"
    print("  OK  Round-trip base64 -> JSON verification passed")
    print("\nDone.  Restart the Orac Server for changes to take effect.")

# Original header
#'ewogICJwcmVzZXRzIjogWwogICAgewogICAgICAidHlwZSI6ICJjb21ldCIsCiAgICAgICJpbnN0YW5j' 
# 'ZUlkIjogImY3YiIsCiAgICAgICJlbmFibGVkIjogdHJ1ZSwKICAgICAgIm9wdGlvbnMiOiB7CiAgICAg' 
# 'ICAgIm5hbWUiOiAiQ29tZXQiLAogICAgICAgICJ0aW1lb3V0IjogNjUwMCwKICAgICAgICAicmVzb3Vy' 
# 'Y2VzIjogWyJzdHJlYW0iXSwKICAgICAgICAiaW5jbHVkZVAyUCI6IHRydWUsCiAgICAgICAgInJlbW92' 
# 'ZVRyYXNoIjogZmFsc2UKICAgICAgfQogICAgfSwKICAgIHsKICAgICAgInR5cGUiOiAibWVkaWFmdXNp' 
# 'b24iLAogICAgICAiaW5zdGFuY2VJZCI6ICI0NTAiLAogICAgICAiZW5hYmxlZCI6IHRydWUsCiAgICAg' 
# 'ICJvcHRpb25zIjogewogICAgICAgICJuYW1lIjogIk1lZGlhRnVzaW9uIiwKICAgICAgICAidGltZW91' 
# 'dCI6IDY1MDAsCiAgICAgICAgInJlc291cmNlcyI6IFsic3RyZWFtIl0sCiAgICAgICAgInVzZUNhY2hl' 
# 'ZFJlc3VsdHNPbmx5IjogdHJ1ZSwKICAgICAgICAiZW5hYmxlV2F0Y2hsaXN0Q2F0YWxvZ3MiOiBmYWxz' 
# 'ZSwKICAgICAgICAiZG93bmxvYWRWaWFCcm93c2VyIjogZmFsc2UsCiAgICAgICAgImNvbnRyaWJ1dG9y' 
# 'U3RyZWFtcyI6IGZhbHNlLAogICAgICAgICJjZXJ0aWZpY2F0aW9uTGV2ZWxzRmlsdGVyIjogW10sCiAg' 
# 'ICAgICAgIm51ZGl0eUZpbHRlciI6IFtdCiAgICAgIH0KICAgIH0KICBdLAogICJmb3JtYXR0ZXIiOiB7' 
# 'CiAgICAiaWQiOiAidG9ycmVudGlvIiwKICAgICJkZWZpbml0aW9uIjogeyJuYW1lIjogIiIsICJkZXNj' 
# 'cmlwdGlvbiI6ICIifQogIH0sCiAgInNvcnRDcml0ZXJpYSI6IHsiZ2xvYmFsIjogW119LAogICJkZWR1' 
# 'cGxpY2F0b3IiOiB7CiAgICAiZW5hYmxlZCI6IGZhbHNlLAogICAgImtleXMiOiBbImZpbGVuYW1lIiwg' 
# 'ImluZm9IYXNoIl0sCiAgICAibXVsdGlHcm91cEJlaGF2aW91ciI6ICJhZ2dyZXNzaXZlIiwKICAgICJj' 
# 'YWNoZWQiOiAic2luZ2xlX3Jlc3VsdCIsCiAgICAidW5jYWNoZWQiOiAicGVyX3NlcnZpY2UiLAogICAg' 
# 'InAycCI6ICJzaW5nbGVfcmVzdWx0IiwKICAgICJleGNsdWRlQWRkb25zIjogW10KICB9Cn0='
