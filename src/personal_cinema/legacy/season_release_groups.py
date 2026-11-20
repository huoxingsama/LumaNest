"""Put explicit season release folders under an already confirmed series."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import shutil
import time
import urllib.parse
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from library_routing import catalog_relative, meaningful
from library_tasks import read, write
from series_view import literal_sidecars
from smart_metadata import _label, normalize_title
SEASON = re.compile('(?i)(?:第\\s*[零〇一二两三四五六七八九十百\\d]+\\s*季|(?:^|[ ._-])(?:Season\\s*|S)\\d{1,2}(?!\\d))')

def series_release_title(value):
    if not SEASON.search(value):
        return None
    title, year = _label(value, kind='Series')
    if not title or normalize_title(title) == normalize_title(value):
        return None
    return title + (' (' + str(year) + ')' if year else '')

def merge_release_seasons(worker, entries):
    from cache_worker import unique_relative, write_catalog
    root = worker.root
    groups = {}
    result = []
    for entry in entries:
        if entry.get('kind') != 'Shows' or entry.get('source_missing') or entry.get('catalog_hidden') or (entry.get('season') == 99):
            continue
        base = series_release_title(entry.get('series', ''))
        if base:
            groups.setdefault((entry['series'], base), []).append(entry)
    for (release, base), selected in groups.items():
        rows = worker.api('jellyfin', '/Items?Recursive=true&IncludeItemTypes=Series&Fields=Path,ProviderIds&SearchTerm=' + urllib.parse.quote(base))['Items']
        candidates = [i for i in rows if (i.get('ProviderIds') or {}).get('Tmdb') and i.get('Path') and (normalize_title(Path(i['Path']).name) == normalize_title(base))]
        if len(candidates) != 1:
            continue
        parent = candidates[0]
        target = Path(parent['Path']).resolve()
        target = cache_path(root / 'catalog', target.relative_to((root / 'catalog').resolve()).as_posix())
        sources = {cache_path(root / 'catalog', Path(catalog_relative(e)).parts[0] + '/' + Path(catalog_relative(e)).parts[1]) for e in selected}
        if len(sources) != 1:
            continue
        source = sources.pop()
        if source == target:
            continue
        if any(((worker.active or {}).get(e['id']) or e.get('playback_active') for e in selected)):
            continue
        old_series = next((i for i in rows if i.get('Path') and Path(i['Path']).resolve() == source.resolve()), None)
        if old_series and (old_series.get('ProviderIds') or {}).get('Tmdb') not in (None, (parent.get('ProviderIds') or {}).get('Tmdb')):
            continue
        known = {e['id'] for e in selected}
        unknown = []
        for f in source.rglob('*.strm'):
            cache_path(root / 'catalog', f.relative_to(root / 'catalog').as_posix())
            if not any(('/stream/' + ident in f.read_text('utf-8') for ident in known)):
                unknown.append(f)
        if unknown:
            continue
        moves = []
        for e in selected:
            old = cache_path(root / 'catalog', Path(catalog_relative(e)).with_suffix('.strm').as_posix())
            name = target.name + ' S' + str(e['season']).zfill(2) + 'E' + str(e['episode']).zfill(2) + Path(e['remote']).suffix.lower()
            relative = unique_relative((target.relative_to(root / 'catalog') / ('Season ' + str(e['season']).zfill(2)) / name).as_posix(), e['id'])
            new = cache_path(root / 'catalog', Path(relative).with_suffix('.strm').as_posix())
            if new.exists() and '/stream/' + e['id'] not in new.read_text('utf-8'):
                raise ValueError('Season destination belongs to another stream')
            moves.append((e, old, new, relative))
        backup = root / 'data/season-release-migrations' / str(time.time_ns())
        backup.mkdir(parents=True)
        write(backup / 'entries-before.json', {e['id']: dict(e) for e in selected})
        journal = root / 'data/library-migrations' / ('season-release-' + selected[0]['id'] + '.json')
        records = list(read(journal).get('records') or [])
        for user in worker.api('jellyfin', '/Users'):
            if not old_series:
                continue
            query = urllib.parse.urlencode({'ParentId': old_series['Id'], 'Recursive': 'true', 'Fields': 'Path', 'EnableUserData': 'true', 'UserId': user['Id']})
            for item in worker.api('jellyfin', '/Items?' + query).get('Items', []):
                if not item.get('Path') or not meaningful(item.get('UserData') or {}):
                    continue
                oldpath = Path(item['Path']).resolve()
                match = next(((e, new) for e, old, new, _ in moves if old.resolve() == oldpath), None)
                if match:
                    newpath = match[1]
                elif item.get('Type') == 'Season' and oldpath.is_relative_to(source.resolve()):
                    newpath = target / oldpath.relative_to(source.resolve())
                else:
                    continue
                records.append({'user_id': user['Id'], 'old_id': item['Id'], 'new_path': str(newpath), 'replacement_entry_id': match[0]['id'] if match else None, 'data': item['UserData'], 'restored': False})
            old = worker.api('jellyfin', '/Users/' + user['Id'] + '/Items/' + old_series['Id']).get('UserData') or {}
            if meaningful(old):
                current = worker.api('jellyfin', '/Users/' + user['Id'] + '/Items/' + parent['Id']).get('UserData') or {}
                payload = {k: current[k] for k in ('Played', 'PlayCount', 'PlaybackPositionTicks', 'LastPlayedDate', 'IsFavorite', 'Likes', 'Rating') if k in current}
                if old.get('IsFavorite'):
                    payload['IsFavorite'] = True
                if old.get('Likes') is not None and current.get('Likes') is None:
                    payload['Likes'] = old['Likes']
                if old.get('Rating') is not None and current.get('Rating') is None:
                    payload['Rating'] = old['Rating']
                write(backup / ('series-user-' + user['Id'] + '.json'), old)
                worker.api('jellyfin', '/UserItems/' + parent['Id'] + '/UserData?userId=' + user['Id'], payload, method='POST')
        records = list({(r['user_id'], r['old_id']): r for r in records}.values())
        write(journal, {'phase': 'moving', 'series': release, 'records': records, 'backup': str(backup)})
        for e, old, new, relative in moves:
            new.parent.mkdir(parents=True, exist_ok=True)
            for sidecar in literal_sidecars(old):
                checked = cache_path(root / 'catalog', sidecar.relative_to(root / 'catalog').as_posix())
                destination = cache_path(root / 'catalog', (new.parent / (new.stem + sidecar.name[len(old.stem):])).relative_to(root / 'catalog').as_posix())
                if destination.exists():
                    if destination.read_bytes() != checked.read_bytes():
                        raise ValueError('Season sidecar conflict')
                    archived = backup / 'duplicate-sidecars' / e['id']
                    archived.mkdir(parents=True, exist_ok=True)
                    checked.rename(archived / checked.name)
                else:
                    checked.rename(destination)
            for filename in ('season.nfo', 'poster.jpg', 'fanart.jpg'):
                origin = old.parent / filename
                destination = new.parent / filename
                if origin.is_file() and (not destination.exists()):
                    shutil.copy2(origin, destination)
            e.setdefault('cache_relative', e['relative'])
            e.update(catalog_relative=relative, series=target.name, series_id=parent['Id'], canonical_series_tmdb=str(parent['ProviderIds']['Tmdb']), display_title=parent['Name'], identity_needs_review=False)
            write_catalog(root / 'catalog', e, worker.config['stream_base_url'], worker.stream_key)
        worker.save()
        for f in source.rglob('*'):
            cache_path(root / 'catalog', f.relative_to(root / 'catalog').as_posix())
        checked_source = source.resolve()
        checked_source.relative_to((root / 'catalog').resolve())
        checked_source.rename(backup / 'old-catalog')
        write(journal, {'phase': 'moved', 'series': release, 'records': records, 'backup': str(backup)})
        from jellyfin_ingestion import notify_changed
        notify_changed(worker, paths=[source], update_type='Deleted')
        notify_changed(worker, paths=[m[2] for m in moves] + [target], update_type='Created')
        worker.api('jellyfin', '/Items/' + parent['Id'] + '/Refresh?Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=None&ReplaceAllMetadata=false', method='POST')
        result.append({'source': str(source), 'series_id': parent['Id'], 'tmdb': parent['ProviderIds']['Tmdb'], 'entries': [e['id'] for e in selected], 'paths': [str(m[2]) for m in moves], 'journal': str(journal)})
    return result
