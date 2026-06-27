import sys
import requests
import json

def format_val_with_source(val):
    if not val:
        return "None"
    
    # Determine source
    path_str = str(val).lower()
    if "fanart.tv" in path_str:
        source = "Fanart"
    elif "assets/images" in path_str or "show_" in path_str or "movie_" in path_str:
        source = "Local"
    elif "tmdb.org" in path_str or "/t/p/" in path_str:
        source = "TMDb"
    else:
        source = "Unknown"
        
    # Format and truncate path
    val_str = str(val)
    if val_str.startswith("http://") or val_str.startswith("https://"):
        parts = val_str.split("/")
        if len(parts) > 4:
            val_str = ".../" + "/".join(parts[-2:])
    elif len(val_str) > 30:
        val_str = val_str[:27] + "..."
        
    return f"{val_str} ({source})"

def print_table(headers, rows):
    # Format all cells
    formatted_rows = []
    for row in rows:
        formatted_row = []
        for idx, val in enumerate(row):
            # First 3 columns (Title, Year, Season/Ep) are printed as-is, rest are images
            if idx < 3 and len(headers) == 6 or idx < 2 and len(headers) == 5:
                val_str = str(val or "")
                if len(val_str) > 35:
                    val_str = val_str[:32] + "..."
                formatted_row.append(val_str)
            else:
                formatted_row.append(format_val_with_source(val))
        formatted_rows.append(formatted_row)

    # Calculate column widths
    widths = [len(h) for h in headers]
    for row in formatted_rows:
        for idx, val in enumerate(row):
            widths[idx] = max(widths[idx], len(val))
            
    # Print header
    header_line = " | ".join(f"{h:<{widths[idx]}}" for idx, h in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))
    
    # Print rows
    for row in formatted_rows:
        print(" | ".join(f"{val:<{widths[idx]}}" for idx, val in enumerate(row)))

def main():
    # Detect port from config or default to 5555
    port = 5555
    try:
        with open("config.json", "r") as f:
            cfg = json.load(f)
            port = cfg.get("SERVER", {}).get("port", 5555)
    except:
        pass

    base_url = f"http://localhost:{port}"
    print(f"Connecting to Orac Server at {base_url}...\n")
    
    # 1. Fetch Next Episodes
    print("Fetching Next Episodes...")
    try:
        resp = requests.get(f"{base_url}/next_episodes?user=newtzen", timeout=10)
        resp.raise_for_status()
        next_eps = resp.json()
    except Exception as e:
        print(f"Error fetching Next Episodes: {e}")
        next_eps = []
        
    # 2. Fetch Liked List 'latest-releases'
    print("Fetching Trakt Liked List 'latest-releases'...")
    try:
        resp = requests.get(f"{base_url}/list?name=latest-releases&item_type=movie&user=giladg", timeout=10)
        resp.raise_for_status()
        latest_releases = resp.json()
    except Exception as e:
        print(f"Error fetching latest-releases: {e}")
        latest_releases = []
        
    # 3. Fetch Internal Index 'Watchlists'
    print("Fetching Internal Index 'Watchlists'...")
    try:
        resp = requests.get(f"{base_url}/internal_index_contents?index_id=Watchlists&item_type=tvshow&user=newtzen", timeout=10)
        resp.raise_for_status()
        watchlists_data = resp.json()
        watchlists = watchlists_data.get("results", [])
    except Exception as e:
        print(f"Error fetching Watchlists index: {e}")
        watchlists = []
        
    # Print Next Episodes table
    print("\n=== NEXT EPISODES ===")
    if next_eps:
        headers = ["Show Title", "Season/Ep", "Episode Title", "Poster Path (Source)", "Fanart Path (Source)", "Clearlogo Path (Source)"]
        rows = []
        for ep in next_eps[:10]:
            se_str = f"S{ep.get('season')}E{ep.get('episode_number')}"
            rows.append([
                ep.get("title", ""),
                se_str,
                ep.get("episode_title", ""),
                ep.get("show_poster_path") or ep.get("poster_path", ""),
                ep.get("show_fanart_path") or ep.get("fanart_path", ""),
                ep.get("show_clearlogo_path") or ep.get("clearlogo_path", "")
            ])
        print_table(headers, rows)
    else:
        print("No items found.")
        
    # Print Latest Releases table
    print("\n=== LATEST RELEASES (TRAKT LIKED LIST) ===")
    if latest_releases:
        headers = ["Title", "Year", "Poster Path (Source)", "Fanart Path (Source)", "Clearlogo Path (Source)"]
        rows = []
        for movie in latest_releases[:10]:
            rows.append([
                movie.get("title", ""),
                movie.get("year", ""),
                movie.get("poster_path", ""),
                movie.get("fanart_path", ""),
                movie.get("clearlogo_path", "")
            ])
        print_table(headers, rows)
    else:
        print("No items found.")
        
    # Print Watchlists table
    print("\n=== WATCHLISTS (INTERNAL INDEX) ===")
    if watchlists:
        headers = ["Show Title", "Year", "Poster Path (Source)", "Fanart Path (Source)", "Clearlogo Path (Source)"]
        rows = []
        for show in watchlists[:10]:
            rows.append([
                show.get("title", ""),
                show.get("year", ""),
                show.get("poster_path", ""),
                show.get("fanart_path", ""),
                show.get("clearlogo_path", "")
            ])
        print_table(headers, rows)
    else:
        print("No items found.")

if __name__ == "__main__":
    main()
