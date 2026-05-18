import sqlite3
import json
from resources.lib.log_utils import log, LOGERROR, LOGDEBUG, LOGINFO
from time import time

def get_next_episodes(tvshows_dynamic_db_path, tvshows_static_db_path, user=None):
    """
    Returns enriched next episode data (including show and episode info)
    for a given user, excluding dropped shows.
    This version calculates the next episode on the fly.
    """


    try:
        starttime = time()
        with sqlite3.connect(tvshows_dynamic_db_path) as dynamic_conn, \
             sqlite3.connect(tvshows_static_db_path) as static_conn:

            # Attach the dynamic DB to the static connection for JOINs
            static_conn.execute(f"ATTACH DATABASE ? AS dynamic_db", (tvshows_dynamic_db_path,))
            cursor = static_conn.cursor()

            # This query calculates the next episode to watch on the fly.
            # It prioritizes partially watched episodes, then the next unwatched episode after the last watched one.
            query = """
                WITH AllEpisodes AS (
                    SELECT
                        show_id,
                        season,
                        episode_number,
                        tmdb_id,
                        ROW_NUMBER() OVER(PARTITION BY show_id ORDER BY season, episode_number) as rn
                    FROM episodes
                    WHERE DATE(air_date) <= DATE('now')
                ),
                UserWatchedEpisodes AS (
                    SELECT
                        ae.show_id,
                        ae.season,
                        ae.episode_number,
                        ae.tmdb_id,
                        ae.rn,
                        we.percent_watched,
                        we.watched_at,
                        we.watched_status
                    FROM AllEpisodes ae
                    JOIN dynamic_db.watched_episodes we ON ae.tmdb_id = we.tmdb_id
                    WHERE we.user = ? COLLATE NOCASE
                ),
                PartiallyWatched AS (
                    SELECT
                        show_id,
                        tmdb_id,
                        watched_at
                    FROM UserWatchedEpisodes
                    WHERE (percent_watched > 0 AND percent_watched < 80) OR watched_status = 1
                ),
                LastFullyWatched AS (
                    SELECT
                        show_id,
                        MAX(rn) as last_watched_rn
                    FROM UserWatchedEpisodes
                    WHERE percent_watched >= 80 OR watched_status = 2
                    GROUP BY show_id
                ),
                NextEpisodeCandidates AS (
                    SELECT show_id, tmdb_id, watched_at FROM PartiallyWatched
                    UNION
                    SELECT
                        lfw.show_id,
                        ae.tmdb_id,
                        NULL as watched_at
                    FROM LastFullyWatched lfw
                    JOIN AllEpisodes ae ON lfw.show_id = ae.show_id AND ae.rn = lfw.last_watched_rn + 1
                    WHERE lfw.show_id NOT IN (SELECT show_id FROM PartiallyWatched)
                )
                SELECT
                    e.episode_trakt_id,
                    s.title,
                    s.year AS show_year,
                    s.overview AS show_overview,
                    s.show_trakt_id,
                    s.show_tmdb_id,
                    s.network,
                    s.imdb_id AS show_imdb_id,
                    s.poster_path AS show_poster_path,
                    s.fanart_path AS show_fanart_path,
                    s.thumbnail_path AS show_thumbnail_path,
                    s.clearlogo_path AS show_clearlogo_path,
                    s.landscape_path AS show_landscape_path,
                    e.season,
                    e.episode_number,
                    e.episode_title,
                    COALESCE(NULLIF(e.episode_overview, ''), s.overview) AS episode_overview,
                    e.tmdb_id,
                    e.imdb_id,
                    e.tvdb_id,
                    e.air_date,
                    e.runtime,
                    e.episode_type,
                    e.original_title,
                    e.rating AS episode_rating,
                    e.episode_poster_path,
                    e.episode_fanart_path,
                    e.episode_clearlogo_path,
                    e.episode_landscape_path,
                    e.episode_thumbnail_path,
                    COALESCE(nec.watched_at, we.watched_at) AS last_watched_at,
                    COALESCE(we.percent_watched, 0) AS percent_watched
                FROM NextEpisodeCandidates nec
                JOIN episodes AS e ON nec.tmdb_id = e.tmdb_id
                JOIN shows AS s ON nec.show_id = s.show_tmdb_id
                LEFT JOIN dynamic_db.watched_episodes AS we ON e.tmdb_id = we.tmdb_id AND we.user = ?
                WHERE (s.dropped IS NULL OR s.dropped = 0)
                ORDER BY last_watched_at DESC;
            """

            cursor.execute(query, (user,user,))
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            results = [dict(zip(columns, row)) for row in rows]
            log(f"[Orac] Retrieved {len(results)} next episodes for user '{user}' in {time() - starttime:.2f} seconds", level=LOGDEBUG)

            return results

    except Exception as e:
        log(f"[Orac] Error retrieving next episodes: {e}", level=LOGERROR)
        return []
