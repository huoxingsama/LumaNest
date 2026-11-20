"""Match explicit subtitle suffixes and recover sidecars from known old parents."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import re
import time
from pathlib import PurePosixPath
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from library_tasks import read, write
from library_routing import catalog_relative
from smart_metadata import normalize_title
from subtitle_rules import classify, suffix
SUB = {'.ass', '.ssa', '.srt', '.vtt', '.sub', '.idx', '.smi'}
LABEL = '(?:外挂字幕|外掛字幕|字幕|chs|cht|chi|zho|eng|jpn|ja|en|jp|und|zh[-_]?(?:cn|tw|hans|hant)|中日双语|中日雙語|简英双语|簡英雙語|繁英双语|繁英雙語|中英双语|中英雙語|日英双语|日英雙語|简体中文|簡體中文|繁体中文|繁體中文|中文字幕|日文字幕|英文字幕|日文|英文|未识别字幕|English|Japanese|default|forced|sdh)(?:·(?:次级注释|次級註釋|听障|聽障))?'
TAIL = re.compile('(?:[._\\s-]+' + LABEL + '|[\\[【(（]' + LABEL + '[\\]】)）])+$', re.I)

def subtitle_matches(name, video_names):
    if PurePosixPath(name).suffix.lower() not in SUB:
        return set()
    key = normalize_title(TAIL.sub('', PurePosixPath(name).stem))
    return {ident for ident, names in video_names.items() if any((normalize_title(PurePosixPath(value).stem) == key for value in names))} if key else set()

def publish_pending_subtitles(worker, entry_ids=None):
    """Replay the local sidecar/index transaction independently of cloud moves."""
    path = worker.root / 'data/subtitle-repair-local.json'
    state = read(path)
    files = state.get('files') or []
    if not files or state.get('native_notified'):
        return 0
    ready = []
    prefixes = [(worker.root / 'catalog' / catalog_relative(worker.entries[i])).with_suffix('').absolute() for i in entry_ids or [] if i in worker.entries] if entry_ids is not None else None
    for record in files:
        if record.get('native_notified'):
            continue
        if prefixes is not None:
            local = Path(record.get('local', '')).absolute()
            if not any((local.parent == p.parent and local.name.startswith(p.name + '.') for p in prefixes)):
                continue
        try:
            local = cache_path(worker.root / 'catalog', Path(record['local']).absolute().relative_to((worker.root / 'catalog').absolute()).as_posix())
            if not local.exists() and record.get('staging'):
                staged = cache_path(worker.root / 'data/subtitle-repair-staging', Path(record['staging']).absolute().relative_to((worker.root / 'data/subtitle-repair-staging').absolute()).as_posix())
                if staged.is_file() and record.get('sha256') and (hashlib.sha256(staged.read_bytes()).hexdigest() == record['sha256']):
                    local.parent.mkdir(parents=True, exist_ok=True)
                    staged.replace(local)
            if not local.is_file():
                continue
            if record.get('sha256') and hashlib.sha256(local.read_bytes()).hexdigest() != record['sha256']:
                continue
            ready.append((record, local))
        except (OSError, ValueError, KeyError):
            continue
    if ready:
        from jellyfin_ingestion import notify_changed
        notify_changed(worker, paths=[local for record, local in ready], update_type='Created')
        for record, local in ready:
            record['native_notified'] = True
        state['native_notified'] = all((record.get('native_notified') for record in files))
        state['notified_at'] = time.time()
        write(path, state)
    return len(ready)

def repair_known_subtitles(worker, managers=None, entry_ids=None):
    """No recursion: only surviving original/previous parents of known movies."""
    from cloud_organizer import Tree, apply_plan
    publish_pending_subtitles(worker, entry_ids=entry_ids)
    root = worker.root
    bindings = read(root / 'data/source-bindings.json')
    groups = {}
    updated = []
    for ident, binding in bindings.items():
        if entry_ids is not None and ident not in entry_ids:
            continue
        entry = worker.entries.get(ident)
        if not entry or not binding.get('normalized') or entry.get('kind') != 'Movies' or entry.get('source_missing') or entry.get('catalog_hidden'):
            continue
        current = str(PurePosixPath(entry['remote']).parent)
        for previous in set([binding.get('original_remote'), *binding.get('previous_paths', [])]):
            if not previous:
                continue
            parent = str(PurePosixPath(previous).parent)
            if parent == current:
                continue
            source = next((s for s in ('/夸克/movie', '/百度/movie') if parent.startswith(s + '/')), None)
            if source:
                groups.setdefault(source, {}).setdefault(parent, {}).setdefault(ident, []).append(PurePosixPath(previous).name)
    for source, parents in groups.items():
        if managers and source in managers:
            q = managers[source]
        elif source.startswith('/夸克/'):
            from quark_manage import Quark
            q = Quark(root)
        else:
            from baidu_manage import Baidu
            q = Baidu(root)
        journal = root / 'data' / ('cloud-organizer-plan' + ('-' + q.slug if hasattr(q, 'slug') else '') + '.json')
        prior = read(journal)
        if prior.get('operations') and prior.get('phase') != 'complete':
            continue
        tree = Tree(q)
        names = {row['file_name'] for row in tree.rows(source) if row.get('dir')}
        operations = []
        local_files = {}
        targets = set()
        for parent, videos in parents.items():
            if parent[len(source) + 1:].split('/')[0] not in names:
                continue
            try:
                rows = tree.rows(parent)
            except (OSError, ValueError, RuntimeError, KeyError):
                continue
            for row in rows:
                if row.get('dir') or not 0 < row.get('size', 0) <= 3000000:
                    continue
                matches = subtitle_matches(row['file_name'], videos)
                if len(matches) != 1:
                    continue
                ident = next(iter(matches))
                entry = worker.entries[ident]
                ext = PurePosixPath(row['file_name']).suffix.lower()
                if ext in ('.idx', '.sub'):
                    continue
                remote = parent + '/' + row['file_name']
                try:
                    with worker.http.open(worker.cloud_request(remote), timeout=20) as response:
                        data = response.read(3000001)
                    if len(data) != row['size']:
                        continue
                except OSError:
                    continue
                info = classify(data, row['file_name'])
                ending = suffix(info, ext, default=info['language'] == 'chi' and (not info['secondary']) and (not info.get('traditional')))
                dest_parent = str(PurePosixPath(entry['remote']).parent)
                base = PurePosixPath(entry['remote']).stem
                destination = dest_parent + '/' + base + ending
                occupied = tree.rows(dest_parent)
                if destination in targets or any((r['file_name'] == PurePosixPath(destination).name and str(r['fid']) != str(row['fid']) for r in occupied)):
                    ending = '.' + hashlib.sha256(str(row['fid']).encode()).hexdigest()[:8] + suffix(info, ext, default=False)
                    destination = dest_parent + '/' + base + ending
                if destination in targets or any((r['file_name'] == PurePosixPath(destination).name and str(r['fid']) != str(row['fid']) for r in occupied)):
                    continue
                targets.add(destination)
                staging = root / 'data/subtitle-repair-staging' / (hashlib.sha256((source + str(row['fid'])).encode()).hexdigest() + ext)
                staging.parent.mkdir(parents=True, exist_ok=True)
                staging.write_bytes(data)
                local = cache_path(root / 'catalog', Path(catalog_relative(entry)).with_suffix('').as_posix() + ending)
                if local.exists() and local.read_bytes() != data:
                    staging.unlink(missing_ok=True)
                    continue
                local_files[str(row['fid'])] = (staging, local)
                operations.append({'video_entry_id': ident, 'fid': row['fid'], 'size': row['size'], 'source': remote, 'target': destination, 'source_parent': parent, 'old_name': row['file_name'], 'kind': 'subtitle', 'subtitle': info, 'state': 'pending'})
        if not operations:
            continue
        plan = {'created_at': time.time(), 'phase': 'planned', 'operations': operations, 'review': [], 'ad_directories': [], 'directory_fids': tree.dirs, 'anchors': read(root / 'data/cloud-normalization-rules.json'), 'subtitles_detected': len(operations)}
        changed = []
        write(root / 'data/subtitle-repair-local.json', {'created_at': time.time(), 'native_notified': False, 'files': [{'fid': fid, 'local': str(local), 'staging': str(staging), 'sha256': hashlib.sha256(staging.read_bytes()).hexdigest()} for fid, (staging, local) in local_files.items()]})
        for operation in operations:
            staging, local = local_files[str(operation['fid'])]
            local.parent.mkdir(parents=True, exist_ok=True)
            if not local.exists():
                staging.replace(local)
            else:
                staging.unlink(missing_ok=True)
            changed.append(local)
        publish_pending_subtitles(worker, entry_ids=entry_ids)
        apply_plan(root, q, worker, plan, tree)
        for operation in operations:
            updated.append({'fid': operation['fid'], 'entry_id': operation['video_entry_id'], 'target': operation['target'], 'subtitle': operation['subtitle']})
    if updated:
        write(root / 'data/subtitle-repair-state.json', {'updated_at': time.time(), 'updated': updated})
    return updated
