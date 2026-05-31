# Orac Server — Changelog

All notable changes are documented here.
The distrib repo is tagged with `vX.Y.Z` for each release so the
in-server update checker can compare against the running version.

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
