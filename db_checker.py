import sqlite3

conn = sqlite3.connect('config_db.db')
cur = conn.cursor()
try:
    cur.execute("SELECT * FROM config WHERE key LIKE 'simkl%'")
    print("CONFIG DB:")
    for row in cur.fetchall():
        print(row)
except Exception as e:
    print(f"Error querying config_db: {e}")
    
print("==============")

conn2 = sqlite3.connect('lists_cache.db')
cur2 = conn2.cursor()
try:
    cur2.execute("SELECT * FROM list_items WHERE list_id = 'simkl:watchlist'")
    print("LISTS DB - ITEMS:")
    for row in cur2.fetchall():
        print(row)
except Exception as e:
    print(f"Error querying lists_cache: {e}")
