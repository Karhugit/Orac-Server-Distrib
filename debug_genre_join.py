
import sqlite3
import os

def check_genres():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dynamic_db = r"d:\Python Coding\Orac Server\orac_server\movies_dynamic_cache.db"
    static_db = r"d:\Python Coding\Orac Server\orac_server\movies_static_cache.db"

    if not os.path.exists(dynamic_db) or not os.path.exists(static_db):
        print(f"Error: Databases not found.\nDynamic: {dynamic_db}\nStatic: {static_db}")
        return

    try:
        conn = sqlite3.connect(dynamic_db)
        cursor = conn.cursor()
        
        # Attach static DB
        cursor.execute(f"ATTACH DATABASE '{static_db}' AS static_db")
        
        # Diagnostic counts
        print(f"Diagnostics:")
        
        cursor.execute("SELECT COUNT(*) FROM movie_status")
        total_dynamic = cursor.fetchone()[0]
        print(f"Total rows in movie_status: {total_dynamic}")

        print("\nFirst 10 Watched IDs in movie_status (dynamic):")
        cursor.execute("SELECT tmdb_id, trakt_id FROM movie_status WHERE watched_status > 0 LIMIT 10")
        rows = cursor.fetchall()
        for r in rows:
            print(f"TMDB: {r[0]} | Trakt: {r[1]}")
        
        print("\nFirst 10 IDs in movies (static):")
        cursor.execute("SELECT tmdb_id, trakt_id, title FROM static_db.movies LIMIT 10")
        rows = cursor.fetchall()
        for r in rows:
            print(f"TMDB: {r[0]} | Trakt: {r[1]} | Title: {r[2]}")
            
        print("-" * 70)

        print(f"{'TMDB ID':<10} | {'Title':<40} | {'Genre'}")
        print("-" * 70)
        
        # Try casting to ensure match
        query = """
            SELECT ms.tmdb_id, m.title, mg.genre
            FROM movie_status ms
            JOIN static_db.movies m ON CAST(ms.tmdb_id AS TEXT) = CAST(m.tmdb_id AS TEXT)
            LEFT JOIN static_db.movie_genres mg ON CAST(ms.tmdb_id AS TEXT) = CAST(mg.tmdb_id AS TEXT)
            WHERE ms.watched_status > 0
            ORDER BY ms.tmdb_id
        """
        
        cursor.execute(query)
        rows = cursor.fetchall()
        
        current_id = None
        genres = []
        title = ""
        
        # Group by ID for cleaner output
        grouped = {}
        for r_id, r_title, r_genre in rows:
            if r_id not in grouped:
                grouped[r_id] = {'title': r_title, 'genres': []}
            if r_genre:
                grouped[r_id]['genres'].append(r_genre)
                
        for tmdb_id, data in grouped.items():
            genre_str = ", ".join(data['genres']) if data['genres'] else "NO GENRES FOUND"
            print(f"{tmdb_id:<10} | {data['title'][:38]:<40} | {genre_str}")
            
    except Exception as e:
        print(f"Error executing query: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    check_genres()
