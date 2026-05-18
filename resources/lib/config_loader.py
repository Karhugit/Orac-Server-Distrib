import os
import json
from pathlib import Path

class ConfigLoader:
    def __init__(self, config_file="config.json"):
        self.project_root = Path(__file__).resolve().parent.parent.parent
        self.config_file = self.project_root / config_file
        self.config = self._load_config()

    def _load_config(self):
        """Loads configuration from file or returns defaults."""
        config = {
            "ORAC_ENV": os.environ.get("ORAC_ENV", "LOCAL"),
            "ORAC_LOG_FILE": os.environ.get("ORAC_LOG_FILE", "TRUE"),
            "ORAC_LOG_PATH": os.environ.get("ORAC_LOG_PATH", str(self.project_root / "orac.log")),
            "DB_PATHS": {
                "movies_static": str(self.project_root / "movies_static_cache.db"),
                "movies_dynamic": str(self.project_root / "movies_dynamic_cache.db"),
                "lists": str(self.project_root / "lists_cache.db"),
                "tvshows_static": str(self.project_root / "tvshows_static_cache.db"),
                "tvshows_dynamic": str(self.project_root / "tvshows_dynamic_cache.db"),
                "trakt_update_queue": str(self.project_root / "update_queue.db"),
                "config": str(self.project_root / "config.db"),
                "ext_indexes": str(self.project_root / "ext_indexes.db"),
                "tags": str(self.project_root / "tags_cache.db"),
                "undesirables": str(self.project_root / "undesirables.db"),
                "trakt_history_sync": str(self.project_root / "trakt_history_sync.db"),
            },
            "TRAKT": {
                "client_id": os.environ.get("TRAKT_CLIENT_ID", ""),
                "client_secret": os.environ.get("TRAKT_CLIENT_SECRET", ""),
            },
            "TMDB": {
                "api_key": os.environ.get("TMDB_API_KEY", ""),
            },
            "SERVER": {
                "port": int(os.environ.get("ORAC_PORT", 5555))
            }
        }

        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    file_config = json.load(f)
                    # Deep update logic could go here, for now simple top-level overrides
                    if "DB_PATHS" in file_config:
                        config["DB_PATHS"].update(file_config["DB_PATHS"])
                    if "TRAKT" in file_config:
                        config["TRAKT"].update(file_config["TRAKT"])
                    if "TMDB" in file_config:
                        config["TMDB"].update(file_config["TMDB"])
                    if "SERVER" in file_config:
                        config["SERVER"].update(file_config["SERVER"])
                    
                    # Update root level keys if they exist
                    for key in ["ORAC_ENV", "ORAC_LOG_FILE", "ORAC_LOG_PATH"]:
                        if key in file_config:
                            config[key] = file_config[key]
            except Exception as e:
                print(f"Error loading config file: {e}")
        
        return config

    def get(self, key, default=None):
        return self.config.get(key, default)

    @property
    def db_paths(self):
        return self.config["DB_PATHS"]
    
    @property
    def trakt_config(self):
        return self.config["TRAKT"]

    @property
    def tmdb_config(self):
        return self.config["TMDB"]

    @property
    def server_config(self):
        return self.config["SERVER"]
