"""Targeted native indexing notifications and progress-aware batch readiness."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import time
import urllib.parse
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from library_routing import catalog_relative
from library_tasks import read, write

def path_key(path):
    return str(Path(path).absolute()).replace('\\', '/').casefold()

def entry_path(worker, entry):
    relative = str(Path(catalog_relative(entry)).with_suffix('.strm')).replace('\\', '/')
    return cache_path(worker.root / 'catalog', relative)

def notify_changed(worker, entries=None, paths=None, update_type='Modified'):
    """Report specific catalog paths instead of refreshing every library."""
    if update_type not in ('Created', 'Modified', 'Deleted'):
        raise ValueError('Unsupported native update type')
    catalog = (worker.root / 'catalog').absolute()
    selected = []
    seen = set()
    for path in list(paths or []) + [entry_path(worker, e) for e in entries or []]:
        path = Path(path).absolute()
        relative = path.relative_to(catalog).as_posix()
        safe = cache_path(catalog, relative)
        key = path_key(safe)
        if key in seen:
            continue
        seen.add(key)
        selected.append({'Path': str(safe), 'UpdateType': update_type})
    for offset in range(0, len(selected), 100):
        worker.api('jellyfin', '/Library/Media/Updated', {'Updates': selected[offset:offset + 100]}, method='POST')
    return len(selected)

def task_progress(tasks):
    task = next((t for t in tasks if t.get('Key') == 'RefreshLibrary'), {})
    return (task.get('State') == 'Running', float(task.get('CurrentProgressPercentage') or 0))

def indexed_paths(worker, kinds):
    query = '/Items?Recursive=true&IncludeItemTypes=' + ','.join(sorted(kinds)) + '&Fields=Path,MediaSources'
    rows = worker.api('jellyfin', query).get('Items', [])
    paths = set()
    shorts = set()
    for row in rows:
        sources = row.get('MediaSources') or []
        candidates = [row.get('Path'), row.get('LocalPath')] + [s.get(key) for s in sources for key in ('Path', 'LocalPath')]
        for path in candidates:
            if not path or re.match('https?://', path, re.I):
                continue
            paths.add(path_key(path))
            found = re.search('\\[([a-f0-9]{12})\\]', Path(path).stem)
            if found:
                shorts.add(found[1])
        primary = row.get('LocalPath') or row.get('Path')
        if not primary or re.match('https?://', primary, re.I):
            continue
        for source in sources:
            if not source.get('Id') or source['Id'] == row.get('Id'):
                continue
            try:
                url = urllib.parse.urlsplit(source.get('Path') or '')
                base = urllib.parse.urlsplit(worker.config.get('stream_base_url') or '')
                match = re.fullmatch('/stream/([a-f0-9]{24})', url.path)
                if not match or base.scheme not in ('http', 'https') or (url.scheme, url.hostname, url.port) != (base.scheme, base.hostname, base.port):
                    continue
                ident = match[1]
                entry = worker.entries.get(ident)
                if not entry or entry.get('source_missing') or entry.get('catalog_hidden') or entry.get('catalog_unavailable_archived'):
                    continue
                local = entry_path(worker, entry)
                if path_key(local.parent) != path_key(Path(primary).parent) or not local.is_file():
                    continue
            except (ValueError, TypeError, OSError):
                continue
            paths.add(path_key(local))
            shorts.add(ident[:12])
    return (paths, shorts)

def wait_for_index(worker, entries=None, paths=None, idle_timeout=180, poll_seconds=2, clock=None, sleep=None, allow_stable_alias=False):
    """Wait only for this batch; advancing work extends the idle deadline.

    A pending result is durable and does not discard other completed ingestion
    work. There is no fixed three-minute wall-clock failure while the server is
    still progressing through a larger global scan.
    """
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    selected = [e for e in entries or [] if not e.get('source_missing') and (not e.get('catalog_hidden'))]
    wanted = {path_key(entry_path(worker, e)): e.get('id', '')[:12] for e in selected}
    wanted.update({path_key(p): '' for p in paths or []})
    kinds = {'Movie' if e.get('kind') == 'Movies' else 'Episode' for e in selected}
    if paths and (not kinds):
        kinds = {'Movie', 'Episode'}
    started = last_progress = clock()
    best_indexed = -1
    previous_native = -1.0
    state_path = worker.root / 'data/manual-ingestion.json'
    while True:
        native_running, native_percent = task_progress(worker.api('jellyfin', '/ScheduledTasks'))
        if wanted:
            registered, shorts = indexed_paths(worker, kinds)
            pending = [path for path, short in wanted.items() if path not in registered and (not (allow_stable_alias and short and (short in shorts)))]
            indexed = len(wanted) - len(pending)
            complete = not pending
        else:
            pending = []
            indexed = 0
            complete = not native_running
        now = clock()
        if indexed > best_indexed or native_percent > previous_native + 0.01:
            last_progress = now
            best_indexed = max(best_indexed, indexed)
        previous_native = native_percent
        result = {'complete': complete, 'indexed': indexed, 'total': len(wanted), 'pending_paths': pending, 'native_scan_running': native_running, 'native_scan_percent': round(native_percent, 2), 'elapsed_seconds': round(now - started, 1), 'idle_seconds': round(now - last_progress, 1), 'updated_at': time.time()}
        write(worker.root / 'data/ingestion-index-status.json', result)
        current = read(state_path)
        if current.get('state') == 'running':
            write(state_path, {**current, 'stage': 'index', 'updated_at': time.time(), 'index_progress': result, 'note': f'本批已建立索引 {indexed}/{len(wanted)}' + (f'；媒体库扫描 {native_percent:.1f}%' if native_running else '') if wanted else f'媒体库扫描 {native_percent:.1f}%'})
        if complete:
            return result
        if now - last_progress >= idle_timeout:
            return {**result, 'reason': 'no-progress'}
        sleep(poll_seconds)
