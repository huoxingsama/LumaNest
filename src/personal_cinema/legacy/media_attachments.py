"""Explicit bonus clips belong beside their feature, not in the movie library."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from franchise_context import ALIASES, BONUS, context, verified_source_identity
from library_routing import catalog_relative
from library_tasks import read, write
MUSIC_BONUS = re.compile('主题曲|主題曲|主題歌|片头曲|片頭曲|片尾曲|插曲|音乐录影带|音樂錄影帶|music[ ._-]*video|theme[ ._-]*song|(?:^|[\\s._\\-\\[（(])(?:MV|OP|ED)(?:$|[\\s._\\-\\]）)])', re.I)
BONUS_FOLDERS = {'附件', '附加内容', '附加內容', '特典', 'extras', 'featurettes', 'clips', 'other', 'music videos', 'mv'}

def explicit_bonus(remote):
    path = PurePosixPath(remote)
    return bool(BONUS.search(path.stem) or MUSIC_BONUS.search(path.stem) or path.parent.name.casefold() in BONUS_FOLDERS)

def mark_discovered_bonus(entry, remote, source_root, listing):
    """A named theme clip beside a feature never becomes a movie version."""
    if not explicit_bonus(remote) or str(PurePosixPath(remote).parent) == source_root:
        return
    from cache_worker import VIDEO
    parent = PurePosixPath(remote).parent
    features = [r for r in listing if not r.get('is_dir') and Path(r['name']).suffix.lower() in VIDEO and (not explicit_bonus(str(parent / r['name'])))]
    if len(features) == 1:
        entry.update(attachment_role='bonus', attachment_original_remote=remote)

def attachment_feature(worker, entry):
    selected = worker.entries.get(entry.get('attachment_parent_id'))
    if selected and (not selected.get('source_missing')) and (not selected.get('catalog_hidden')):
        return selected
    bindings = read(worker.root / 'data/source-bindings.json')

    def paths(e):
        b = bindings.get(e['id'], {})
        return [p for p in [e.get('remote'), e.get('attachment_original_remote'), b.get('original_remote'), *b.get('previous_paths', [])] if p]
    packs = set()
    for path in paths(entry):
        parent = PurePosixPath(path).parent
        while parent.name.casefold() in BONUS_FOLDERS:
            parent = parent.parent
        if str(parent) not in ('/夸克/movie', '/百度/movie'):
            packs.add(str(parent))
    candidates = []
    for feature in worker.entries.values():
        if feature['id'] == entry['id'] or feature.get('kind') != 'Movies' or feature.get('catalog_hidden') or feature.get('source_missing') or feature.get('attachment_role'):
            continue
        if any((str(PurePosixPath(p).parent) in packs and (not explicit_bonus(p)) for p in paths(feature))):
            candidates.append(feature)
    if len(candidates) == 1:
        return candidates[0]
    return None

def publish_bonus(worker, entry, feature):
    """Use native extras, so every client exposes it inside the feature page."""
    from cache_worker import clean_name
    from jellyfin_ingestion import notify_changed
    root = worker.root
    original = entry.get('attachment_original_remote') or entry['remote']
    name = clean_name(PurePosixPath(original).stem)
    folder = Path(catalog_relative(feature)).parent / 'extras'
    relative = (folder / (name + ' [' + entry['id'][:12] + '].strm')).as_posix()
    target = cache_path(root / 'catalog', relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = worker.config['stream_base_url'].rstrip('/') + '/stream/' + entry['id'] + '?key=' + worker.stream_key + '\n'
    changed = not target.exists() or target.read_text('utf-8') != text
    previous = entry.get('attachment_catalog_relative')
    if previous and previous != relative:
        old = cache_path(root / 'catalog', previous)
        if old.exists():
            records = []
            from library_routing import meaningful
            for user in worker.api('jellyfin', '/Users'):
                query = urllib.parse.urlencode({'Recursive': 'true', 'UserId': user['Id'], 'Fields': 'Path', 'EnableUserData': 'true'})
                for item in worker.api('jellyfin', '/Items?' + query).get('Items', []):
                    if item.get('Path') and Path(item['Path']).absolute() == old.absolute() and meaningful(item.get('UserData') or {}):
                        records.append({'user_id': user['Id'], 'old_id': item['Id'], 'new_path': str(target), 'data': item['UserData'], 'restored': False})
            write(root / 'data/library-migrations' / ('attachment-' + entry['id'] + '.json'), {'phase': 'moved', 'series': 'movie-extra', 'records': records, 'created_at': time.time()})
            archive = root / 'data/attachments' / entry['id'] / ('published-' + str(time.time_ns()))
            archive.mkdir(parents=True, exist_ok=True)
            old.rename(archive / old.name)
            if old.with_suffix('.nfo').exists():
                old.with_suffix('.nfo').rename(archive / old.with_suffix('.nfo').name)
            notify_changed(worker, paths=[old], update_type='Deleted')
    if changed:
        target.write_text(text, 'utf-8')
        node = ET.Element('movie')
        ET.SubElement(node, 'title').text = name
        ET.SubElement(node, 'plot').text = '本片附加内容' + (' · 主题曲 / 音乐影像' if MUSIC_BONUS.search(name) else '')
        ET.SubElement(node, 'lockdata').text = 'true'
        ET.ElementTree(node).write(target.with_suffix('.nfo'), encoding='utf-8', xml_declaration=True)
        notify_changed(worker, paths=[target, target.parent.parent], update_type='Created')
    entry.update(attachment_catalog_relative=relative, attachment_parent_id=feature['id'], attachment_published=True)
    return changed

def _subtitle_key(title, family):
    from smart_metadata import normalize_title
    key = normalize_title(BONUS.sub('', title or ''))
    for alias in sorted({family, *[a for a, f in ALIASES.items() if f == family]}, key=len, reverse=True):
        prefix = normalize_title(alias)
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    key = re.sub('^(?:剧场版|电影版|剧场|电影)', '', key)
    if len(key) >= 8:
        key = re.sub('(?:相遇|遭遇)$', '相遇', key)
    return key

def _verified_bonus_identity(worker, remote, evidence):
    from auto_versions import work_id
    family = evidence.get('family')
    wanted = _subtitle_key(evidence.get('bonus_title') or PurePosixPath(remote).stem, family)
    if len(wanted) < 5:
        return None
    bindings = read(worker.root / 'data/source-bindings.json')
    rows = worker.api('jellyfin', '/Items?Recursive=true&IncludeItemTypes=Movie&Fields=Path,ProviderIds')['Items']
    by_path = {str(Path(row['Path']).absolute()).casefold(): row for row in rows if row.get('Type') == 'Movie' and row.get('Path')}
    by_stream = {}
    for row in rows:
        match = re.search('\\[([a-f0-9]{12})\\]', Path(row.get('Path', '')).stem)
        if row.get('Type') == 'Movie' and match:
            by_stream[match[1]] = row
    profiles = read(worker.root / 'data/chinese-credits-profiles.json')
    matches = set()
    for feature in worker.entries.values():
        if feature.get('kind') != 'Movies' or feature.get('catalog_hidden') or feature.get('source_missing') or feature.get('attachment_role'):
            continue
        local = (worker.root / 'catalog' / catalog_relative(feature)).with_suffix('.strm')
        item = by_path.get(str(local.absolute()).casefold()) or by_stream.get(feature['id'][:12])
        tmdb = str((item or {}).get('ProviderIds', {}).get('Tmdb') or '')
        if not tmdb.isdecimal() or work_id(feature) != tmdb:
            continue
        binding = bindings.get(feature['id'], {})
        titles = []
        same_family = False
        paths = [feature.get('remote'), binding.get('original_remote'), *binding.get('previous_paths', [])]
        for path in dict.fromkeys((p for p in paths if p)):
            source = context(path)
            if source.get('family') != family or source.get('role') == 'bonus':
                continue
            same_family = True
            titles.extend(source.get('titles', []))
            identity = verified_source_identity(path)
            if identity and str(identity.get('provider_ids', {}).get('Tmdb')) == tmdb:
                titles.extend([identity.get('title'), *(identity.get('aliases') or [])])
        if not same_family:
            continue
        for profile in profiles.values():
            if isinstance(profile, dict) and str(profile.get('provider_ids', {}).get('Tmdb')) == tmdb:
                titles.extend([profile.get('title'), *(profile.get('aliases') or [])])
        if wanted in {_subtitle_key(title, family) for title in titles if isinstance(title, str)}:
            matches.add(tmdb)
    return next(iter(matches)) if len(matches) == 1 else None

def bonus_identity(remote, worker=None):
    evidence = context(remote)
    if evidence.get('role') != 'bonus':
        return None
    path = PurePosixPath(remote)
    for name in (path.parent.name, BONUS.sub('', path.stem).strip(' ._-')):
        if not name:
            continue
        identity = verified_source_identity(str(path.parent / (name + '.mkv')))
        if identity:
            return identity['provider_ids']['Tmdb']
    return _verified_bonus_identity(worker, remote, evidence) if worker is not None else None

def hide_bonus(worker, entry):
    remote = entry.get('attachment_original_remote') or entry['remote']
    feature = attachment_feature(worker, entry) if explicit_bonus(remote) or entry.get('attachment_role') == 'bonus' else None
    if entry.get('attachment_role') != 'bonus' and context(remote).get('role') != 'bonus' and (not (explicit_bonus(remote) and feature)):
        return False
    if entry.get('playback_active') or getattr(worker, 'active', {}).get(entry['id']) or getattr(worker, 'downloading', None) == entry['id']:
        return False
    ident = entry['id']
    if not re.fullmatch('[a-f0-9]{24}', ident):
        raise ValueError('Invalid attachment identity')
    root = worker.root
    journal_path = root / 'data/attachments' / (ident + '.json')
    journal = read(journal_path)
    local = cache_path(root / 'catalog', Path(catalog_relative(entry)).with_suffix('.strm').as_posix())
    if journal.get('phase') not in ('hidden', 'organized'):
        if local.exists() and '/stream/' + ident not in local.read_text('utf-8'):
            raise ValueError('Derived stream identity mismatch')
        if not journal:
            items = worker.api('jellyfin', '/Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=Path')['Items']
            matched = [item for item in items if item.get('Path') and Path(item['Path']).absolute() == local.absolute()]
            records = []
            if matched:
                ids = ','.join((item['Id'] for item in matched))
                for user in worker.api('jellyfin', '/Users'):
                    query = urllib.parse.urlencode({'UserId': user['Id'], 'Ids': ids, 'EnableUserData': 'true'})
                    for item in worker.api('jellyfin', '/Items?' + query).get('Items', []):
                        records.append({'user_id': user['Id'], 'item_id': item['Id'], 'data': item.get('UserData') or {}})
            files = []
            if local.parent.exists():
                from series_view import literal_sidecars
                files = [str(cache_path(root / 'catalog', p.relative_to(root / 'catalog').as_posix())) for p in literal_sidecars(local)]
            journal = {'phase': 'prepared', 'original_entry': dict(entry), 'records': records, 'files': files, 'created_at': time.time()}
            write(journal_path, journal)
        archive = root / 'data/attachments' / ident
        archive.mkdir(parents=True, exist_ok=True)
        for source in journal.get('files', []):
            file = cache_path(root / 'catalog', Path(source).absolute().relative_to((root / 'catalog').absolute()).as_posix())
            destination = archive / file.name
            if file.exists():
                if destination.exists():
                    raise ValueError('Attachment archive collision')
                file.rename(destination)
        journal.update(phase='hidden', updated_at=time.time())
        write(journal_path, journal)
    if not journal.get('native_notified'):
        from jellyfin_ingestion import notify_changed
        notify_changed(worker, paths=[local], update_type='Deleted')
        journal['native_notified'] = True
        write(journal_path, journal)
    tmdb = entry.get('attachment_to_tmdb') or bonus_identity(remote, worker)
    if feature:
        from auto_versions import work_id
        tmdb = work_id(feature) or tmdb
    with worker.lock:
        entry.update(attachment_role='bonus', attachment_original_remote=remote, catalog_hidden=True)
        entry.pop('identity_needs_review', None)
        if feature:
            entry['attachment_parent_id'] = feature['id']
        if tmdb:
            entry['attachment_to_tmdb'] = tmdb
        hidden = read(root / 'data/catalog-hidden.json')
        hidden[remote] = {'reason': 'bonus-attachment', 'fid': entry.get('source_fid'), 'attachment_to_tmdb': tmdb}
        write(root / 'data/catalog-hidden.json', hidden)
        worker.catalog_hidden = hidden
        worker.save()
    return True

def reconcile_bonus(worker):
    result = []
    for entry in list(worker.entries.values()):
        if entry.get('source_missing'):
            continue
        if hide_bonus(worker, entry):
            result.append(entry['id'])
    return result

def organize_bonus(worker, managers=None, entry_ids=None):
    """Call after feature normalization. Read only known source/target parents."""
    from auto_versions import work_id
    from cloud_organizer import Tree, apply_plan
    bindings = read(worker.root / 'data/source-bindings.json')
    by_source = {}
    done = []
    for entry in worker.entries.values():
        if entry.get('attachment_role') != 'bonus' or entry.get('source_missing'):
            continue
        parent_feature = attachment_feature(worker, entry)
        if entry_ids is not None and entry['id'] not in entry_ids and (not parent_feature or parent_feature['id'] not in entry_ids):
            continue
        if parent_feature and work_id(parent_feature):
            entry['attachment_to_tmdb'] = work_id(parent_feature)
        if not entry.get('attachment_to_tmdb'):
            continue
        if entry.get('playback_active') or getattr(worker, 'active', {}).get(entry['id']) or getattr(worker, 'downloading', None) == entry['id']:
            continue
        source = next((prefix for prefix in ('/夸克/movie', '/百度/movie') if entry['remote'].startswith(prefix + '/')), None)
        if not source:
            continue
        features = [e for e in worker.entries.values() if e['id'] != entry['id'] and (not e.get('catalog_hidden')) and (not e.get('source_missing')) and e.get('remote', '').startswith(source + '/') and (work_id(e) == str(entry['attachment_to_tmdb'])) and bindings.get(e['id'], {}).get('normalized')]
        if not features:
            continue
        feature = max(features, key=lambda e: (bool(e.get('quality_preferred')), e.get('created_at', 0)))
        entry['attachment_parent_id'] = feature['id']
        target_dir = str(PurePosixPath(feature['remote']).parent / '附加内容')
        if str(PurePosixPath(entry['remote']).parent) == target_dir:
            continue
        by_source.setdefault(source, []).append((entry, target_dir))
    for source, entries in by_source.items():
        if managers and source in managers:
            q = managers[source]
        elif source.startswith('/夸克/'):
            from quark_manage import Quark
            q = Quark(worker.root)
        else:
            from baidu_manage import Baidu
            q = Baidu(worker.root)
        journal = worker.root / 'data' / ('cloud-organizer-plan' + ('-' + q.slug if hasattr(q, 'slug') else '') + '.json')
        existing = read(journal)
        if existing.get('operations') and existing.get('phase') != 'complete':
            continue
        tree = Tree(q)
        operations = []
        planned_targets = set()
        for entry, target_dir in entries:
            fid = entry.get('source_fid') or bindings.get(entry['id'], {}).get('fid')
            if not fid:
                continue
            parent = str(PurePosixPath(entry['remote']).parent)
            actual = next((row for row in tree.rows(parent, fresh=True) if str(row.get('fid')) == str(fid) and (not row.get('dir'))), None)
            if not actual or actual.get('size') != entry['size']:
                continue
            feature_dir = str(PurePosixPath(target_dir).parent)
            attachment_dir = next((row for row in tree.rows(feature_dir) if row.get('dir') and row.get('file_name') == '附件'), None)
            target_rows = q.listing(attachment_dir['fid']) if attachment_dir else []
            name = actual['file_name']
            target = target_dir + '/' + name
            if target in planned_targets or any((row.get('file_name') == name and str(row.get('fid')) != str(fid) for row in target_rows)):
                p = PurePosixPath(name)
                name = p.stem + ' [' + entry['id'] + ']' + p.suffix
                target = target_dir + '/' + name
                if any((row.get('file_name') == name and str(row.get('fid')) != str(fid) for row in target_rows)):
                    continue
            operations.append({'entry_id': entry['id'], 'fid': actual['fid'], 'size': entry['size'], 'source': entry['remote'], 'target': target, 'old_name': actual['file_name'], 'source_parent': parent, 'kind': 'attachment', 'state': 'pending'})
            planned_targets.add(target)
        if not operations:
            continue
        plan = {'created_at': time.time(), 'phase': 'planned', 'operations': operations, 'review': [], 'ad_directories': [], 'directory_fids': tree.dirs, 'anchors': read(worker.root / 'data/cloud-normalization-rules.json'), 'subtitles_detected': 0}
        apply_plan(worker.root, q, worker, plan, tree)
        with worker.lock:
            worker.source_bindings.apply(worker.entries)
            worker.save()
        for operation in operations:
            path = worker.root / 'data/attachments' / (operation['entry_id'] + '.json')
            record = read(path)
            record.update(phase='organized', target=operation['target'], updated_at=time.time())
            write(path, record)
            done.append(operation['entry_id'])
    for entry in worker.entries.values():
        if entry.get('attachment_role') != 'bonus' or entry.get('source_missing'):
            continue
        feature = attachment_feature(worker, entry)
        if entry_ids is not None and entry['id'] not in entry_ids and (not feature or feature['id'] not in entry_ids):
            continue
        if feature and work_id(feature):
            publish_bonus(worker, entry, feature)
    worker.save()
    return done
