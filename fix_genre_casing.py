
import sqlite3

def fix_genre_casing():
    db_path = r"d:\Python Coding\Orac Server\orac_server\movies_static_cache.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("Scanning for non-slug genres (e.g. 'Science Fiction', 'Action')...")
    
    # Get all genres
    cursor.execute("SELECT name FROM genres")
    all_genres = [r[0] for r in cursor.fetchall()]
    
    for genre in all_genres:
        slug_cased = genre.lower().replace(" ", "-")
        
        if genre != slug_cased:
            print(f"Fixing '{genre}' -> '{slug_cased}'")
            
            # 1. Ensure Slug Case genre exists
            cursor.execute("INSERT OR IGNORE INTO genres(name) VALUES(?)", (slug_cased,))
            
            # 2. Update movie_genres to point to the Slug Case genre
            # This might fail if the mapping already exists (UNIQUE constraint), so we handle that by ignoring
            cursor.execute("""
                UPDATE OR IGNORE movie_genres 
                SET genre = ? 
                WHERE genre = ?
            """, (slug_cased, genre))
            
            # 3. Delete any remaining mappings to the old non-slug genre 
            # (these are the ones that failed update because the movie already had the Slug genre linked)
            cursor.execute("DELETE FROM movie_genres WHERE genre = ?", (genre,))
            
            # 4. Delete the old non-slug genre from genres table
            cursor.execute("DELETE FROM genres WHERE name = ?", (genre,))
            
    conn.commit()
    print("Genre casing fixed to slug format.")
    
    # Diagnostics
    cursor.execute("SELECT name FROM genres ORDER BY name")
    print("\nRemaining Genres:")
    for row in cursor.fetchall():
        print(row[0])

    conn.close()

if __name__ == "__main__":
    fix_genre_casing()
