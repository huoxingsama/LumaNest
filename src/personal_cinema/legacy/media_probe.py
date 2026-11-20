from personal_cinema.runtime_paths import proxy_handler
from personal_cinema.network_security import credential_opener
"""Bounded, cached source-file metadata for managed STRM entries."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import contextlib
import hashlib
import http.server
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from fractions import Fraction
from personal_cinema.runtime_paths import RuntimePath as Path
from library_tasks import read, write
_LOCK = threading.RLock()
_WORKERS = {}
_BUDGET = 12 * 1024 * 1024

def identity(entry):
    remote = str(entry.get('remote') or '')
    source = remote.strip('/').split('/', 1)[0]
    revision = {k: entry[k] for k in ('source_revision', 'source_hash', 'source_md5', 'source_sha1') if entry.get(k)}
    return hashlib.sha256(json.dumps([entry['id'], entry.get('size'), entry.get('source_fid'), source if entry.get('source_fid') else remote, revision], sort_keys=True).encode()).hexdigest()

def eligible(entry):
    return bool(entry and (not entry.get('source_missing')) and (not entry.get('catalog_hidden')) and (entry.get('size', 0) > 0) and (entry.get('relative') or entry.get('catalog_relative')))

@contextlib.contextmanager
def state_lock(root):
    import msvcrt
    with _LOCK:
        path = Path(root) / 'data/media-probe.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+b') as stream:
            stream.seek(0)
            stream.write(b'0')
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

def queue_entries(root, entries, entry_ids=None, prune=False):
    root = Path(root)
    selected = list(entry_ids) if entry_ids is not None else list(entries)
    count = 0
    path = root / 'data/media-probe-state.json'
    with state_lock(root):
        state = read(path)
        rows = state.setdefault('entries', {})
        queue = state.setdefault('queue', [])
        if prune:
            active = {i for i, e in entries.items() if eligible(e)}
            for ident in set(rows) - active:
                rows.pop(ident, None)
            queue[:] = [i for i in queue if i in active]
        queued = set(queue)
        for ident in selected:
            entry = entries.get(ident)
            if not eligible(entry):
                continue
            key = identity(entry)
            prior = rows.get(ident, {})
            if prior.get('key') == key and (prior.get('state') == 'complete' or prior.get('retry_at', 0) > time.time()):
                continue
            if prior.get('key') != key or prior.get('state') not in ('pending', 'running'):
                rows[ident] = {'key': key, 'state': 'pending', 'updated_at': time.time()}
            if ident not in queued:
                queue.append(ident)
                queued.add(ident)
                count += 1
        write(path, state)
    worker = _WORKERS.get(str(root.absolute()))
    if worker:
        worker.wake.set()
    return count

def _number(value):
    try:
        return float(value) if value not in (None, 'N/A', '') else None
    except (ValueError, TypeError):
        return None

def _int(value):
    value = _number(value)
    return int(value) if value is not None and value >= 0 else None

def _fps(value):
    try:
        result = float(Fraction(str(value)))
        return result if result > 0 else None
    except (ValueError, ZeroDivisionError):
        return None

def translate(payload):
    fmt = payload.get('format') or {}
    streams = []
    for raw in payload.get('streams') or []:
        if (raw.get('disposition') or {}).get('attached_pic'):
            continue
        kind = {'video': 'Video', 'audio': 'Audio', 'subtitle': 'Subtitle'}.get(raw.get('codec_type'))
        if not kind:
            continue
        tags = raw.get('tags') or {}
        disposition = raw.get('disposition') or {}
        stream = {'Index': raw['index'], 'Type': kind, 'Codec': raw.get('codec_name'), 'IsDefault': bool(disposition.get('default')), 'IsForced': bool(disposition.get('forced')), 'IsExternal': False}
        mapping = {'codec_long_name': 'CodecDescription', 'profile': 'Profile', 'pix_fmt': 'PixelFormat', 'color_space': 'ColorSpace', 'color_primaries': 'ColorPrimaries', 'color_transfer': 'ColorTransfer', 'color_range': 'ColorRange', 'channel_layout': 'ChannelLayout', 'field_order': 'FieldOrder', 'codec_tag_string': 'CodecTag'}
        for source, target in mapping.items():
            if raw.get(source) not in (None, 'unknown', 'N/A'):
                stream[target] = raw[source]
        for source, target in [('width', 'Width'), ('height', 'Height'), ('bit_rate', 'BitRate'), ('sample_rate', 'SampleRate'), ('channels', 'Channels'), ('level', 'Level')]:
            value = _int(raw.get(source))
            if value is not None:
                stream[target] = value
        if 'BitRate' not in stream:
            rate = next((_int(v) for k, v in tags.items() if k.upper() in ('BPS', 'BPS-ENG') and _int(v)), None)
            if rate:
                stream['BitRate'] = rate
        depth = _int(raw.get('bits_per_raw_sample')) or _int(raw.get('bits_per_sample'))
        if not depth:
            match = re.search('(?:p|yuv\\d+p)(\\d+)(?:le|be)$', raw.get('pix_fmt', ''))
            if match:
                depth = int(match[1])
        if depth:
            stream['BitDepth'] = depth
        for source, target in [('avg_frame_rate', 'AverageFrameRate'), ('r_frame_rate', 'RealFrameRate')]:
            value = _fps(raw.get(source))
            if value:
                stream[target] = value
        for source, target in [('language', 'Language'), ('title', 'Title')]:
            if tags.get(source):
                stream[target] = tags[source]
        transfer = raw.get('color_transfer')
        side = raw.get('side_data_list') or []
        dolby = next((s for s in side if 'dovi' in s.get('side_data_type', '').lower()), None)
        if kind == 'Video':
            if raw.get('field_order') == 'progressive':
                stream['IsInterlaced'] = False
            elif raw.get('field_order') in ('tt', 'bb', 'tb', 'bt'):
                stream['IsInterlaced'] = True
            if dolby:
                stream.update(VideoRange='HDR', VideoRangeType='DOVI')
                for source, target in [('dv_profile', 'DvProfile'), ('dv_level', 'DvLevel')]:
                    if source in dolby:
                        stream[target] = dolby[source]
            elif any(('2094' in s.get('side_data_type', '') or 'HDR10+' in s.get('side_data_type', '') for s in side)):
                stream.update(VideoRange='HDR', VideoRangeType='HDR10Plus')
            elif transfer == 'smpte2084':
                stream.update(VideoRange='HDR', VideoRangeType='HDR10')
            elif transfer == 'arib-std-b67':
                stream.update(VideoRange='HDR', VideoRangeType='HLG')
            elif transfer in ('bt709', 'smpte170m', 'smpte240m', 'iec61966-2-1', 'bt470m', 'bt470bg'):
                stream.update(VideoRange='SDR', VideoRangeType='SDR')
        streams.append({k: v for k, v in stream.items() if v is not None})
    if not any((s['Type'] == 'Video' for s in streams)):
        raise ValueError('No video metadata returned')
    result = {'MediaStreams': streams}
    container = (fmt.get('format_name') or '').split(',')[0]
    if container:
        result['Container'] = {'matroska': 'mkv'}.get(container, container)
    rate = _int(fmt.get('bit_rate'))
    duration = _number(fmt.get('duration'))
    if rate:
        result['Bitrate'] = rate
    if duration and duration > 0:
        result['RunTimeTicks'] = round(duration * 10000000)
    return result

def _playing(root):
    status = read(Path(root) / 'data/cache-status.json')
    return bool(status.get('playback_streaming', status.get('playback_exclusive')))

class _BudgetBridge:
    """Present a seekable file to ffprobe while bounding actual upstream bytes."""

    def __init__(self, root, entry):
        self.entry = entry
        self.used = 0
        self.requests = 0
        self.lock = threading.Lock()
        key = (Path(root) / 'secrets/stream-key.txt').read_text().strip()
        self.url = _cinema_endpoint('stream') + '/stream/' + entry['id'] + '?' + urllib.parse.urlencode({'key': key})
        self.http = credential_opener(proxy_handler())
        self.skips = []
        try:
            header = self.read_range(0, min(entry['size'] - 1, 65535))

            def vint(pos, ident=False):
                first = header[pos]
                n = next((n for n in range(1, 9) if first & 1 << 8 - n))
                if pos + n > len(header):
                    raise ValueError('Incomplete EBML header')
                value = int.from_bytes(header[pos:pos + n], 'big')
                return (value if ident else value & (1 << 7 * n) - 1, n)
            pos = 0
            for _ in range(40):
                if pos + 12 > len(header):
                    break
                element, n = vint(pos, True)
                length, m = vint(pos + n)
                header_len = n + m
                if element == 423732329:
                    for width in range(1, min(8, header_len - 1) + 1):
                        payload = length + header_len - 1 - width
                        if payload >= 0 and payload < (1 << 7 * width) - 1:
                            replacement = b'\xec' + (1 << 7 * width | payload).to_bytes(width, 'big')
                            replacement += b'\x00' * (header_len - len(replacement))
                            self.skips.append((pos, pos + header_len + length, replacement))
                            break
                if element == 408125543:
                    pos += header_len
                else:
                    pos += header_len + length
        except (OSError, ValueError, IndexError, StopIteration):
            pass
        token = secrets.token_hex(16)
        bridge = self

        class Handler(http.server.BaseHTTPRequestHandler):

            def log_message(self, *args):
                pass

            def do_HEAD(self):
                if self.path != '/' + token:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(entry['size']))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()

            def do_GET(self):
                if self.path != '/' + token:
                    self.send_error(404)
                    return
                value = self.headers.get('Range', 'bytes=0-')
                match = re.fullmatch('bytes=(\\d+)-(\\d*)', value)
                if not match:
                    self.send_error(416)
                    return
                start = int(match[1])
                size = entry['size']
                with bridge.lock:
                    bridge.requests += 1
                    available = _BUDGET - bridge.used
                    if bridge.requests > 8 or available <= 0 or start >= size:
                        self.send_error(429)
                        return
                    end = min(size - 1, int(match[2]) if match[2] else size - 1, start + 4 * 1024 * 1024 - 1)
                try:
                    self.send_response(206)
                    self.send_header('Content-Length', str(end - start + 1))
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                    self.send_header('Accept-Ranges', 'bytes')
                    self.end_headers()
                    pos = start
                    while pos <= end:
                        skip = next((s for s in bridge.skips if s[0] <= pos < s[1]), None)
                        if skip:
                            offset = pos - skip[0]
                            data = skip[2][offset:min(len(skip[2]), offset + end - pos + 1)] if offset < len(skip[2]) else b'\x00' * min(65536, end - pos + 1, skip[1] - pos)
                        else:
                            stop = min([end] + [s[0] - 1 for s in bridge.skips if s[0] > pos])
                            data = bridge.read_range(pos, min(stop, pos + 65535))
                        if not data:
                            break
                        self.wfile.write(data)
                        pos += len(data)
                except (OSError, ValueError):
                    pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.input = 'http://127.0.0.1:' + str(self.server.server_port) + '/' + token

    def read_range(self, start, end):
        length = end - start + 1
        with self.lock:
            if self.used + length > _BUDGET:
                raise OSError('Probe byte budget reached')
            self.used += length
        request = urllib.request.Request(self.url, headers={'Range': f'bytes={start}-{end}'})
        with self.http.open(request, timeout=7) as response:
            if response.status != 206 or response.headers.get('Content-Range') != f"bytes {start}-{end}/{self.entry['size']}":
                raise ValueError('Source range mismatch')
            result = response.read(length)
            if len(result) != length:
                raise OSError('Incomplete probe range')
            return result

    def close(self):
        self.server.shutdown()
        self.server.server_close()

def probe_entry(root, entry):
    root = Path(root)
    from personal_cinema.runtime_paths import tool_path
    ffprobe = Path(tool_path('ffprobe'))
    if not ffprobe.is_file():
        raise OSError('ffprobe unavailable')
    relative = entry.get('cache_relative') or entry['relative']
    from cache_policy import cache_path
    local = cache_path(_cinema_cache_path('ready'), relative)
    complete = bool(entry.get('ready') and local.is_file() and (local.stat().st_size == entry['size']))
    if not complete and _playing(root):
        return {'state': 'deferred', 'retry_at': time.time() + 30, 'note': '等待当前播放结束后读取源视频信息'}
    bridge = None
    process = None
    try:
        if complete:
            source = str(local)
        else:
            bridge = _BudgetBridge(root, entry)
            source = bridge.input
        args = [str(ffprobe), '-v', 'error', '-probesize', '1048576', '-analyzeduration', '250000', '-fpsprobesize', '0', '-show_streams', '-show_format', '-of', 'json']
        if bridge:
            args += ['-rw_timeout', '7000000']
        args.append(source)
        process = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=22, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if len(process.stdout) > 4 * 1024 * 1024:
            raise ValueError('Probe output exceeds budget')
        data = translate(json.loads(process.stdout))
        return {'state': 'complete', 'result': data, 'updated_at': time.time(), 'bytes_read': bridge.used if bridge else 0, 'note': '已读取源视频媒体信息'}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        diagnostic = process.stderr.decode('utf-8', 'replace').replace(bridge.input, '[probe input]')[:1000] if process is not None and bridge else type(error).__name__
        return {'state': 'error', 'retry_at': time.time() + 300, 'updated_at': time.time(), 'note': '源视频信息暂未读全，可稍后重试', 'error_type': type(error).__name__, 'diagnostic': diagnostic, 'bytes_read': bridge.used if bridge else 0}
    finally:
        if bridge:
            bridge.close()

def process_one(root, ident):
    root = Path(root)
    entry = read(root / 'data/cache-state.json').get(ident)
    path = root / 'data/media-probe-state.json'
    if not eligible(entry):
        with state_lock(root):
            state = read(path)
            state['queue'] = [i for i in state.get('queue', []) if i != ident]
            state.setdefault('entries', {}).pop(ident, None)
            write(path, state)
        return {'state': 'removed'}
    key = identity(entry)
    with state_lock(root):
        state = read(path)
        state.setdefault('entries', {})[ident] = {'key': key, 'state': 'running', 'updated_at': time.time()}
        write(path, state)
    result = probe_entry(root, entry)
    with state_lock(root):
        state = read(path)
        current = state.setdefault('entries', {}).get(ident, {})
        latest = read(root / 'data/cache-state.json').get(ident)
        if not eligible(latest):
            state['entries'].pop(ident, None)
            state['queue'] = [i for i in state.get('queue', []) if i != ident]
            write(path, state)
            return {'state': 'removed'}
        latest_key = identity(latest)
        if latest_key != key:
            state['entries'][ident] = {'key': latest_key, 'state': 'pending', 'updated_at': time.time(), 'note': '源文件已更换，正在重新读取媒体信息'}
            if ident not in state.setdefault('queue', []):
                state['queue'].append(ident)
            write(path, state)
            return {'state': 'pending'}
        if current.get('key') == key:
            state['entries'][ident] = {'key': key, **result}
            if result['state'] == 'complete':
                state['queue'] = [i for i in state.get('queue', []) if i != ident]
            elif ident not in state.setdefault('queue', []):
                state['queue'].append(ident)
            write(path, state)
    return result

class ProbeCache:

    def __init__(self, root):
        self.root = Path(root)
        self.wake = threading.Event()
        self.stamp = None
        self.rows = {}
        self.catalog_stamp = None
        self.catalog_keys = None
        _WORKERS[str(self.root.absolute())] = self
        self.wake.set()
        threading.Thread(target=self._run, name='source-media-probe', daemon=True).start()

    def _sync_catalog(self):
        path = self.root / 'data/cache-state.json'
        stamp = path.stat().st_mtime_ns
        if stamp == self.catalog_stamp:
            return
        entries = json.loads(path.read_text('utf-8'))
        if not isinstance(entries, dict):
            return
        keys = {i: identity(e) for i, e in entries.items() if eligible(e)}
        if keys != self.catalog_keys:
            queue_entries(self.root, entries, prune=True)
            self.catalog_keys = keys
        self.catalog_stamp = stamp

    def _run(self):
        while True:
            self.wake.wait(15)
            self.wake.clear()
            try:
                self._sync_catalog()
                state = read(self.root / 'data/media-probe-state.json')
                rows = state.get('entries', {})
                ident = next((i for i in state.get('queue', []) if rows.get(i, {}).get('retry_at', 0) <= time.time()), None)
                if ident:
                    result = process_one(self.root, ident)
                    if result.get('state') in ('complete', 'removed'):
                        self.wake.set()
            except (OSError, ValueError, KeyError):
                pass

    def apply(self, value, entries, base_url, request_path, enqueue=True):
        path = self.root / 'data/media-probe-state.json'
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp != self.stamp:
            self.rows = read(path).get('entries', {})
            self.stamp = stamp
        url = urllib.parse.urlsplit(request_path)
        single = bool(re.fullmatch('(?:/Users/[^/]+)?/Items/[a-f0-9]{32}(?:/PlaybackInfo)?', url.path, re.I))
        catalog = {str((self.root / 'catalog' / (e.get('catalog_relative') or e['relative'])).with_suffix('.strm').absolute()).casefold(): e for e in entries.values() if e.get('catalog_relative') or e.get('relative')}
        wanted = set()

        def display_streams(probed, original):
            streams = list(probed)
            for stream in original:
                if stream.get('IsExternal') and stream not in streams:
                    streams.append(stream)
            return streams

        def transform(obj):
            if isinstance(obj, list):
                return [transform(v) for v in obj]
            if not isinstance(obj, dict):
                return obj
            result = {k: transform(v) for k, v in obj.items()}
            source = obj.get('Path')
            entry = None
            if isinstance(source, str):
                parsed = urllib.parse.urlsplit(source)
                base = urllib.parse.urlsplit(base_url)
                match = re.fullmatch('/stream/([a-f0-9]{24})', parsed.path)
                if match and (parsed.scheme, parsed.hostname, parsed.port) == (base.scheme, base.hostname, base.port):
                    entry = entries.get(match[1])
                elif not parsed.scheme or re.match('^[A-Za-z]:', source):
                    entry = catalog.get(str(Path(source).absolute()).casefold())
            if entry:
                record = self.rows.get(entry['id'], {})
                if record.get('key') != identity(entry):
                    record = {}
                if single and enqueue and (record.get('state') != 'complete'):
                    wanted.add(entry['id'])
                result.update(MediaProbeStatus=record.get('state', 'pending'), MediaProbeUpdatedAt=record.get('updated_at'), MediaProbeNote=record.get('note', '正在排队读取源视频媒体信息'), OriginalFileName=Path(entry.get('remote', '')).name, OriginalFilePath=entry.get('remote'))
                if record.get('state') == 'complete':
                    metadata = record['result']
                    result.update({k: v for k, v in metadata.items() if k != 'MediaStreams'})
                    result['MediaProbeStreams'] = display_streams(metadata.get('MediaStreams', []), result.get('MediaStreams') or [])
            sources = result.get('MediaSources') or []
            if sources:
                selected = sources[0]
                for k in ('MediaProbeStatus', 'MediaProbeUpdatedAt', 'MediaProbeNote', 'OriginalFileName', 'OriginalFilePath', 'RunTimeTicks', 'Bitrate', 'Container'):
                    if k in selected:
                        result[k] = selected[k]
                if 'MediaProbeStreams' in selected:
                    result['MediaProbeStreams'] = display_streams(selected['MediaProbeStreams'], result.get('MediaStreams') or [])
            return result
        result = transform(value)
        if wanted:
            queue_entries(self.root, entries, wanted)
        return result
