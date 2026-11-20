"""Episode-specific resume thumbnails; never copy a parent poster as a still."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import json
import re
import time
import urllib.request
import urllib.parse
from personal_cinema.runtime_paths import RuntimePath as Path
from PIL import Image
from chinese_credits import Document
from metadata_matcher import read_json, write_json
from season_artwork import api, upload_image
from series_view import episode_source, own_episode_image

def episode_still(content, series, season, number):
    target = f'/tv/{re.escape(str(series))}(?:-[^/]+)?/season/{season}/episode/{number}'
    doc = Document(content)
    for card in doc.root.find(cls='card'):
        if not re.fullmatch(target, urllib.parse.urlsplit(card.attrs.get('data-url', '')).path):
            continue
        for img in card.find('img'):
            url = img.attrs.get('src', '')
            if 'backdrop' in img.attrs.get('class', '') and re.match('https://(?:media\\.themoviedb\\.org|image\\.tmdb\\.org)/t/p/', url):
                return re.sub('/t/p/[^/]+/', '/t/p/original/', url)

def refresh_resume_artwork(root, client, limit=20):
    overrides = read_json(root / 'data/episode-artwork-sources.json', {})
    state = read_json(root / 'data/episode-artwork-state.json', {})
    folder = root / 'data/episode-thumbnails'
    folder.mkdir(exist_ok=True)
    pages = {}
    seen = set()
    for user in api(client, '/Users'):
        if user.get('Policy', {}).get('IsDisabled'):
            continue
        items = api(client, '/UserItems/Resume?userId=' + user['Id'] + '&Limit=' + str(limit) + '&Fields=Path')['Items']
        for brief in items:
            if brief.get('Type') != 'Episode' or brief['Id'] in seen:
                continue
            seen.add(brief['Id'])
            item = api(client, '/Items/' + brief['Id'])
            if item.get('ImageTags', {}).get('Thumb'):
                continue
            path = folder / (item['Id'] + '.jpg')
            try:
                if item.get('ImageTags', {}).get('Primary'):
                    request = urllib.request.Request(_cinema_endpoint('jellyfin') + '/Items/' + item['Id'] + '/Images/Primary', headers={'Authorization': 'MediaBrowser Token="' + client.token + '"'})
                    with client.http.open(request, timeout=20) as response:
                        data = response.read(12 * 1024 * 1024)
                    path.write_bytes(data)
                    source = 'existing-episode-primary'
                else:
                    parent = api(client, '/Items/' + item['SeriesId'])
                    own = own_episode_image(root, parent, item)
                    if own and own.is_file():
                        path.write_bytes(own.read_bytes())
                        upload_image(client, item['Id'], path, 'Primary')
                        upload_image(client, item['Id'], path, 'Thumb')
                        state[item['Id']] = {'status': 'applied', 'source': 'verified-own-special-content', 'updated_at': time.time()}
                        write_json(root / 'data/episode-artwork-state.json', state)
                        continue
                    match = overrides.get(item['SeriesId']) or overrides.get(parent.get('Name', ''))
                    tmdb, season, number = episode_source(root, parent, item)
                    if match:
                        tmdb, season = (str(match['tmdb']), match['season'])
                    if not tmdb or season is None:
                        continue
                    key = (tmdb, season)
                    if key not in pages:
                        with urllib.request.urlopen(f'https://www.themoviedb.org/tv/{tmdb}/season/{season}?language=zh-CN', timeout=25) as response:
                            pages[key] = response.read(5 * 1024 * 1024).decode('utf-8')
                    source = episode_still(pages[key], tmdb, season, number)
                    if not source:
                        continue
                    with urllib.request.urlopen(source, timeout=25) as response:
                        path.write_bytes(response.read(12 * 1024 * 1024))
                    with Image.open(path) as image:
                        if image.width / image.height < 1.3:
                            raise ValueError('Refuse a portrait poster as an episode still')
                    upload_image(client, item['Id'], path, 'Primary')
                with Image.open(path) as image:
                    if image.width / image.height < 1.3:
                        raise ValueError('Refuse parent-like portrait art for resume')
                upload_image(client, item['Id'], path, 'Thumb')
                state[item['Id']] = {'status': 'applied', 'source': source, 'season': item.get('ParentIndexNumber'), 'episode': item.get('IndexNumber'), 'updated_at': time.time()}
            except (OSError, ValueError, KeyError) as error:
                state[item['Id']] = {'status': 'needs-review', 'error_type': type(error).__name__, 'updated_at': time.time()}
            write_json(root / 'data/episode-artwork-state.json', state)
    return state

def refresh_new_episode_artwork(root, client, limit=8, target_ids=None):
    """Enrich new catalog episodes even before their first playback."""
    state = read_json(root / 'data/episode-artwork-state.json', {})
    folder = root / 'data/episode-thumbnails'
    folder.mkdir(exist_ok=True)
    if target_ids is not None and (not target_ids):
        return state
    page_cache = root / 'data/episode-source-pages'
    page_cache.mkdir(exist_ok=True)
    done = 0
    parents = {p['Id']: p for p in api(client, '/Items?Recursive=true&IncludeItemTypes=Series&Fields=ProviderIds')['Items']}
    for item in api(client, '/Items?Recursive=true&IncludeItemTypes=Episode&Fields=Path')['Items']:
        if target_ids is not None and item['Id'] not in target_ids:
            continue
        if item.get('ImageTags', {}).get('Primary') and item.get('ImageTags', {}).get('Thumb'):
            continue
        prior = state.get(item['Id'], {})
        if prior.get('next_attempt_at', 0) > time.time():
            continue
        parent = parents.get(item.get('SeriesId'), {})
        tmdb, s, n = episode_source(root, parent, item)
        own = own_episode_image(root, parent, item)
        if (not tmdb or s is None or s == 99 or (not n)) and (not (own and own.is_file())):
            continue
        try:
            path = folder / (item['Id'] + '.jpg')
            source = None
            if own and own.is_file():
                path.write_bytes(own.read_bytes())
                source = 'verified-own-special-content'
                upload_image(client, item['Id'], path, 'Primary')
            elif item.get('ImageTags', {}).get('Primary'):
                path.write_bytes(client.image('/Items/' + item['Id'] + '/Images/Primary'))
                with Image.open(path) as im:
                    if im.width / im.height >= 1.3:
                        source = 'existing-episode-primary'
            if source is None:
                page = page_cache / (tmdb + '-' + str(s) + '.html')
                if not page.exists() or time.time() - page.stat().st_mtime > 86400:
                    with urllib.request.urlopen(f'https://www.themoviedb.org/tv/{tmdb}/season/{s}?language=zh-CN', timeout=20) as r:
                        page.write_bytes(r.read(6000000))
                source = episode_still(page.read_text('utf-8'), tmdb, s, n)
                if not source:
                    raise ValueError('No verified episode still')
                with urllib.request.urlopen(source, timeout=20) as r:
                    path.write_bytes(r.read(12000000))
                with Image.open(path) as im:
                    assert im.width / im.height >= 1.3
                upload_image(client, item['Id'], path, 'Primary')
            upload_image(client, item['Id'], path, 'Thumb')
            state[item['Id']] = {'status': 'applied', 'source': source, 'season': s, 'episode': n, 'updated_at': time.time()}
        except Exception as error:
            state[item['Id']] = {**prior, 'status': 'needs-review', 'error_type': type(error).__name__, 'next_attempt_at': time.time() + 86400}
        write_json(root / 'data/episode-artwork-state.json', state)
        done += 1
        if done >= limit:
            break
    return state
