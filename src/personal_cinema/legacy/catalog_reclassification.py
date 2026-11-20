"""Split raw anthology entries before metadata matching, preserving cache IDs."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import time
import urllib.parse
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from catalog_rules import retain_cache_path
from franchise_context import context, verified_source_identity
from library_routing import catalog_relative, meaningful
from library_tasks import read, write
from series_view import literal_sidecars

def reclassify_context(worker, target_ids=None):
    from cache_worker import clean_name, unique_relative, write_catalog
    root = worker.root
    bindings = read(root / 'data/source-bindings.json')
    planned = []
    for entry in worker.entries.values():
        if target_ids is not None and entry['id'] not in target_ids:
            continue
        renamed = entry.get('source_rename_pending')
        if entry.get('kind') != 'Movies' or entry.get('source_missing') or entry.get('catalog_hidden') or (not renamed and (entry.get('canonical_movie_tmdb') or entry.get('catalog_rule_applied') or bindings.get(entry['id'], {}).get('normalized'))) or entry.get('playback_active') or getattr(worker, 'active', {}).get(entry['id']):
            continue
        evidence = context(entry.get('remote', ''))
        if not renamed and evidence.get('role') != 'movie':
            continue
        identity = verified_source_identity(entry['remote'])
        if not renamed and (not identity) and (not evidence.get('filename_first')):
            continue
        title = renamed.get('display_title') or renamed.get('series') if renamed else identity['title'] if identity else next(iter(evidence.get('titles') or []), None)
        if not title:
            continue
        old_rel = Path(catalog_relative(entry)).with_suffix('.strm').as_posix()
        if identity and re.search('\\[tmdbid-' + re.escape(identity['provider_ids']['Tmdb']) + '\\]', old_rel):
            continue
        folder = clean_name(title)
        relative = f'Movies/{folder}/{folder}' + Path(entry['remote']).suffix.lower()
        cached_entry = {**entry, 'cache_relative': entry.get('cache_relative') or entry['relative']}
        classified = retain_cache_path(cached_entry, {'kind': 'Movies', 'series': folder, 'display_title': title, 'relative': relative, 'catalog_relative': relative}, entry['id'], unique_relative)
        new_rel = Path(classified['catalog_relative']).with_suffix('.strm').as_posix()
        if new_rel == old_rel:
            continue
        old = cache_path(root / 'catalog', old_rel)
        new = cache_path(root / 'catalog', new_rel)
        if new.exists() and '/stream/' + entry['id'] not in new.read_text('utf-8'):
            raise ValueError('Catalog target belongs to a different stream')
        planned.append({'entry': entry, 'before': dict(entry), 'classified': classified, 'old': old, 'new': new})
    if not planned:
        return {'changed': 0, 'entries': []}
    userdata = {}
    for user in worker.api('jellyfin', '/Users'):
        query = urllib.parse.urlencode({'Recursive': 'true', 'IncludeItemTypes': 'Movie', 'Fields': 'Path', 'EnableUserData': 'true', 'UserId': user['Id']})
        userdata[user['Id']] = worker.api('jellyfin', '/Items?' + query).get('Items', [])
    changed = []
    old_paths = []
    new_paths = []
    for plan in planned:
        entry = plan['entry']
        old = plan['old']
        new = plan['new']
        ident = entry['id']
        backup = root / 'data/catalog-reclassification' / ident / str(time.time_ns())
        backup.mkdir(parents=True)
        journal = root / 'data/library-migrations' / ('source-context-' + ident + '.json')
        records = []
        for uid, items in userdata.items():
            for item in items:
                path = item.get('Path', '')
                match = re.search('\\[([a-f0-9]{12})\\]', Path(path).stem)
                same = str(Path(path).absolute()).casefold() == str(old.absolute()).casefold() if path else False
                if (same or (match and match[1] == ident[:12])) and meaningful(item.get('UserData') or {}):
                    records.append({'user_id': uid, 'old_id': item['Id'], 'new_path': str(new), 'replacement_entry_id': ident, 'data': item['UserData'], 'restored': False})
        write(backup / 'entry-before.json', plan['before'])
        write(journal, {'phase': 'moving', 'series': 'source-context-reclassification', 'records': records, 'entry_id': ident, 'old_path': str(old), 'new_path': str(new), 'backup': str(backup)})
        moves = []
        try:
            if old.parent.exists():
                for sidecar in literal_sidecars(old):
                    checked = cache_path(root / 'catalog', sidecar.relative_to(root / 'catalog').as_posix())
                    destination = backup / checked.name
                    checked.rename(destination)
                    moves.append((destination, checked))
            entry.setdefault('cache_relative', entry['relative'])
            entry.update(plan['classified'])
            entry.pop('jellyfin_item_id', None)
            if entry.pop('source_rename_pending', None):
                entry.pop('canonical_movie_tmdb', None)
                entry.pop('catalog_rule_applied', None)
                entry['source_renamed_at'] = time.time()
                entry['source_name_override'] = entry['display_title']
            write_catalog(root / 'catalog', entry, worker.config['stream_base_url'], worker.stream_key)
            worker.save()
            write(journal, {'phase': 'moved', 'series': 'source-context-reclassification', 'records': records, 'entry_id': ident, 'old_path': str(old), 'new_path': str(new), 'backup': str(backup)})
        except Exception:
            new.unlink(missing_ok=True)
            for archived, original in reversed(moves):
                if not original.exists():
                    archived.rename(original)
            entry.clear()
            entry.update(plan['before'])
            worker.save()
            write(journal, {'phase': 'rolled-back', 'series': 'source-context-reclassification', 'records': records, 'backup': str(backup)})
            raise
        old_paths.append(old)
        new_paths.append(new)
        changed.append(ident)
    from jellyfin_ingestion import notify_changed
    notify_changed(worker, paths=old_paths, update_type='Deleted')
    notify_changed(worker, paths=new_paths, update_type='Created')
    return {'changed': len(changed), 'entries': changed}
