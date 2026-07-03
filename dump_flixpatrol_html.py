import requests
import os

def dump_html():
    url = "https://flixpatrol.com/"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        print(f"Fetching {url}...")
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        output_path = os.path.join(os.getcwd(), "flixpatrol_dump.html")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(response.text)
            
        print(f"Successfully dumped HTML to {output_path}")
        print(f"Content length: {len(response.text)} bytes")
        
    except Exception as e:
        print(f"Error fetching HTML: {e}")

if __name__ == "__main__":
    dump_html()
