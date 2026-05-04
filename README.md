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

1.  Download the code as a zip file:

2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

3.
4.  ## Usage

Start the server:

```bash
python run_server.py
```

The server will start on the configured port (default: 5555).

## API Endpoints

-   **GET /ping**: Check server status.
-   **GET /movie?tmdb_id=<id>**: Get movie details.
-   **GET /show?tmdb_id=<id>**: Get show details.
-   **GET /scrape?tmdb_id=<id>&item_type=<movie|episode>**: Scrape for streams.
-   **PUT /watched**: Mark an item as watched.
-   **GET /list?name=<list_name>**: Get items from a specific list.

## Architecture

-   **`run_server.py`**: Entry point. initializes databases and starts the HTTP server.
-   **`resources/lib/http_server.py`**: Handles HTTP requests and routes them to appropriate handlers.
-   **`resources/scrapers/`**: Contains scraper modules.
-   **`resources/lib/trakt_handler.py`**: Handles Trakt API authentication and requests.
-   **`resources/lib/queue_worker.py`**: Background worker for processing Trakt updates.

## Data Storage

Orac Server uses several SQLite databases for caching:
-   `movies_static.db`: Static movie metadata.
-   `movies_dynamic.db`: Dynamic user state (watched status, ratings).
-   `tvshows_static.db`: Static TV show/episode metadata.
-   `tvshows_dynamic.db`: User state for TV shows.
-   `lists.db`: Caches Trakt lists.
-   `trakt_update_queue.db`: Queue for background Trakt sync operations.
