import sys

file_path = r'd:\Python Coding\Orac Server\orac_server\resources\lib\db_init.py'

with open(file_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# The file currently truncates abruptly around line 620 because of the bad replace_file_content.
# We will just rewrite the end of the file properly.

# Find the end of init_tags_db:
# We know it had:
#         cursor.execute("""
#             CREATE INDEX IF NOT EXISTS idx_tag_items_media ON tag_items(media_type, tmdb_id);
#         """)

keep_lines = []
found = False
for line in lines:
    keep_lines.append(line)
    if "CREATE INDEX IF NOT EXISTS idx_tag_items_media ON tag_items(media_type, tmdb_id);" in line:
        found = True
        break

if not found:
    print("Could not find anchor line.")
    sys.exit(1)

# Add the closing quote for the execute and the rest of the file
append_content = """        \")
        cursor.execute(\"\"\"
            CREATE INDEX IF NOT EXISTS idx_tag_items_trakt ON tag_items(trakt_id);
        \"\"\")

        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Failed to initialize tags database: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()

def init_undesirables_db(db_path=None, conn=None):
    \"\"\"Initialize undesirables database with defaults.\"\"\"
    close_conn = False
    if conn is None:
        conn = DatabaseManager().get_connection(db_path)
        close_conn = True
    try:
        cursor = conn.cursor()

        cursor.execute('''CREATE TABLE IF NOT EXISTS undesirables (
            keyword TEXT NOT NULL, 
            user_defined BOOL NOT NULL, 
            enabled BOOL NOT NULL, 
            UNIQUE(keyword)
        )''')

        # Insert defaults safely
        from resources.scrapers.modules.source_utils import UNDESIRABLES
        default_entries = [(keyword, False, True) for keyword in UNDESIRABLES]
        cursor.executemany('INSERT OR IGNORE INTO undesirables VALUES (?, ?, ?)', default_entries)

        conn.commit()
        return True
    except Exception as e:
        log(f"[Orac] Failed to initialize undesirables database: {e}", level=LOGERROR)
        return False
    finally:
        if close_conn and conn:
            conn.close()
"""

with open(file_path, 'w', encoding='utf-8') as f:
    f.writelines(keep_lines)
    f.write(append_content)

print("db_init fixed successfully!")
