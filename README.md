# Orac Server

Orac Server is a Python-based media server application that integrates with Trakt, Simkl, TMDb and punchplay to manage your media library, track watched status, and scrape streams from various sources. It was originally designed as a Kodi addon service but has been adapted to run as a standalone server.

## Features

- **Trakt Integration**: Syncs your lists, collection, and watched history with Trakt.tv.
- **TMDb Integration**: Fetches metadata for movies and TV shows.
- **SIMKL Integration**: Full V2 support
- **Punchplay Integration**: Adheres to rate constraints
- **Scraping**: multi-threaded scraping framework to find media streams (Torrents, etc.).
- **Caching**: Uses local SQLite databases to cache metadata and reduce API calls.
- **API**: Provides a JSON HTTP API for client applications to interact with.

## Prerequisites

- Python 3.8+

## Installation

Download the zip file and extract locally

## Usage

Start the server:

Windows - double click start_server.bat
Linux - start_server.sh
Docker - Install docker image

The server will start on the configured port (default: 5555).

## API Endpoints

-   **GET /ping**: Check server status.
-   **GET /movie?tmdb_id=<id>**: Get movie details.
-   **GET /show?tmdb_id=<id>**: Get show details.
-   **GET /scrape?tmdb_id=<id>&item_type=<movie|episode>**: Scrape for streams.
-   **PUT /watched**: Mark an item as watched.
-   **GET /list?name=<list_name>**: Get items from a specific list.

## Architecture

- Local DB is source of truth, watched history/dropped shows are synced across all authorised services

## Data Storage

Orac Server uses several SQLite databases for caching:
-   `movies_static.db`: Static movie metadata.
-   `movies_dynamic.db`: Dynamic user state (watched status, ratings).
-   `tvshows_static.db`: Static TV show/episode metadata.
-   `tvshows_dynamic.db`: User state for TV shows.
-   `lists.db`: Caches Trakt lists.
-   `trakt_update_queue.db`: Queue for background Trakt sync operations.
