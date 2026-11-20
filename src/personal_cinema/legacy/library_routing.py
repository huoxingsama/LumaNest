"""Classify animated series while keeping video caches and stream IDs stable.

Only the cache worker moves catalog folders. The metadata worker writes small
requests; it never edits the cache worker's state. Journals retain user progress
until Jellyfin has indexed the new catalog paths.
"""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import json
import os
import time
import urllib.parse
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path

def load(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)

def catalog_relative(entry):
    return entry.get('catalog_relative', entry['relative'])

def is_animation_series(item):
    return item.get('Type') == 'Series' and any((str(g).casefold() in {'animation', 'anime', '动画', '動畫', '动漫', '動漫'} for g in item.get('Genres') or []))

def queue_animation(root, item, entries):
    if not is_animation_series(item):
        return
    try:
        relative = Path(item.get('Path') or '').absolute().relative_to((root / 'catalog').absolute()).as_posix()
        cache_path(root / 'catalog', relative)
    except ValueError:
        return
    if not relative.startswith('Shows/') or len(Path(relative).parts) != 2:
        return
    series = Path(relative).parts[1]
    if not any((e.get('kind') == 'Shows' and e.get('series') == series and catalog_relative(e).startswith(relative + '/') for e in entries.values())):
        return
    ident = hashlib.sha256(series.encode()).hexdigest()[:24]
    path = root / 'data/requests' / ('anime-' + ident + '.json')
    if not path.exists():
        save(path, {'action': 'route-anime', 'series': series})

def meaningful(data):
    return bool(data.get('Played') or data.get('IsFavorite') or data.get('PlayCount') or data.get('PlaybackPositionTicks') or (data.get('Likes') is not None) or (data.get('Rating') is not None))

def _series_paths(root, series):
    if not series or len(Path(series).parts) != 1 or series in ('.', '..'):
        raise ValueError('Expected one managed series folder name')
    return (cache_path(root / 'catalog', 'Shows/' + series), cache_path(root / 'catalog', 'Anime/' + series))

def route_to_anime(worker, series):
    source, target = _series_paths(worker.root, series)
    entries = [e for e in worker.entries.values() if e.get('kind') == 'Shows' and e.get('series') == series]
    if not entries:
        return True
    for entry in entries:
        if not entry['relative'].startswith('Shows/' + series + '/'):
            raise ValueError('Series path does not match its catalog folder')
    journal_path = worker.root / 'data/library-migrations' / (hashlib.sha256(series.encode()).hexdigest()[:24] + '.json')
    journal = load(journal_path, None)
    if source.exists():
        if target.exists():
            raise FileExistsError('Both catalog folders exist; manual inspection required')
        for node in source.rglob('*'):
            cache_path(worker.root / 'catalog', node.relative_to(worker.root / 'catalog').as_posix())
        query = urllib.parse.urlencode({'Recursive': 'true', 'IncludeItemTypes': 'Series,Season,Episode', 'Fields': 'Path'})
        items = worker.api('jellyfin', '/Items?' + query).get('Items', [])
        managed = {i['Id'] for i in items if i.get('Path') and Path(i['Path']).is_relative_to(source)}
        sessions = worker.api('jellyfin', '/Sessions')
        if any((s.get('NowPlayingItem', {}).get('Id') in managed for s in sessions)):
            return False
        if any((getattr(worker, 'active', {}).get(e['id']) for e in entries)):
            return False
        if journal is None:
            records = []
            for user in worker.api('jellyfin', '/Users'):
                user_query = query + '&UserId=' + urllib.parse.quote(user['Id']) + '&EnableUserData=true'
                for item in worker.api('jellyfin', '/Items?' + user_query).get('Items', []):
                    data = item.get('UserData') or {}
                    if item['Id'] in managed and meaningful(data):
                        new_path = target / Path(item['Path']).relative_to(source)
                        records.append({'user_id': user['Id'], 'old_id': item['Id'], 'new_path': str(new_path), 'data': data, 'restored': False})
            journal = {'series': series, 'phase': 'prepared', 'created_at': time.time(), 'records': records}
            save(journal_path, journal)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
    elif not target.exists() or journal is None:
        raise FileNotFoundError('No recoverable catalog migration')
    with worker.lock:
        worker.routes[series] = 'Anime'
        for entry in entries:
            entry['catalog_relative'] = 'Anime/' + entry['relative'][len('Shows/'):]
        save(worker.root / 'data/library-routing.json', worker.routes)
        worker.save()
    journal['phase'] = 'moved'
    save(journal_path, journal)
    worker.api('jellyfin', '/Library/Refresh', {}, method='POST')
    return True

def restore_progress(worker):
    for path in (worker.root / 'data/library-migrations').glob('*.json'):
        journal = load(path, {})
        if journal.get('phase') == 'prepared':
            route_to_anime(worker, journal['series'])
            journal = load(path, {})
        if journal.get('phase') != 'moved':
            continue
        records = journal['records']
        by_user = {}
        for record in records:
            if record.get('restored') or record.get('waiting_replacement'):
                continue
            target_entry = worker.entries.get(record.get('replacement_entry_id'))
            if target_entry:
                record['new_path'] = str((worker.root / 'catalog' / catalog_relative(target_entry)).with_suffix('.strm'))
            uid = record['user_id']
            if uid not in by_user:
                query = urllib.parse.urlencode({'Recursive': 'true', 'UserId': uid, 'IncludeItemTypes': 'Movie,Series,Season,Episode', 'Fields': 'Path', 'EnableUserData': 'true'})
                by_user[uid] = {os.path.normcase(i.get('Path', '')): i for i in worker.api('jellyfin', '/Items?' + query).get('Items', [])}
            item = by_user[uid].get(os.path.normcase(record['new_path']))
            if not item:
                continue
            old = record['data']
            current = item.get('UserData') or {}
            newer = (current.get('LastPlayedDate') or '') > (old.get('LastPlayedDate') or '') or current.get('PlaybackPositionTicks', 0) > old.get('PlaybackPositionTicks', 0) or current.get('PlayCount', 0) > old.get('PlayCount', 0)
            if not newer:
                fields = ('PlaybackPositionTicks', 'PlayCount', 'IsFavorite', 'Likes', 'Rating', 'LastPlayedDate', 'Played')
                payload = {k: old[k] for k in fields if k in old}
                worker.api('jellyfin', '/UserItems/' + item['Id'] + '/UserData?userId=' + urllib.parse.quote(uid), payload, method='POST')
            record['restored'] = True
            record['new_id'] = item['Id']
            save(path, journal)
        if all((r.get('restored') for r in records)):
            journal['phase'] = 'complete'
            save(path, journal)
