"""Follow confirmed catalog names without changing cache IDs or partial blocks."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import time
import xml.etree.ElementTree as ET
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from library_tasks import read, write
from cache_subtitles import SUBTITLE_EXTENSIONS, subtitle_suffix, sync_ready as sync_cached_subtitles
SIDECARS = SUBTITLE_EXTENSIONS | {'.nfo', '.jpg', '.jpeg', '.png', '.webp'}

def busy(worker, entry):
    ident = entry['id']
    return bool(entry.get('playback_active') or entry.get('active') or worker.active.get(ident) or (getattr(worker, 'downloading', None) == ident) or (getattr(worker, 'transfer_entry_id', None) == ident) or getattr(worker, 'playback_status_unknown', False))

def desired(worker, entry):
    relative = entry.get('catalog_relative')
    if not relative or entry.get('identity_needs_review') or entry.get('source_missing'):
        return None
    catalog = cache_path(worker.root / 'catalog', Path(relative).with_suffix('.strm').as_posix())
    if entry.get('catalog_hidden'):
        tmdb = str(entry.get('series_tmdb') or entry.get('canonical_series_tmdb') or entry.get('canonical_movie_tmdb') or '')
        nfo = catalog.parent.parent / 'tvshow.nfo' if entry.get('kind') == 'Shows' else catalog.with_suffix('.nfo')
        try:
            node = ET.parse(nfo).getroot()
            proven = {str(n.text or '').strip() for n in node.findall('uniqueid') if n.get('type', '').lower() == 'tmdb'}
            if node.findtext('tmdbid'):
                proven.add(node.findtext('tmdbid').strip())
            if not tmdb.isdecimal() or tmdb not in proven:
                return None
        except (OSError, ET.ParseError):
            return None
    elif not catalog.is_file():
        return None
    confirmed = entry.get('canonical_movie_tmdb') or entry.get('series_tmdb') or entry.get('canonical_series_tmdb') or entry.get('catalog_rule_applied') or re.search('\\[tmdbid-\\d+\\]', relative)
    if not confirmed:
        nfo = catalog.with_suffix('.nfo') if entry.get('kind') == 'Movies' else catalog.parent.parent / 'tvshow.nfo'
        try:
            node = ET.parse(nfo).getroot()
            confirmed = any(((n.text or '').isdecimal() and n.get('type', '').lower() == 'tmdb' for n in node.findall('uniqueid'))) or bool(node.findtext('tmdbid'))
        except (OSError, ET.ParseError):
            return None
    if not confirmed:
        return None
    return cache_path(_cinema_cache_path('ready'), relative).relative_to(_cinema_cache_path('ready')).as_posix()

def prune_empty(root, path):
    root = Path(root).resolve()
    current = Path(path)
    while current.resolve() != root:
        checked = cache_path(root, current.absolute().relative_to(root).as_posix())
        try:
            checked.rmdir()
        except OSError:
            break
        current = checked.parent

def _finish(worker, journal, path):
    entry = worker.entries.get(journal['id'])
    if not entry or entry.get('size') != journal.get('size'):
        raise ValueError('缓存身份已变化，保留迁移日志待处理')
    root = _cinema_cache_path('ready')
    for move in journal['moves']:
        old = cache_path(root, move['old'])
        new = cache_path(root, move['new'])
        if old.exists():
            if new.exists():
                raise ValueError('目标已存在，未覆盖任何缓存文件')
            stat = old.stat()
            if stat.st_size != move['size'] or (move.get('inode') and stat.st_ino != move['inode']):
                raise ValueError('缓存文件已变化，未继续移动')
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
        elif not new.is_file():
            raise ValueError('缓存迁移缺少原文件，已保留记录')
        else:
            stat = new.stat()
            if stat.st_size != move['size'] or (move.get('inode') and stat.st_ino != move['inode']):
                raise ValueError('目标与迁移日志不符，未接管该文件')
    entry.update(relative=journal['target'], cache_relative=journal['target'], cache_name_updated_at=time.time())
    entry.pop('cache_name_error', None)
    entry.pop('cache_name_retry_at', None)
    worker.save()
    journal.update(state='complete', completed_at=time.time())
    write(path, journal)
    for move in journal['moves']:
        prune_empty(root, cache_path(root, move['old']).parent)
    sync_cached_subtitles(worker.root, [entry])
    return {'id': entry['id'], 'state': 'moved' if journal['moves'] else 'updated', 'from': journal['old_relative'], 'to': journal['target'], 'files': len(journal['moves'])}

def recover(worker):
    """Run before playback/download threads start, under the worker instance lock."""
    results = []
    folder = worker.root / 'data/cache-name-migrations'
    with worker.lock:
        for path in folder.glob('*.json'):
            journal = read(path)
            if journal.get('state') not in ('prepared', 'error'):
                continue
            entry = worker.entries.get(journal.get('id'))
            if not entry or busy(worker, entry):
                continue
            try:
                results.append(_finish(worker, journal, path))
            except (OSError, ValueError, KeyError) as error:
                entry['cache_name_error'] = type(error).__name__
                journal.update(state='error', error_type=type(error).__name__, updated_at=time.time())
                write(path, journal)
    return results

def rename_entry(worker, entry):
    with worker.lock:
        if busy(worker, entry):
            return {'id': entry['id'], 'state': 'waiting'}
        root = _cinema_cache_path('ready')
        target = desired(worker, entry)
        if not target:
            return {'id': entry['id'], 'state': 'retained' if entry.get('catalog_hidden') or entry.get('source_missing') else 'unconfirmed'}
        aliases = list(dict.fromkeys([entry.get('cache_relative') or entry['relative'], entry['relative']]))
        if all((name == target for name in aliases)):
            return {'id': entry['id'], 'state': 'unchanged'}
        path = worker.root / 'data/cache-name-migrations' / (entry['id'] + '.json')
        prior = read(path)
        if prior.get('state') in ('prepared', 'error'):
            return _finish(worker, prior, path)
        existing = [cache_path(root, name) for name in aliases if cache_path(root, name).is_file()]
        if len(existing) > 1:
            raise ValueError('两个旧缓存路径均有文件，未自动覆盖')
        old = existing[0] if existing else cache_path(root, aliases[0])
        new = cache_path(root, target)
        if entry.get('ready') and (not old.is_file()):
            raise ValueError('完整缓存的原文件缺失，未更改路径')
        if not old.is_file() and new.exists():
            raise ValueError('新路径已有未绑定文件，未接管或覆盖')
        moves = []
        if old.is_file():
            if old.stat().st_size != entry['size']:
                raise ValueError('缓存长度不符，未更改文件')
            files = [old] + [p for p in old.parent.iterdir() if p.is_file() and p != old and (p.suffix.lower() in SIDECARS) and (subtitle_suffix(p.name, old.stem) is not None if p.suffix.lower() in SUBTITLE_EXTENSIONS else p.name.startswith(old.stem) and p.name[len(old.stem):].startswith(('.', '-')))]
            for source in files:
                source = cache_path(root, source.relative_to(root).as_posix())
                destination = new if source == old else cache_path(root, (new.parent / (new.stem + source.name[len(old.stem):])).relative_to(root).as_posix())
                if source == destination:
                    continue
                if destination.exists():
                    raise ValueError('目标已存在，未覆盖任何缓存文件')
                stat = source.stat()
                moves.append({'old': source.relative_to(root).as_posix(), 'new': destination.relative_to(root).as_posix(), 'size': stat.st_size, 'inode': stat.st_ino})
        journal = {'id': entry['id'], 'size': entry['size'], 'state': 'prepared', 'created_at': time.time(), 'old_relative': entry['relative'], 'old_cache_relative': entry.get('cache_relative'), 'target': target, 'moves': moves}
        write(path, journal)
        return _finish(worker, journal, path)

def follow(worker, entry_ids=None, limit=None):
    if getattr(worker, 'ingestion_running', False):
        return []
    selected = set(entry_ids) if entry_ids is not None else None
    results = []
    for entry in list(worker.entries.values()):
        if selected is not None and entry['id'] not in selected:
            continue
        if selected is None and (not (entry.get('ready') or entry.get('partial_bytes_completed'))):
            continue
        if selected is None and entry.get('cache_name_retry_at', 0) > time.time():
            continue
        target = entry.get('catalog_relative')
        if not target or (selected is None and entry.get('relative') == target and (entry.get('cache_relative', target) == target)):
            continue
        try:
            result = rename_entry(worker, entry)
        except (OSError, ValueError, KeyError) as error:
            entry['cache_name_error'] = type(error).__name__
            result = {'id': entry['id'], 'state': 'error', 'error_type': type(error).__name__, 'message': str(error) if isinstance(error, ValueError) else '缓存暂未改名，原文件和进度已保留'}
        if result['state'] in ('error', 'unconfirmed', 'retained'):
            entry['cache_name_retry_at'] = time.time() + 600
        results.append(result)
        if limit and len(results) >= limit:
            break
    return results

def process_request(worker, path, payload):
    if getattr(worker, 'ingestion_running', False):
        return False
    selected = payload.get('ids')
    results = follow(worker, entry_ids=selected)
    receipt = worker.root / 'data/task-receipts' / (payload['request_id'] + '.json') if payload.get('request_id') else worker.root / 'data/cache-name-status.json'
    prior = read(receipt)
    records = {r['id']: r for r in prior.get('results', [])}
    records.update({r['id']: r for r in results})
    waiting = [r['id'] for r in results if r['state'] == 'waiting']
    failed = [r for r in records.values() if r['state'] in ('error', 'unconfirmed')]
    status = 'waiting' if waiting else 'partial' if failed else 'applied'
    write(receipt, {**prior, 'request_id': payload.get('request_id'), 'action': 'normalize-cache-names', 'state': status, 'message': '播放或缓存传输结束后继续规范名称' if waiting else '部分缓存名称待处理，原文件已保留' if failed else '缓存名称已与影视库同步，已有视频和下载进度保留', 'updated_at': time.time(), 'moved': sum((r['state'] == 'moved' for r in records.values())), 'updated': sum((r['state'] == 'updated' for r in records.values())), 'results': list(records.values())})
    if waiting:
        write(path, {**payload, 'ids': waiting})
        return False
    return True
