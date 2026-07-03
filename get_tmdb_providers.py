import requests, json

api_key = '872d408b3d926ebb32f84f5167764cc3'
BASE = 'https://api.themoviedb.org/3'
REGION = 'AU'
result = {}

for media_type in ('movie', 'tv'):
    resp = requests.get(
        f'{BASE}/watch/providers/{media_type}',
        params={'api_key': api_key, 'language': 'en-US', 'watch_region': REGION},
        timeout=15
    )
    resp.raise_for_status()
    data = resp.json().get('results', [])
    data.sort(key=lambda p: p.get('display_priorities', {}).get(REGION, 9999))
    result[media_type] = data

# Deduplicate by provider_id
all_ids = {}
for mt in ('movie', 'tv'):
    for p in result[mt]:
        pid = p['provider_id']
        if pid not in all_ids:
            all_ids[pid] = {
                'id': pid,
                'name': p['provider_name'],
                'logo': p.get('logo_path', ''),
                'movie': False,
                'tv': False
            }
        all_ids[pid][mt] = True

print(f'Total unique providers in {REGION}: {len(all_ids)}')
print()
print(f"{'ID':>6}  {'Provider Name':<40}  {'Movie':^5}  {'TV':^3}")
print('-' * 60)
for pid, p in sorted(all_ids.items(), key=lambda x: x[1]['name'].lower()):
    mv = 'Y' if p['movie'] else ''
    tv = 'Y' if p['tv'] else ''
    print(f"{pid:>6}  {p['name']:<40}  {mv:^5}  {tv:^3}")

with open('tmdb_providers_au.json', 'w') as f:
    json.dump(all_ids, f, indent=2)
print('\nSaved to tmdb_providers_au.json')
