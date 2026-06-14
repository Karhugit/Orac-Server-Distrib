# Orac Server — Changelog

All notable changes are documented here.
The distrib repo is tagged with `vX.Y.Z` for each release so the
in-server update checker can compare against the running version.

---

## [1.2.6] — 2026-06-14

### Added
- **Higher-resolution images** — upgraded all TMDb image size parameters: posters `w500→w780`, fanart/landscape `w780→w1280`, thumbnails/episode stills `w300→w780`, clear logos `w300→w500`.
- **Image resolution migration script** — added `migrate_image_resolution.py` to upgrade all existing stored image URLs in the database without requiring a full resync.
- **AIOStreams diagnostic logging** — added detailed INFO-level logging across the full settings pipeline (Liberator send → Orac receive/store → scraper credential load → header source) with masked passwords.

### Fixed
- **FlixPatrol title resolution** — extract year from URL slug (e.g. `/title/the-babysitter-1995/`) and pass it to TMDb search, fixing wrong-version matches like The Babysitter (2017 vs 1995).
- **FlixPatrol ranking order** — preserve scraped rank order for FlixPatrol/web/mdblist lists instead of resorting by release date. Fixed an int/string type mismatch in the `id_to_index` lookup that was silently defeating the sort.

---

## [1.2.5] — 2026-06-07

### Added
- **Torz Scraper** — registered a new scraper module wrapping StremThru aggregator providers with parallel querying and hash deduplication.
- **Library List Items Detail Modal** — implemented a landscape-optimized glassmorphic grid overlay to show all items (movies and TV shows) inside any selected library list.
- **Details & Reviews View** — added a detail overlay panel displaying poster, rating, type, overview, and user reviews from TMDb (with translation formatting for Kodi BBCode tags).
- **Manual Scrape Action** — added a scrape button inside the list detail view cards to run scraper queries manually and inspect parsed results.

### Fixed
- **Thread pool worker starvation** — fixed thread allocation limits to guarantee dynamically registered scrapers receive a full thread pool.
- **TMDb TV show fallback matching** — resolved a mapping error where show IMDb IDs were matched with episode IMDb IDs when enriching show cache fallbacks.
- **Simkl watchlist sync crash** — fixed a KeyError when synchronizing lists lacking titles/years by enriching them from the TMDb API.

---

## [1.2.4] — 2026-06-04

### Fixed
- **TMDB list v3 updates** — added the `"media_type"` parameter inside the request JSON payload body for custom list additions and removals, resolving the 403 Forbidden client error when adding or removing TV shows.

---

## [1.2.3] — 2026-06-02

### Added
- **Dynamic Scraper Modes** — implemented backend support for standalone AIOStreams mode, hybrid mode, and standard Orac scraper mode based on client setting preferences.
- **Provider Partitioning Filtering** — added query parameter extraction and dynamic filtering for primary/background providers during scrape requests.

---

## [1.2.2] — 2026-06-02

### Fixed
- **TMDB changes daily safety limit** — expanded the TMDB changes daily sync safety limit from 10 to 50 pages, allowing retrieval of up to 5,000 show updates per cycle so that no edits are lost on busy days where changes exceed 1,000.
- **ID Deduplication** — optimized the TMDB TV changes sync engine to deduplicate incoming show IDs using a set, avoiding redundant thread spawning and database writes.

---

## [1.2.1] — 2026-06-02

### Added
- **External Lists Watched Status Enrichment** — implemented automatic local database lookup and status injection (watched ticks and resume percentage points) for movies and TV shows fetched from external lists (TMDB, Trakt, Simkl, MDblist) that are not imported into the library.

---

## [1.2.0] — 2026-06-01

### Added
- **`/api/status` endpoint** — returns central authorization status and access tokens (Trakt, Simkl, TMDb, MDbList) to seamlessly synchronize credentials across multiple active Liberator addon instances.

### Fixed
- **TMDB custom list pagination** — implemented a pagination loop to aggregate all items across multiple pages (bypassing the 20-item v3 details API limit) when syncing user-created lists.

---

## [1.1.0] — 2026-05-29

### Added
- **Versioning system** — `version.py` is now the single source of truth for the
  server version number (`1.1.0`).
- **Automatic update notifications** — the server checks GitHub Releases once at
  startup and then every 24 hours. When a newer release is available:
  - An amber banner appears in the web dashboard linking to the release notes.
  - A `[WARNING]` log line is written at startup and on each daily check.
- **`/api/version` endpoint** — returns current version, latest version, whether
  an update is available, the release URL, and when the check last ran.
- **Dynamic TMDB watch providers** — new `providers_handler.py` syncs and serves
  the TMDB watch-provider catalogue; exposed via the `/providers` endpoint.
- **Stale episode metadata refresh** — new `stale_episode_refresh.py` background
  worker refreshes episode metadata that has gone stale (24-hour cycle).
- **`docker-entrypoint.sh`** — improved Docker entry-point script.

### Changed
- **Trakt watched history sync now paginated** — `sync_engine.py` loops through
  pages of `/sync/watched/movies` and `/sync/watched/shows` using a 250-item page
  size, ready for Trakt's June 2026 hard limit.
- **`last_activities` change detection** — `sync_providers()` now calls
  `/sync/last_activities` before fetching watched history. If neither movies nor
  episodes have changed since the last sync the paginated fetches are skipped
  entirely, saving significant API quota on idle hourly cycles.
- **Watchlist, favorites, and collection syncs paginated** — `trakt_utils.py` and
  `sync_trakt_with_db.py` now loop through pages for `/users/me/watchlist`,
  `/users/me/favorites`, `/users/me/collection/movies`, and
  `/users/me/collection/shows`.
- **Custom list page size** — `_fetch_all_list_items` default reduced from 1000 to
  250 to comply with the new Trakt limit (the pagination loop was already correct).
- **MDBList sync** — major rewrite of `mdblist_list_sync.py` with reliability fixes.

### Fixed
- **AIOStreams scraper** — switched from HTTP basic auth to the required
  `x-aiostreams-user-data` header; handles `{"data": null}` responses gracefully
  instead of raising an exception.

---

## [1.0.0] — 2026-05-01

- Initial public release.
