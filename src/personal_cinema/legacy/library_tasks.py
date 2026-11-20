"""Durable, bounded job observations. No credentials, cloud URLs or raw exceptions."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import json
import os
import secrets
import time

def read(path, default=None):
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default

def write(path, value):
    if _cinema_secret_write(path, value):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False), 'utf-8')
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)

def observe(root, worker, stage, title='', state='running', item_id=None, count=None, retry_at=None):
    """One record per worker: a crashed job remains visible with its last heartbeat."""
    if worker not in ('scan', 'metadata', 'credits', 'artwork', 'portraits'):
        raise ValueError('Unknown worker')
    prior = read(root / 'data/task-events' / (worker + '.json'))
    now = time.time()
    same = prior.get('state') == 'running' and prior.get('stage') == stage and (prior.get('item_id') == item_id)
    value = {'worker': worker, 'stage': stage, 'title': str(title)[:300], 'state': state, 'item_id': item_id, 'started_at': prior.get('started_at', now) if same else now, 'updated_at': now, 'count': count, 'retry_at': retry_at}
    write(root / 'data/task-events' / (worker + '.json'), value)
    return value

def safe_event(value, now=None):
    now = time.time() if now is None else now
    result = {k: value.get(k) for k in ('worker', 'stage', 'title', 'state', 'item_id', 'started_at', 'updated_at', 'count', 'total', 'retry_at')}
    if result.get('state') == 'running' and now - (result.get('updated_at') or 0) > 180:
        result['state'] = 'stalled'
    return result

def movie_title(path, parts, force_file=False):
    """A film in an anthology has its own folder/title, rather than the anthology's."""
    from personal_cinema.runtime_paths import RuntimePath as Path
    from smart_metadata import _label
    folder = parts[-2] if len(parts) > 2 else parts[0] if len(parts) > 1 else ''
    anthology = any((word in folder for word in ('合集', '合辑', '全集', '剧场版', '劇場版'))) and (not __import__('re').search('(?:19|20)\\d{2}', folder))
    if len(parts) > 2 or anthology or force_file:
        raw = Path(path).stem if anthology or force_file else folder
        title, year = _label(raw)
        if title:
            return title + (f' ({year})' if year else '')
    return folder or Path(path).stem
