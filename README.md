# Orac Server

Orac Server is a Python-based media server application that integrates with Trakt.tv, TMDb, Simkl and MDBLIST to manage your media library, track watched status, and scrape streams from various sources. It runs as a standalone server.

## Features

- **Trakt Integration**: Syncs your lists, collection, and watched history with Trakt.tv.
- **TMDb Integration**: Syncs metadata and lists for movies and TV shows.
- **SIMKL Integration**: Syncs watchlist and watched history.
- **MDBLIST Integration**: Syncs your lists and watched history
- **Scraping**: multi-threaded scraping framework to find media streams (Torrents, etc.).
- **Caching**: Uses local SQLite databases to cache metadata and reduce API calls.
- **API**: Provides a JSON HTTP API for client applications to interact with.

## Prerequisites

- Python 3.8+
- VENV for Linux virtual environments
- Docker for Docker environments

## Installation

## 1.  Download the code as a zip file. Extract to a new folder ##

## 2.  For Windows : ##
   Either
   
       Use the installer OracServerSetup, this will create a new app 'Orac Server' with two options, 'Start Orac server' and 'Stop Orac server'
       Select the app from the list and select 'Start Orac Server'
   Or
   
       In the new folder select the windows batch file start_server.bat

## 3.  For Linux : ##
   Either
   
        Run as server in a terminal: 
           "python3 run_server.py"
           Stop it via CTRL-C
   Or
       
       Run in a virtual environment
           "bash start_server.sh"
           Stop it via CTRL-C
   Or
       
       Run in a docker container
           "docker compose up"
           Stop it via "docker compose down"

        
## 4.  Usage ##

The server will start on the configured port (default: 5555). You should see it running in the terminal. It has a dashboard which you can access on http://localhost:5555/web. Try this and you should see the 
orac dashboard, if not then the server did not start.


5. ## Data Storage ##

Orac Server uses several SQLite databases for caching:
-   `movies_static.db`: Static movie metadata.
-   `movies_dynamic.db`: Dynamic user state (watched status, ratings).
-   `tvshows_static.db`: Static TV show/episode metadata.
-   `tvshows_dynamic.db`: User state for TV shows.
-   `lists.db`: Caches lists as indexes to the DBs above.
-   `trakt_update_queue.db`: Queue for background Trakt sync operations.
