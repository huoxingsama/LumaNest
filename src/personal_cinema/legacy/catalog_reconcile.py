"""Journalled, idle-only canonical movie paths; cloud/cache identities never move."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import re
import shutil
import time
import urllib.parse
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from library_routing import catalog_relative, meaningful
from library_tasks import read, write
from movie_groups import clean_movie_title

def queue_movie(root, item, entries):
    if item.get('Type') != 'Movie' or item.get('IsLocked'):
        return
    tmdb = str((item.get('ProviderIds') or {}).get('Tmdb') or '')
    if not tmdb.isdecimal() or not item.get('ProductionYear'):
        return
    try:
        relative = Path(item['Path']).absolute().relative_to((root / 'catalog').absolute())
    except (ValueError, KeyError):
        return
    if relative.parts[0] != 'Movies':
        return
    canonical_path = f'[tmdbid-{tmdb}]' in str(relative)
    from cache_worker import clean_name
    aliases = read(root / 'data/recognition-aliases.json', [])
    canonical = clean_movie_title(next((a['title'] for a in aliases if a.get('kind') == 'Movie' and a.get('year') == item.get('ProductionYear') and (a.get('provider_ids', {}).get('Tmdb') == tmdb)), item['Name']))
    preferred_folder = clean_name(canonical) + f" ({item['ProductionYear']}) [tmdbid-{tmdb}]"
    path_tag = re.search('\\[tmdbid-(\\d+)\\]', relative.parent.name)
    repair_path = bool(path_tag and (path_tag[1] != tmdb or relative.parent.name != preferred_folder))
    if canonical_path and (not repair_path):
        return
    matched = [e for e in entries.values() if Path(catalog_relative(e)).with_suffix('.strm') == relative and (not e.get('catalog_hidden')) and (not e.get('source_rename_pending')) and (not e.get('catalog_rule_applied') or repair_path)]
    if not matched:
        return
    ident = hashlib.sha256((str(relative) + tmdb).encode()).hexdigest()[:24]
    file = root / 'data/requests' / ('canonical-' + ident + '.json')
    if not file.exists():
        write(file, {'action': 'canonical-movie', 'id': matched[0]['id'], 'item_id': item['Id'], 'old_relative': relative.as_posix(), 'tmdb': tmdb, 'title': canonical, 'year': item['ProductionYear']})

def reconcile(worker, payload):
    from series_view import literal_sidecars
    if getattr(worker, 'playback_present', False) or any((e.get('active') or worker.active.get(e['id']) for e in worker.entries.values())):
        return False
    from cache_worker import clean_name, unique_relative
    ident = payload['id']
    entry = worker.entries.get(ident)
    if not entry or entry.get('catalog_hidden'):
        return True
    previous_title = entry.get('display_title') or clean_movie_title(Path(catalog_relative(entry)).parent.name)
    payload = {**payload, 'title': clean_movie_title(payload['title'])}
    wanted_folder = clean_name(payload['title']) + f" ({int(payload['year'])}) [tmdbid-{payload['tmdb']}]"
    if entry.get('canonical_movie_tmdb') == payload['tmdb'] and Path(catalog_relative(entry)).parent.name == wanted_folder:
        folder = Path(catalog_relative(entry)).parts[1]
        _save_rule(worker, entry, folder, payload, previous_title=previous_title)
        _notify_paths(worker, payload, entry)
        return True
    old = cache_path(worker.root / 'catalog', Path(catalog_relative(entry)).with_suffix('.strm').as_posix())
    if old.relative_to(worker.root / 'catalog').as_posix() != payload['old_relative']:
        return True
    item = worker.api('jellyfin', '/Items/' + payload['item_id'])
    if str(item.get('ProviderIds', {}).get('Tmdb')) != payload['tmdb'] or item.get('IsLocked'):
        return True
    if Path(item.get('Path', '')).absolute() != old.absolute():
        return True
    folder = clean_name(payload['title']) + f" ({int(payload['year'])}) [tmdbid-{payload['tmdb']}]"
    existing = [Path(catalog_relative(e)).parts[1] for e in worker.entries.values() if e.get('kind') == 'Movies' and f"[tmdbid-{payload['tmdb']}]" in catalog_relative(e) and (not e.get('catalog_hidden')) and (not e.get('catalog_unavailable_archived'))]
    if folder in existing:
        folder = folder
    relative = unique_relative(f'Movies/{folder}/{folder}' + Path(entry['remote']).suffix.lower(), ident)
    target = cache_path(worker.root / 'catalog', Path(relative).with_suffix('.strm').as_posix())
    if target.exists():
        return True
    records = []
    for user in worker.api('jellyfin', '/Users'):
        q = urllib.parse.urlencode({'UserId': user['Id'], 'Ids': item['Id'], 'EnableUserData': 'true'})
        for row in worker.api('jellyfin', '/Items?' + q).get('Items', []):
            if meaningful(row.get('UserData') or {}):
                records.append({'user_id': user['Id'], 'old_id': item['Id'], 'new_path': str(target), 'data': row['UserData'], 'restored': False})
    journal = worker.root / 'data/library-migrations' / ('canonical-' + ident + '.json')
    backup = worker.root / 'data/backups' / ('canonical-' + ident + '-' + str(int(time.time())))
    backup.mkdir(parents=True)
    write(backup / 'entry.json', entry)
    write(backup / 'item.json', item)
    write(journal, {'series': 'movie-canonicalization', 'phase': 'moving', 'records': records, 'created_at': time.time(), 'backup': str(backup)})
    moves = []
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        for source in literal_sidecars(old):
            cache_path(worker.root / 'catalog', source.relative_to(worker.root / 'catalog').as_posix())
            dest = target.with_name(target.stem + source.name[len(old.stem):])
            if dest.exists():
                raise FileExistsError('Canonical sidecar already exists')
            source.rename(dest)
            moves.append((dest, source))
        entry.setdefault('cache_relative', entry['relative'])
        entry.update(catalog_relative=relative, series=folder, display_title=payload['title'], canonical_movie_tmdb=payload['tmdb'])
        entry.pop('jellyfin_item_id', None)
        worker.save()
        write(journal, {'series': 'movie-canonicalization', 'phase': 'moved', 'records': records, 'created_at': time.time(), 'backup': str(backup)})
    except Exception:
        for source, dest in reversed(moves):
            source.rename(dest)
        entry.clear()
        entry.update(read(backup / 'entry.json'))
        worker.save()
        write(journal, {'phase': 'rolled-back', 'records': records, 'backup': str(backup)})
        raise
    _save_rule(worker, entry, folder, payload, previous_title=previous_title)
    _notify_paths(worker, payload, entry)
    return True

def _notify_paths(worker, payload, entry):
    journal = worker.root / 'data/library-migrations' / ('canonical-' + entry['id'] + '.json')
    record = read(journal)
    if record.get('native_notified'):
        return
    from jellyfin_ingestion import notify_changed
    old = cache_path(worker.root / 'catalog', payload['old_relative'])
    target = cache_path(worker.root / 'catalog', Path(catalog_relative(entry)).with_suffix('.strm').as_posix())
    if old != target:
        notify_changed(worker, paths=[old], update_type='Deleted')
    notify_changed(worker, paths=[target], update_type='Created')
    write(journal, {**record, 'native_notified': True})

def _save_rule(worker, entry, folder, payload, previous_title=None):
    rules = read(worker.root / 'data/catalog-rules.json', [])
    previous = next((rule for rule in rules if rule.get('remote') == entry['remote']), {})
    rule = {'remote': entry['remote'], 'kind': 'Movies', 'catalog_folder': folder, 'title': payload['title'], 'verified_by': 'metadata-identity', 'tmdb': payload['tmdb']}
    rules = [r for r in rules if r.get('remote') != entry['remote']] + [rule]
    write(worker.root / 'data/catalog-rules.json', rules)
    worker.catalog_rules = rules
    bindings_path = worker.root / 'data/source-bindings.json'
    bindings = read(bindings_path)
    binding = bindings.get(entry['id'])
    if binding:
        old_title = previous_title if previous_title is not None else binding.get('layout_title') or previous.get('title')
        if old_title is not None and str(old_title) != payload['title']:
            binding['normalized'] = False
        binding['layout_title'] = payload['title']
        write(bindings_path, bindings)
