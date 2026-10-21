import base64
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'data' / 'catalog.json'
UA = 'LeBullaApp/2.0 (+https://github.com/uptobe8/LeBulla)'

def request(url, token=None, method='GET', form=None, basic=None):
    headers = {'Accept': 'application/json, application/atom+xml, application/xml;q=0.9', 'User-Agent': UA}
    if token:
        headers['Authorization'] = 'OAuth ' + token
    if basic:
        headers['Authorization'] = 'Basic ' + base64.b64encode(':'.join(basic).encode()).decode()
    data = urllib.parse.urlencode(form).encode() if form else None
    if data:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read()

def get_json(url, token=None):
    return json.loads(request(url, token=token))

def duration(seconds):
    seconds = max(0, int(seconds or 0))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f'{h}:{m:02}:{s:02}' if h else f'{m}:{s:02}'

def safe_link(url, domain):
    parsed = urllib.parse.urlparse(str(url or ''))
    return parsed.scheme == 'https' and parsed.hostname in {domain, 'www.' + domain}

def latest_mixcloud():
    endpoint = 'https://api.mixcloud.com/LeBulla/cloudcasts/?limit=100'
    collected = []
    for _ in range(3):
        payload = get_json(endpoint)
        for item in payload.get('data', []):
            key = item.get('key', '')
            if not re.match(r'^/LeBulla/[^/]+/$', key, re.I):
                continue
            url = 'https://www.mixcloud.com' + key
            title = str(item.get('name') or 'Sesión Le Bulla').strip()[:150]
            tags = item.get('tags') or []
            genre = ' · '.join((t.get('name') or '').strip().upper() for t in tags[:3] if isinstance(t, dict)) or 'SESIÓN ORIGINAL'
            collected.append({'id': 'mc-' + key.strip('/').replace('/', '-'), 'title': title, 'duration': duration(item.get('audio_length', 0)), 'genre': genre, 'url': url, 'artwork': (item.get('pictures') or {}).get('large') or '', 'published': item.get('created_time') or ''})
        next_url = (payload.get('paging') or {}).get('next')
        if not next_url or not safe_link(next_url, 'api.mixcloud.com'):
            break
        endpoint = next_url
    if not collected:
        raise ValueError('Mixcloud devolvió 0 sesiones válidas')
    return unique(collected)

def latest_youtube():
    ids = [v.strip() for v in os.environ.get('YOUTUBE_CHANNEL_IDS', '').split(',') if v.strip()]
    found = []
    for channel_id in ids:
        if not re.fullmatch(r'UC[a-zA-Z0-9_-]{22}', channel_id):
            print('YouTube channel ID no válido, omitido', file=sys.stderr)
            continue
        url = 'https://www.youtube.com/feeds/videos.xml?channel_id=' + channel_id
        data = request(url)
        tree = ET.fromstring(data)
        ns = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}
        for entry in tree.findall('a:entry', ns):
            vid = entry.findtext('yt:videoId', default='', namespaces=ns)
            if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
                continue
            title = (entry.findtext('a:title', default='', namespaces=ns) or 'Le Bulla — Directo')[:150]
            found.append({'id': 'yt-' + vid, 'title': title, 'duration': 'VÍDEO', 'genre': 'YOUTUBE · CANAL VERIFICADO', 'youtubeId': vid, 'url': 'https://www.youtube.com/watch?v=' + vid, 'artwork': 'https://img.youtube.com/vi/' + vid + '/hqdefault.jpg', 'published': entry.findtext('a:published', default='', namespaces=ns)})
    return unique(found)

def latest_soundcloud():
    cid = os.environ.get('SOUNDCLOUD_CLIENT_ID', '').strip()
    secret = os.environ.get('SOUNDCLOUD_CLIENT_SECRET', '').strip()
    if not cid or not secret:
        return None
    body = request('https://secure.soundcloud.com/oauth/token', method='POST', basic=(cid, secret), form={'grant_type': 'client_credentials'})
    token = json.loads(body)['access_token']
    profile = get_json('https://api.soundcloud.com/resolve?' + urllib.parse.urlencode({'url': 'https://soundcloud.com/lebulla'}), token=token)
    urn = profile.get('urn') or profile.get('id')
    if not urn:
        raise ValueError('SoundCloud no devolvió identificador de usuario')
    endpoint = 'https://api.soundcloud.com/users/' + urllib.parse.quote(str(urn), safe=':') + '/tracks?linked_partitioning=true&limit=100'
    found = []
    for _ in range(3):
        payload = get_json(endpoint, token=token)
        items = payload.get('collection', []) if isinstance(payload, dict) else payload
        for track in items:
            url = track.get('permalink_url', '')
            if not safe_link(url, 'soundcloud.com') or track.get('sharing') == 'private':
                continue
            found.append({'id': 'sc-' + str(track.get('id') or track.get('urn') or len(found)), 'title': str(track.get('title') or 'Le Bulla — Track')[:150], 'duration': duration((track.get('duration') or 0) / 1000), 'genre': str(track.get('genre') or 'SOUNDCLOUD · AUDIO')[:95], 'url': url, 'artwork': track.get('artwork_url') or '', 'published': track.get('created_at') or ''})
        next_url = payload.get('next_href') if isinstance(payload, dict) else None
        if not next_url or not safe_link(next_url, 'api.soundcloud.com'):
            break
        endpoint = next_url
    return unique(found)

def unique(items):
    seen, result = set(), []
    for item in items:
        if item['url'] not in seen:
            result.append(item)
            seen.add(item['url'])
    return result

def combine_new(old, newer, limit=100):
    return unique(newer + old)[:limit]

def main():
    current = json.loads(OUTPUT.read_text(encoding='utf-8'))
    catalog = {key: current.get(key, []) for key in ('mixcloud', 'youtube', 'soundcloud')}
    changed = []
    for source, getter in [('mixcloud', latest_mixcloud), ('youtube', latest_youtube), ('soundcloud', latest_soundcloud)]:
        try:
            recent = getter()
            if recent is None or (source == 'youtube' and not os.environ.get('YOUTUBE_CHANNEL_IDS', '').strip()):
                print(source + ': sin credenciales o identificadores; manteniendo catálogo actual')
                continue
            if not recent:
                print(source + ': sin entradas nuevas; manteniendo catálogo actual')
                continue
            catalog[source] = combine_new(catalog[source], recent)
            if catalog[source] != current.get(source, []):
                changed.append(source)
            print(source + ': ' + str(len(recent)) + ' elementos recibidos')
        except Exception as exc:
            print(source + ': no se actualiza por error de origen ' + str(exc), file=sys.stderr)
    if changed:
        catalog['lastUpdated'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        OUTPUT.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('Actualizadas: ' + ', '.join(changed))
    else:
        print('Sin cambios; archivo intacto')

if __name__ == '__main__':
    main()
