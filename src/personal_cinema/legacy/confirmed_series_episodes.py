"""Resolve provisional Season 99 only against an exact, single-season provider table."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import shutil
import time
import urllib.parse
import urllib.request
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from library_tasks import read, write
from library_routing import catalog_relative, meaningful
from chinese_credits import Document

def provider_table(root, tmdb):
    if not str(tmdb).isdecimal():
        return None
    folder = root / 'data/manual-match-source-pages'
    folder.mkdir(parents=True, exist_ok=True)

    def page(suffix, filename):
        path = folder / filename
        if not path.is_file() or time.time() - path.stat().st_mtime > 86400:
            with urllib.request.urlopen('https://www.themoviedb.org/tv/' + tmdb + suffix + '?language=zh-CN', timeout=20) as response:
                content = response.read(6000000).decode('utf-8')
            path.write_text(content, 'utf-8')
        doc = Document(path.read_text('utf-8'))
        canonical = next((n.attrs.get('href', '') for n in doc.root.find('link') if n.attrs.get('rel') == 'canonical'), '')
        expected = '/tv/' + re.escape(tmdb) + '(?:-[^/]+)?' + re.escape(suffix) + '/?'
        if not re.fullmatch(expected, urllib.parse.urlsplit(canonical).path):
            raise ValueError('Series episode table identity mismatch')
        return doc
    listing = page('/seasons', tmdb + '-seasons.html')
    pattern = '/tv/' + re.escape(tmdb) + '(?:-[^/]+)?/season/(\\d+)/?'
    numbers = set()
    for link in listing.root.find('a'):
        match = re.fullmatch(pattern, urllib.parse.urlsplit(link.attrs.get('href', '')).path)
        if match and int(match[1]) > 0:
            numbers.add(int(match[1]))
    if len(numbers) != 1:
        return None
    number = next(iter(numbers))
    doc = page('/season/' + str(number), tmdb + '-season-' + str(number) + '.html')
    pattern = '/tv/' + re.escape(tmdb) + '(?:-[^/]+)?/season/' + str(number) + '/episode/(\\d+)/?'
    episodes = {}
    for card in doc.root.find(cls='card'):
        match = re.fullmatch(pattern, urllib.parse.urlsplit(card.attrs.get('data-url', '')).path)
        if not match:
            continue
        title = next((a.text().strip() for a in card.find('a') if a.text().strip() and re.fullmatch(pattern, urllib.parse.urlsplit(a.attrs.get('href', '')).path)), None)
        if not title:
            continue
        overview = next((p.text().strip() for p in card.find(cls='overview')), '')
        episodes[int(match[1])] = {'title': title, 'overview': overview}
    return {'season': number, 'episodes': episodes, 'source_url': 'https://www.themoviedb.org/tv/' + tmdb + '/season/' + str(number)} if episodes else None

def apply_mapping(root, entry):
    """Stable file identity keeps confirmed ordering across future source scans."""
    record = read(Path(root) / 'data/confirmed-series-episodes.json').get(entry.get('id'))
    if not record or record.get('size') != entry.get('size'):
        return False
    fid = record.get('source_fid')
    if fid and entry.get('source_fid') and (str(fid) != str(entry['source_fid'])):
        return False
    if entry.get('season') == 99 and record.get('catalog_relative'):
        entry.setdefault('cache_relative', entry.get('relative'))
        entry['catalog_relative'] = record['catalog_relative']
    entry.update(record['fields'])
    return True

def confirm_review_seasons(worker, entries, target_ids, explicit=False):
    from catalog_intelligence import episode_label
    from series_view import literal_sidecars
    from cache_worker import write_catalog
    from episode_review_metadata import clear_local_review
    root = worker.root
    state = read(root / 'data/metadata-state.json')
    results = []
    candidates = [e for e in entries if e.get('kind') == 'Shows' and e.get('season') == 99 and (not e.get('source_missing')) and (not e.get('catalog_hidden'))]
    if not candidates:
        return results
    for target_id in target_ids:
        parent = worker.api('jellyfin', '/Items/' + target_id)
        tmdb = str((parent.get('ProviderIds') or {}).get('Tmdb') or '')
        if parent.get('Type') != 'Series' or not tmdb.isdecimal() or (not parent.get('Path')):
            continue
        status = state.get(target_id, {})
        if not explicit and status.get('status') not in ('matched', 'already_matched', 'locked'):
            continue
        base = cache_path(root / 'catalog', Path(parent['Path']).resolve().relative_to((root / 'catalog').resolve()).as_posix())
        selected = [e for e in candidates if (root / 'catalog' / catalog_relative(e)).resolve().is_relative_to(base.resolve())]
        if not selected:
            continue
        table = provider_table(root, tmdb)
        if not table:
            continue
        numbered = []
        for entry in selected:
            label = episode_label(Path(entry['remote']).name)
            if not label or label.get('season') not in (None, table['season']) or label.get('episode') != entry.get('episode') or (entry.get('episode') not in table['episodes']):
                break
            numbered.append(entry['episode'])
        else:
            if len(numbered) != len(set(numbered)):
                continue
            if any((e.get('playback_active') or worker.active.get(e['id']) for e in selected)):
                continue
            moves = []
            for entry in selected:
                old = cache_path(root / 'catalog', Path(catalog_relative(entry)).with_suffix('.strm').as_posix())
                name = re.sub('S99(?=E\\d)', f"S{table['season']:02}", old.name, flags=re.I)
                new = cache_path(root / 'catalog', (base.relative_to(root / 'catalog') / f"Season {table['season']:02}" / name).as_posix())
                if new == old or new.exists():
                    raise ValueError('Confirmed episode destination is already occupied')
                moves.append((entry, old, new))
            backup = root / 'data/confirmed-series-migrations' / (target_id + '-' + str(time.time_ns()))
            backup.mkdir(parents=True)
            write(backup / 'entries-before.json', {e['id']: dict(e) for e in selected})
            journal = root / 'data/library-migrations' / ('confirmed-season-' + target_id + '.json')
            records = list(read(journal).get('records') or [])
            old_parents = {old.parent for _, old, _ in moves}
            for user in worker.api('jellyfin', '/Users'):
                query = urllib.parse.urlencode({'ParentId': target_id, 'Recursive': 'true', 'IncludeItemTypes': 'Episode,Season', 'Fields': 'Path', 'UserId': user['Id'], 'EnableUserData': 'true'})
                for item in worker.api('jellyfin', '/Items?' + query).get('Items', []):
                    if not item.get('Path') or not meaningful(item.get('UserData') or {}):
                        continue
                    oldpath = Path(item['Path']).resolve()
                    move = next((m for m in moves if m[1].resolve() == oldpath), None)
                    if move:
                        newpath = move[2]
                    elif item.get('Type') == 'Season' and oldpath in {p.resolve() for p in old_parents}:
                        newpath = moves[0][2].parent
                    else:
                        continue
                    records.append({'user_id': user['Id'], 'old_id': item['Id'], 'new_path': str(newpath), 'replacement_entry_id': move[0]['id'] if move else None, 'data': item['UserData'], 'restored': False})
            records = list({(r['user_id'], r['old_id']): r for r in records}.values())
            write(journal, {'phase': 'moving', 'series': parent['Name'], 'records': records, 'backup': str(backup)})
            confirmed = read(root / 'data/confirmed-series-episodes.json')
            rules = read(root / 'data/manual-metadata-overrides.json')
            for entry, old, new in moves:
                episode = table['episodes'][entry['episode']]
                fields = {'season': table['season'], 'episode': entry['episode'], 'series_id': target_id, 'series_tmdb': tmdb, 'series_imdb': (parent.get('ProviderIds') or {}).get('Imdb'), 'series_year': parent.get('ProductionYear'), 'display_title': parent['Name'], 'season_title': '第 ' + str(table['season']) + ' 季', 'episode_title': episode['title'], 'episode_overview': episode['overview'], 'episode_title_prefix': False, 'identity_needs_review': False, 'recognition_rule': 'confirmed-single-season', 'recognition_evidence': '已确认作品；官方单季集目与源文件集号逐一对应'}
                entry.update(fields)
                clear_local_review(root / 'catalog', entry)
                new.parent.mkdir(parents=True, exist_ok=True)
                for sidecar in literal_sidecars(old):
                    checked = cache_path(root / 'catalog', sidecar.relative_to(root / 'catalog').as_posix())
                    destination = cache_path(root / 'catalog', (new.parent / (new.stem + sidecar.name[len(old.stem):])).relative_to(root / 'catalog').as_posix())
                    if destination.exists():
                        raise ValueError('Confirmed episode sidecar destination is occupied')
                    checked.rename(destination)
                entry.setdefault('cache_relative', entry['relative'])
                entry['catalog_relative'] = new.with_suffix(Path(entry['remote']).suffix).relative_to(root / 'catalog').as_posix()
                write_catalog(root / 'catalog', entry, worker.config['stream_base_url'], worker.stream_key)
                confirmed[entry['id']] = {'fields': fields, 'size': entry['size'], 'source_fid': entry.get('source_fid'), 'catalog_relative': entry['catalog_relative'], 'source_url': table['source_url'], 'confirmed_at': time.time()}
                if entry['id'] in rules.get('entries', {}):
                    rules['entries'][entry['id']]['episode_mapping'] = fields
                write(root / 'data/confirmed-series-episodes.json', confirmed)
                if rules:
                    write(root / 'data/manual-metadata-overrides.json', rules)
                worker.save()
            for old_parent in old_parents:
                remaining = list(old_parent.iterdir())
                allowed = {'season.nfo', 'poster.jpg', 'poster.png', 'fanart.jpg', 'folder.jpg', 'landscape.jpg', 'banner.jpg'}
                if all((p.is_file() and p.name.lower() in allowed for p in remaining)):
                    checked = cache_path(root / 'catalog', old_parent.relative_to(root / 'catalog').as_posix())
                    destination = backup / old_parent.name
                    checked.rename(destination)
            write(journal, {'phase': 'moved', 'series': parent['Name'], 'records': records, 'backup': str(backup)})
            from jellyfin_ingestion import notify_changed
            notify_changed(worker, paths=[old for _, old, _ in moves] + list(old_parents), update_type='Deleted')
            notify_changed(worker, paths=[new for _, _, new in moves], update_type='Created')
            worker.api('jellyfin', '/Items/' + target_id + '/Refresh?Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=None&ReplaceAllMetadata=false', method='POST')
            results.extend((e['id'] for e in selected))
    return results
