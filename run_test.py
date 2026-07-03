from pathlib import Path
import sys
import os
import importlib
import time
import asyncio


# Force Python to import xbmc from "custom_modules" subdirectory
script_dir = os.path.dirname(os.path.abspath(__file__))  # Get current script directory
xbmc_path = os.path.join(script_dir, "stubs")   # Define path to the subdirectory

# Insert at the beginning of sys.path
sys.path.insert(0, xbmc_path)
from resources.lib.trakt_handler import TraktAuth  # Import the TraktAuth class from trakt_handler.py

cache_file = Path("movies_dynamic_cache.db")  # Change to the actual path

if cache_file.exists():
    cache_file.unlink()
    print("movies_dynamic_cache.db deleted successfully")
else:
    print("movies_dynamic_cache.db not found")

cache_file = Path("movies_static_cache.db")  # Change to the actual path

if cache_file.exists():
    cache_file.unlink()
    print("movies_static_cache.db deleted successfully")
else:
    print("movies_static_cache.db not found")

from resources.lib.db_init import init_static_movie_db, init_dynamic_movie_db
# Initialize the static and dynamic databases
static_db_path = "movies_static_cache.db"  # Replace with the actual path to your static DB
dynamic_db_path = "movies_dynamic_cache.db"  # Replace with the actual path to your dynamic DB
if not init_static_movie_db(static_db_path):
    print(f"Failed to initialize static DB at {static_db_path}")
if not init_dynamic_movie_db(dynamic_db_path):
    print(f"Failed to initialize dynamic DB at {dynamic_db_path}")

addon = "service.orac"  # Replace with your actual addon ID
config_db_path = "config.db"
trakt_handler = TraktAuth(
    addon=addon,
    config_db_path=config_db_path,
    client_id="378e7c8adf3569e809b57a26e318dee3d4080e3c58dafa817537f6b7d6662cd6",  # Replace with your actual Trakt client ID
    client_secret="e454afd65b734faea58be818af256bb05e88e6151404df987d5716025dbc0b29",  # Replace with your actual Trakt client secret
)

trakt_handler.token_file = "tokens.json"  # Replace with the actual path to your token file
#print(f"[Orac] Token file path: {trakt_handler.token_file}")
#tokens = trakt_handler.get_saved_tokens()
#print(f"Tokens: {tokens}")

#lists = trakt_handler.get("/users/me/lists")
#print(f"Lists response: {lists}")

from resources.lib.sync_trakt_with_db import trakt_list_sync_task
from resources.lib.tmdb_handler import TMDbAPI
static_db_path = "movies_static_cache.db"  # Replace with the actual path to your static DB
dynamic_db_path = "movies_dynamic_cache.db"  # Replace with the actual path to your dynamic DB
tmdb_handler = TMDbAPI(api_key="b8f106f33261688001712a149f6f6990")
asyncio.run(trakt_list_sync_task(
    trakt_auth=trakt_handler,
    tmdb_handler=tmdb_handler,
    lists_db_path="lists_cache.db",
    movie_static_db_path=static_db_path,
    movie_dynamic_db_path=dynamic_db_path,
    tvshows_static_db_path="tvshows_static_cache.db",
    tvshows_dynamic_db_path="tvshows_dynamic_cache.db",
    trakt_queue_path="trakt_update_queue.db",
    username="test_user"
))

# print out the movies static db
import sqlite3
conn = sqlite3.connect(static_db_path)
cursor = conn.cursor()
input("Press Enter to continue...")  # Wait for user input
cursor.execute("SELECT * FROM movies")
rows = cursor.fetchall()

print("Movies in static DB:")
for row in rows:
    print(row)
conn.close()

# print out the movies dynamic db
conn = sqlite3.connect(dynamic_db_path)
cursor = conn.cursor()
cursor.execute("SELECT * FROM movie_lists")
rows = cursor.fetchall()
print("Movies_lists in dynamic DB:")
for row in rows:
    print(row)
cursor.execute("SELECT * FROM movie_status")
rows = cursor.fetchall()
print("Movies_status in dynamic DB:")
for row in rows:
    print(row)

conn.close()
