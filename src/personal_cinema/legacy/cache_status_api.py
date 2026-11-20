"""Read-only cache status for the current authenticated Jellyfin user."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import http.client
import json
import math
import time
from library_activity import activities

def number(value, default=0):
    if isinstance(value, (int, float)) and (not isinstance(value, bool)) and math.isfinite(value):
        return max(0, value)
    return default

def playback_buffer_status(value, entries, stale=False):
    """Publish the disk ring separately from persistent whole-film downloads."""
    if not isinstance(value, dict):
        return None
    limit = number(value.get('limit_bytes'))
    used = number(value.get('pool_bytes'))
    reserved = number(value.get('reserved_bytes'))

    def current(item):
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            return None
        entry = entries.get(item['id'], {})
        title = entry.get('display_title') or item.get('title') or entry.get('series') or '当前影片'
        if not isinstance(title, str):
            title = '当前影片'
        remote = entry.get('remote', '')
        source = '百度' if isinstance(remote, str) and remote.startswith('/百度/') else '夸克' if isinstance(remote, str) and remote.startswith('/夸克/') else item.get('source', '')
        if source not in ('百度', '夸克'):
            source = ''
        phase = item.get('phase')
        if phase not in ('watching', 'paused', 'grace', 'stopped', 'error'):
            phase = 'stopped'
        return {'id': item['id'][:128], 'title': title[:400], 'source': source, 'phase': 'unavailable' if stale else phase, 'ahead_bytes': min(number(item.get('ahead_bytes')), used), 'cursor_bytes': number(item.get('cursor_bytes')), 'reading': bool(item.get('reading')) and (not stale), 'readers': int(number(item.get('readers')))}
    active = value.get('active')
    return {'enabled': bool(value.get('enabled')), 'available': bool(value.get('available')) and (not stale), 'pool_bytes': used, 'limit_bytes': limit, 'reserved_bytes': reserved, 'current': current(value.get('current')), 'active': [item for raw in active[:32] if (item := current(raw))] if isinstance(active, list) else [], 'error': '后台状态暂不可用' if stale else '磁盘缓冲暂不可用' if value.get('error') else None}

def public_status(root, entries, now=None):
    now = time.time() if now is None else now
    try:
        status = json.loads((root / 'data/cache-status.json').read_text('utf-8'))
        config = json.loads((root / 'data/cache-config.json').read_text('utf-8'))
        if not isinstance(status, dict) or not isinstance(config, dict):
            raise ValueError()
    except (OSError, ValueError):
        return {'phase': 'unavailable', 'updated_at': None, 'current': None, 'cache': {}}
    updated = number(status.get('updated_at'))
    stale = not updated or now - updated > max(90, 3 * number(config.get('poll_seconds'), 30))
    buffer = playback_buffer_status(status.get('playback_buffer'), entries, stale)
    buffered_current = (buffer or {}).get('current')
    buffering = bool(buffered_current and buffered_current['phase'] in ('watching', 'paused', 'grace', 'error'))
    ident = status.get('downloading')
    entry = entries.get(ident, {}) if isinstance(ident, str) else {}
    if stale:
        phase = 'unavailable'
    elif status.get('downloads_paused') or config.get('downloads_paused'):
        phase = 'paused'
    elif status.get('steam_downloading'):
        phase = 'steam'
    elif status.get('playback_network_priority'):
        phase = 'playback'
    elif ident:
        phase = 'downloading'
    elif status.get('last_error'):
        phase = 'error'
    elif status.get('playback_exclusive'):
        phase = 'playback'
    else:
        phase = 'idle'
    work = activities(root, now)
    playback_current = bool(entry.get('playback_active') or status.get('playback_network_priority') or status.get('playback_exclusive') or buffering)
    if not stale and playback_current:
        phase = 'playback'
    if work and (not playback_current):
        phase = 'library'
    current = None
    if phase == 'downloading':
        size = number(entry.get('size'))
        done = min(number(status.get('downloaded_bytes')), size) if size else 0
        title = entry.get('display_title') or entry.get('series') or '正在读取影片信息'
        if not isinstance(title, str):
            title = '正在读取影片信息'
        remote = entry.get('remote', '')
        source = '百度' if isinstance(remote, str) and remote.startswith('/百度/') else '夸克' if isinstance(remote, str) and remote.startswith('/夸克/') else ''
        current = {'title': title[:400], 'season': number(entry.get('season')) if entry.get('season') is not None else None, 'episode': number(entry.get('episode')) if entry.get('episode') is not None else None, 'source': source, 'downloaded_bytes': done, 'size': size, 'percent': round(done * 100 / size, 1) if size else 0, 'mode': 'background'}
    return {'phase': phase, 'updated_at': max([updated] + [v['updated_at'] for v in work]) or None, 'current': current, 'activities': work, 'activity': work[0] if work else None, 'background_yielding': bool(work and (not playback_current)), 'playback_buffer': buffer, 'cache': {'bytes': number(status.get('cache_bytes')), 'limit': number(status.get('max_cache_bytes'), number(config.get('max_cache_bytes'))), 'ready_items': int(number(status.get('ready_items')))}}

def serve_status(handler, catalog, upstream_port):
    headers = {name: value for name, value in handler.headers.items() if name.lower() in {'authorization', 'x-emby-token', 'x-mediabrowser-token'}}
    value = {'error': 'Unauthorized'}
    status = 401
    if headers:
        connection = _cinema_jellyfin_connection(timeout=5)
        try:
            connection.request('GET', '/Users/Me', headers=headers)
            response = connection.getresponse()
            raw = response.read(65537)
            if response.status >= 500:
                value = {'error': 'Status service temporarily unavailable'}
                status = 503
            user = json.loads(raw) if response.status == 200 and len(raw) <= 65536 else {}
            if isinstance(user, dict) and user.get('Id') and (not (user.get('Policy') or {}).get('IsDisabled')):
                entries, _ = catalog.snapshot()
                value = public_status(catalog.root, entries)
                status = 200
        except (OSError, ValueError, http.client.HTTPException):
            value = {'error': 'Status service temporarily unavailable'}
            status = 503
        finally:
            connection.close()
    content = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(content)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.end_headers()
    if handler.command != 'HEAD':
        handler.wfile.write(content)
