import asyncio
import sqlite3
import json
import urllib.request
import urllib.error
from urllib.parse import urlencode

def run_tests():
    url = 'http://127.0.0.1:5555/collections/movies'
    print(f"Testing {url}...")
    
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as response:
            status = response.getcode()
            body = response.read().decode('utf-8')
            
            print(f"Status Code: {status}")
            assert status == 200, f"Expected status 200, got {status}"
            
            data = json.loads(body)
            assert data.get('success') is True, f"Expected success to be True, got {data.get('success')}"
            
            collections = data.get('collections', [])
            print(f"Found {len(collections)} collections.")
            
            if len(collections) > 0:
                first_coll = collections[0]
                assert 'id' in first_coll, "Missing id in collection"
                assert 'name' in first_coll, "Missing name in collection"
                assert 'movies' in first_coll, "Missing movies in collection"
                print(f"Sample collection: {first_coll['name']} with {len(first_coll['movies'])} movies.")
                
            print("Test passed successfully.")
    except urllib.error.URLError as e:
        print(f"Test failed: Unable to connect to server. Ensure Orac server is running. {e}")
    except AssertionError as e:
        print(f"Test failed: {e}")
    except Exception as e:
        print(f"Test failed with error: {e}")

if __name__ == "__main__":
    run_tests()
