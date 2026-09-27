# Orac Server

Orac Server is a Python-based media server application that integrates with Trakt.tv and TMDb to manage your media library, track watched status, and scrape streams from various sources. It was originally designed as a Kodi addon service but has been adapted to run as a standalone server.

## Features

- **Trakt Integration**: Syncs your lists, collection, and watched history with Trakt.tv.
- **TMDb Integration**: Fetches metadata for movies and TV shows.
- **Scraping**: multi-threaded scraping framework to find media streams (Torrents, etc.).
- **Caching**: Uses local SQLite databases to cache metadata and reduce API calls.
- **API**: Provides a JSON HTTP API for client applications to interact with.

## Prerequisites

- Python 3.8+
- [Trakt.tv](https://trakt.tv/) API Application (Client ID & Secret)
- [TMDb](https://www.themoviedb.org/) API Key

## Installation

1.  Clone the repository:
    ```bash
    git clone https://github.com/yourusername/orac_server.git
    cd orac_server
    ```

2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

3.  Configure the application:
    - Copy `config.example.json` to `config.json`.
    - Edit `config.json` and fill in your API keys and paths.

    ```json
    {
      "trakt": {
        "client_id": "YOUR_TRAKT_CLIENT_ID",
        "client_secret": "YOUR_TRAKT_CLIENT_SECRET"
      },
      "tmdb": {
        "api_key": "YOUR_TMDB_API_KEY"
      },
      "server": {
        "port": 5555
      },
      "env": {
        "orac_env": "LOCAL",
        "log_path": "."
      }
    }
    ```

## Usage

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
