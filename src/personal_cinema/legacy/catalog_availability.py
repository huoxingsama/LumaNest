"""Retire missing derived links after complete scans; preserve originals and history."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import os
import re
import time
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from cache_policy import delete_cached
from library_routing import catalog_relative, meaningful
from library_tasks import read, write

def restore_link(worker, entry):
    archive = entry.get('catalog_archive')
    if not archive:
        return
    source = cache_path(worker.root, archive)
    target = (worker.root / 'catalog' / catalog_relative(entry)).with_suffix('.strm')
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        for file in source.iterdir():
            cache_path(worker.root, file.relative_to(worker.root).as_posix())
            dest = target.parent / file.name
            if file.is_file() and (not dest.exists()):
                file.rename(dest)
    entry.pop('catalog_unavailable_archived', None)
    entry.pop('catalog_archive', None)

def work_id(e):
    match = re.search('\\[tmdbid-(\\d+)\\]', catalog_relative(e))
    return e.get('movie_tmdb') or (match[1] if match else None)

def same_work(a, e):
    if a.get('kind') != e.get('kind'):
        return False
    if e.get('kind') == 'Movies':
        return Path(catalog_relative(a)).parent == Path(catalog_relative(e)).parent or bool(work_id(e) and work_id(a) == work_id(e))
    if e.get('kind') != 'Shows':
        return False
    series_e = e.get('series_tmdb') or e.get('series')
    series_a = a.get('series_tmdb') or a.get('series')
    if not series_e or series_e != series_a or bool(e.get('is_extra')) != bool(a.get('is_extra')):
        return False
    if e.get('absolute_episode') and a.get('absolute_episode'):
        return e['absolute_episode'] == a['absolute_episode']
    return e.get('episode') is not None and (e.get('season'), e['episode']) == (a.get('season'), a.get('episode'))

def replacement_for(worker, e):
    alternatives = [a for a in worker.entries.values() if a['id'] != e['id'] and same_work(a, e) and (not a.get('source_missing')) and (not a.get('catalog_hidden')) and (not a.get('catalog_unavailable_archived'))]
    return max(alternatives, key=lambda a: (a.get('quality_preferred', False), a.get('ready', False), a.get('size', 0)), default=None)

def reconnect_replacements(worker):
    for e in worker.entries.values():
        if not e.get('replacement_pending'):
            continue
        new = replacement_for(worker, e)
        if not new:
            continue
        for path in (worker.root / 'data/library-migrations').glob('*.json'):
            journal = read(path)
            changed = False
            for record in journal.get('records', []):
                if record.get('entry_id') != e['id'] or not record.get('waiting_replacement'):
                    continue
                record.update(new_path=str((worker.root / 'catalog' / catalog_relative(new)).with_suffix('.strm')), replacement_entry_id=new['id'], waiting_replacement=False, restored=False)
                changed = True
            if changed:
                journal['phase'] = 'moved'
                write(path, journal)
        if e.get('had_cache_on_retirement') and (not new.get('ready')):
            new.update(queued=True, queued_at=time.time(), cache_upgrade_from=e['id'])
        e.update(replacement_pending=False, quality_superseded_by=new['id'])

def reconcile_missing(worker, successful_sources):
    if getattr(worker, 'playback_present', False):
        return 0
    reconnect_replacements(worker)
    candidates = [e for e in worker.entries.values() if e.get('source_missing') and (not e.get('catalog_unavailable_archived')) and e.get('relative') and (not e.get('active')) and (getattr(worker, 'downloading', None) != e['id']) and (not getattr(worker, 'active', {}).get(e['id'])) and any((e.get('remote', '').startswith(s.rstrip('/') + '/') for s in successful_sources))]
    if not candidates:
        return 0
    from series_view import literal_sidecars
    old_paths = {os.path.normcase(str((worker.root / 'catalog' / catalog_relative(e)).with_suffix('.strm'))): e for e in candidates}
    records = []
    replacements = {}
    selected_replacements = {}
    for e in candidates:
        replacement = replacement_for(worker, e)
        replacements[e['id']] = str((worker.root / 'catalog' / catalog_relative(replacement or e)).with_suffix('.strm'))
        selected_replacements[e['id']] = replacement
    for user in worker.api('jellyfin', '/Users'):
        items = worker.api('jellyfin', '/Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=Path&EnableUserData=true&UserId=' + user['Id'])['Items']
        for item in items:
            e = old_paths.get(os.path.normcase(item.get('Path', '')))
            if e and meaningful(item.get('UserData') or {}):
                records.append({'user_id': user['Id'], 'old_id': item['Id'], 'entry_id': e['id'], 'replacement_entry_id': (selected_replacements[e['id']] or {}).get('id'), 'waiting_replacement': selected_replacements[e['id']] is None, 'new_path': replacements[e['id']], 'data': item['UserData'], 'restored': False})
    stamp = str(time.time_ns())
    base = worker.root / 'data/unavailable-catalog' / stamp
    before = {e['id']: dict(e) for e in candidates}
    journal = worker.root / 'data/library-migrations' / ('unavailable-' + stamp + '.json')
    write(journal, {'phase': 'archiving', 'records': records, 'created_at': time.time(), 'entry_ids': list(before)})
    moves = []
    try:
        for e in candidates:
            old = cache_path(worker.root / 'catalog', Path(catalog_relative(e)).with_suffix('.strm').as_posix())
            destination = base / e['id']
            destination.mkdir(parents=True, exist_ok=True)
            for source in literal_sidecars(old):
                cache_path(worker.root / 'catalog', source.relative_to(worker.root / 'catalog').as_posix())
                target = destination / source.name
                source.rename(target)
                moves.append((target, source))
            e['catalog_unavailable_archived'] = True
            e['catalog_archive'] = destination.relative_to(worker.root).as_posix()
            replacement = selected_replacements[e['id']]
            had_cache = bool(e.get('ready') or e.get('partial_bytes_completed'))
            e.update(had_cache_on_retirement=had_cache, replacement_pending=replacement is None)
            if replacement:
                e['quality_superseded_by'] = replacement['id']
            if replacement and had_cache and (not replacement.get('ready')):
                replacement.update(queued=True, queued_at=time.time(), cache_upgrade_from=e['id'])
            delete_cached(_cinema_cache_path('ready'), e['relative'])
            for suffix in ('.part', '.invalid'):
                delete_cached(_cinema_cache_path('partial'), e['id'] + suffix)
            (worker.root / 'data/range-resume' / (e['id'] + '.json')).unlink(missing_ok=True)
            e.update(ready=False, cached_at=None, source_retired_at=time.time(), queued=False)
            e.pop('partial_bytes_completed', None)
        worker.save()
        write(journal, {'phase': 'moved', 'series': 'unavailable-cloud-links', 'records': records, 'created_at': time.time(), 'entry_ids': list(before)})
    except Exception:
        for source, target in reversed(moves):
            source.rename(target)
        for e in candidates:
            ident = e['id']
            e.clear()
            e.update(before[ident])
        write(journal, {'phase': 'rolled-back', 'records': records})
        raise
    return len(candidates)
