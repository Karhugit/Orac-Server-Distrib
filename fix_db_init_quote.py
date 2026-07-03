file_path = r'd:\Python Coding\Orac Server\orac_server\resources\lib\db_init.py'

with open(file_path, 'r', encoding='utf-8') as f:
    text = f.read()

# Replace the blank line bug with the proper closing quote
text = text.replace('CREATE INDEX IF NOT EXISTS idx_tag_items_media ON tag_items(media_type, tmdb_id);\n\n        cursor.execute', 'CREATE INDEX IF NOT EXISTS idx_tag_items_media ON tag_items(media_type, tmdb_id);\n        """)\n        cursor.execute')

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(text)
print("done")
