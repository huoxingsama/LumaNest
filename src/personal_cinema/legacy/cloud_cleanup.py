"""Durable, bounded cleanup of confirmed former media directories."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import threading
import time
from pathlib import PurePosixPath
from library_tasks import read, write
LOCK = threading.RLock()
from source_policy import source_roots, source_entries, manager_for, journal_path, plan_id, _permission
AD = re.compile('广告|廣告|引流|扫码|掃碼|加群|进群|進群|公众号|公眾號|微信|QQ群|领取|領取|更多资源|更多資源|https?[:：]|www\\.', re.I)
AD_EXT = {'.txt', '.url', '.webloc', '.html', '.htm'}

def _load(root):
    state = read(root / 'data/cloud-cleanup-queue.json')
    state.setdefault('directories', {})
    state.setdefault('refresh', {})
    return state

def _save(root, state):
    state['updated_at'] = time.time()
    write(root / 'data/cloud-cleanup-queue.json', state)

def _enqueue(state, path, source, reason='empty-after-organization', force=False, fid=None):
    if not path.startswith(source + '/'):
        return
    prior = state['directories'].get(path)
    departed = set((prior or {}).get('departed_fids', []))
    if fid:
        departed.add(str(fid))
    if prior and (not force):
        prior['departed_fids'] = sorted(departed)
        return
    state['directories'][path] = {**(prior or {}), 'source': source, 'reason': reason, 'state': 'pending', 'retry_at': 0, 'updated_at': time.time(), 'departed_fids': sorted(departed)}

def _ancestors(path, source):
    while path.startswith(source + '/'):
        yield path
        path = str(PurePosixPath(path).parent)

def queue_plan_cleanup(root, plan, source):
    """Called only after every video/sidecar move and source binding succeeded."""
    if any((op.get('state') != 'done' for op in plan.get('operations', []))):
        return
    bindings = read(root / 'data/source-bindings.json')
    for op in plan.get('operations', []):
        if op.get('entry_id'):
            binding = bindings.get(op['entry_id'], {})
            if binding.get('phase') != 'done' or binding.get('remote') != op['target']:
                return
    with LOCK:
        state = _load(root)
        for op in plan.get('operations', []):
            old = str(PurePosixPath(op['source']).parent)
            target = str(PurePosixPath(op['target']).parent)
            if old != target:
                for path in _ancestors(old, source):
                    _enqueue(state, path, source, force=not plan.get('cleanup_enqueued_at'), fid=op.get('fid'))
            for path in (old, target):
                state['refresh'][path] = {'retry_at': 0, 'attempts': 0}
        for ad in plan.get('ad_directories', []):
            _enqueue(state, ad['source'], source, 'advertisement-directory', force=not plan.get('cleanup_enqueued_at'))
        plan['cleanup_enqueued_at'] = time.time()
        _save(root, state)

def _protected(worker, bindings):
    sources = source_roots(worker.root)
    result = set(sources)
    for ident, row in bindings.items():
        entry = getattr(worker, 'entries', {}).get(ident, {})
        if entry.get('source_retired_at') or entry.get('catalog_unavailable_archived'):
            continue
        remote = row.get('remote', '')
        for source in sources:
            if remote.startswith(source + '/'):
                result.update(_ancestors(str(PurePosixPath(remote).parent), source))
    for source in sources:
        for op in read(journal_path(worker.root, source)).get('operations', []):
            if op.get('state') == 'done':
                continue
            for remote in (op.get('source', ''), op.get('target', '')):
                for source in sources:
                    if remote.startswith(source + '/'):
                        result.update(_ancestors(str(PurePosixPath(remote).parent), source))
    return result

def _flush_refresh(worker, state, limit=32):
    attempted = 0
    for path, row in list(state['refresh'].items()):
        if row.get('retry_at', 0) > time.time():
            continue
        if attempted >= limit:
            break
        attempted += 1
        try:
            result = worker.api('openlist', '/api/fs/list', {'path': path, 'page': 1, 'per_page': 100, 'refresh': True})
            if result.get('code') != 200:
                raise RuntimeError('listing refresh unavailable')
            state['refresh'].pop(path, None)
        except (OSError, ValueError, RuntimeError, KeyError):
            row['attempts'] = row.get('attempts', 0) + 1
            row['retry_at'] = time.time() + min(1800, 30 * 2 ** min(row['attempts'], 6))

def cleanup_old_directories(worker, seed=True, limit=24):
    """Only old paths recorded in bindings/plans; no media-root traversal."""
    sources = []
    for entry in source_entries(worker.root):
        if entry.get('kind') != 'openlist' or not entry.get('enabled', True):
            continue
        approval = plan_id(read(journal_path(worker.root, entry['id'])))
        try:
            _permission(worker.root, entry, cleanup=True, approval=approval)
        except RuntimeError:
            continue
        sources.append(entry['path'])
    if not sources:
        return []
    with LOCK:
        root = worker.root
        state = _load(root)
        bindings = read(root / 'data/source-bindings.json')
        removed = []
        if seed:
            for row in bindings.values():
                current = row.get('remote', '')
                for previous in [row.get('original_remote'), *row.get('previous_paths', [])]:
                    if not previous or previous == current:
                        continue
                    for source in sources:
                        if previous.startswith(source + '/'):
                            for path in _ancestors(str(PurePosixPath(previous).parent), source):
                                _enqueue(state, path, source, fid=row.get('fid'))
        protected = _protected(worker, bindings)
        _flush_refresh(worker, state)
        trees = {}
        reads = 0
        processed = 0

        def tree_for(source):
            if source not in trees:
                from cloud_organizer import Tree
                manager = manager_for(worker, source)
                manager.approval = plan_id(read(journal_path(worker.root, source)))
                trees[source] = Tree(manager)
            return trees[source]

        def directory(tree, path):
            if path == tree.source_root:
                return tree.dirs[path]
            parent = str(PurePosixPath(path).parent)
            parent_fid = directory(tree, parent)
            if parent_fid is None:
                return None
            if parent not in tree.cache:
                tree.cache[parent] = tree.q.listing(parent_fid)
            matches = [r for r in tree.cache[parent] if r.get('dir') and r['file_name'] == PurePosixPath(path).name]
            if not matches:
                return None
            if len(matches) != 1:
                raise RuntimeError('ambiguous old directory')
            tree.dirs[path] = matches[0]['fid']
            return matches[0]['fid']

        def prune(tree, path, fid, advert=False, depth=0):
            nonlocal reads
            if path in protected:
                return False
            if depth > 24 or reads >= 96:
                raise RuntimeError('cleanup batch limit')
            reads += 1
            rows = tree.q.listing(fid)
            files = [r for r in rows if not r.get('dir')]
            departed = set(state['directories'].get(path, {}).get('departed_fids', []))
            if any((str(r.get('fid')) in departed for r in files)):
                raise RuntimeError('cloud move listing not yet settled')
            if files:
                return False
            for row in rows:
                if row.get('dir') and (not prune(tree, path + '/' + row['file_name'], row['fid'], advert or bool(AD.search(row['file_name'])), depth + 1)):
                    return False
            if rows:
                reads += 1
                if tree.q.listing(fid):
                    return False
            write(root / 'data/task-events/cleanup.json', {'worker': 'cleanup', 'stage': 'cleanup', 'state': 'running', 'title': path[len(tree.source_root):].strip('/'), 'updated_at': time.time()})
            tree.q.recycle([fid])
            removed.append(path)
            state['directories'][path] = {**state['directories'].get(path, {}), 'source': tree.source_root, 'state': 'removed', 'updated_at': time.time()}
            state['refresh'].pop(path, None)
            parent = str(PurePosixPath(path).parent)
            state['refresh'][parent] = {'retry_at': 0, 'attempts': 0}
            tree.cache.pop(parent, None)
            tree.cache.pop(path, None)
            tree.dirs.pop(path, None)
            _save(root, state)
            return True
        for path in sorted(state['directories'], key=lambda p: (-p.count('/'), p)):
            row = state['directories'][path]
            if row.get('state') in ('removed', 'gone') or row.get('retry_at', 0) > time.time():
                continue
            if processed >= limit:
                break
            if path in protected:
                row.update(state='protected', retry_at=time.time() + 300)
                continue
            source = row.get('source')
            if source not in sources or not path.startswith(source + '/'):
                continue
            processed += 1
            try:
                tree = tree_for(source)
                fid = directory(tree, path)
                if fid is None:
                    row.update(state='gone', updated_at=time.time())
                    state['refresh'].pop(path, None)
                    parent = str(PurePosixPath(path).parent)
                    while parent != source and directory(tree, parent) is None:
                        parent = str(PurePosixPath(parent).parent)
                    state['refresh'][parent] = {'retry_at': 0, 'attempts': 0}
                elif not prune(tree, path, fid, row.get('reason') == 'advertisement-directory'):
                    row.update(state='preserved', blocked_reason='contains-current-or-unknown-content', retry_at=time.time() + 21600, updated_at=time.time())
            except (OSError, ValueError, RuntimeError, KeyError, StopIteration) as error:
                row['attempts'] = row.get('attempts', 0) + 1
                row.update(state='retry', error=type(error).__name__, retry_at=time.time() + min(1800, 30 * 2 ** min(row['attempts'], 6)))
            _save(root, state)
        _flush_refresh(worker, state)
        pending = sum((r.get('state') in ('pending', 'retry') for r in state['directories'].values())) + len(state['refresh'])
        _save(root, state)
        write(root / 'data/task-events/cleanup.json', {'worker': 'cleanup', 'stage': 'cleanup', 'state': 'waiting' if pending else 'idle', 'title': '旧目录清理等待自动重试' if pending else '旧空目录清理完成', 'updated_at': time.time(), 'pending': pending})
        if removed:
            write(root / 'data/cloud-empty-cleanup.json', {'updated_at': time.time(), 'removed': removed})
        return removed

def retry_due(worker):
    """Resume recorded cleanup only; manual-only discovery stays disabled."""
    if getattr(worker, 'ingestion_running', False):
        return
    if getattr(worker, 'playback_present', False) or getattr(worker, 'playback_status_unknown', False) or getattr(worker, 'steam_downloading', False):
        return
    if time.time() - getattr(worker, 'last_cleanup_retry', 0) < 30:
        return
    worker.last_cleanup_retry = time.time()
    state = _load(worker.root)
    now = time.time()
    due = any((r.get('state') in ('pending', 'retry') and r.get('retry_at', 0) <= now for r in state['directories'].values()))
    due = due or any((r.get('retry_at', 0) <= now for r in state['refresh'].values()))
    if due:
        cleanup_old_directories(worker, seed=False, limit=8)
