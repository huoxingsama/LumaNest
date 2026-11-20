"""Loopback Jellyfin adapter: report original sizes for managed STRM media.

Authentication remains Jellyfin's responsibility. Requests go only to the fixed
loopback upstream, including redirects, binary streams and WebSocket upgrades.
"""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import http.client
import json
import logging
import os
import re
import select
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_status_api import serve_status
from people_localization import localize_people
from external_ratings import CachedRatings
from media_probe import ProbeCache
from task_center_api import route as task_route, augment_views, authorize as task_authorize, auth_headers as task_headers, APIError
ROOT = Path(os.environ.get('MEDIA_LIBRARY_ROOT', _cinema_path('')))
DEFAULT_STREAM_BASE = _cinema_endpoint('stream')
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade'}
MAX_JSON = 32 * 1024 * 1024
MAX_REQUEST = 16 * 1024 * 1024

def managed_source(path, entries, base_url):
    if not isinstance(path, str):
        return None
    try:
        url = urllib.parse.urlsplit(path)
        base = urllib.parse.urlsplit(base_url)
        match = re.fullmatch('/stream/([a-f0-9]{24})', url.path)
        if match and (url.scheme, url.hostname, url.port) == (base.scheme, base.hostname, base.port):
            return entries.get(match[1])
    except ValueError:
        pass

def source_label(entry):
    raw = entry.get('remote', '')
    source = '百度' if raw.startswith('/百度/') else '夸克' if raw.startswith('/夸克/') else '影视库'
    quality = '蓝光原盘' if re.search('blu.?ray', raw, re.I) and re.search('remux', raw, re.I) else '原画'
    state = ' · 已在本地' if entry.get('ready') else ' · 源文件缺失' if entry.get('source_missing') else ''
    return f"{source} · {quality} · {entry.get('size', 0) / 1000000000.0:.2f} GB" + state

def correct_sizes(value, entries, stream_base_url=DEFAULT_STREAM_BASE):
    """Copy JSON, replacing only the size of an identified managed source."""
    if isinstance(value, list):
        return [correct_sizes(item, entries, stream_base_url) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: correct_sizes(item, entries, stream_base_url) for key, item in value.items()}
    if isinstance(result.get('MediaSources'), list):
        sources = result['MediaSources']

        def rank(source):
            e = managed_source(source.get('Path'), entries, stream_base_url)
            return (not e.get('quality_preferred'), not e.get('ready'), bool(e.get('source_missing')), bool(e.get('download_error')), not bool(re.search('remux', e.get('remote', ''), re.I)), -e.get('size', 0)) if e else (True, True, False, False, True, 0)
        good = [s for s in sources if not (managed_source(s.get('Path'), entries, stream_base_url) or {}).get('source_missing') or (managed_source(s.get('Path'), entries, stream_base_url) or {}).get('ready')]
        result['MediaSources'] = sorted(good or sources, key=rank)
        managed = [managed_source(s.get('Path'), entries, stream_base_url) for s in sources]
        keys = {re.search('\\[tmdbid-(\\d+)\\]', e.get('catalog_relative', '')).group(1) for e in managed if e and e.get('kind') == 'Movies' and re.search('\\[tmdbid-(\\d+)\\]', e.get('catalog_relative', ''))}
        if len(keys) == 1 and all((e and e.get('kind') == 'Movies' and re.search('\\[tmdbid-(\\d+)\\]', e.get('catalog_relative', '')) for e in managed)):
            result['MediaSources'] = result['MediaSources'][:1]
            if 'MediaSourceCount' in result:
                result['MediaSourceCount'] = 1
    keys = {key.lower(): key for key in result}
    if not {'path', 'size', 'protocol'}.issubset(keys):
        return result
    path = result[keys['path']]
    if not isinstance(path, str):
        return result
    url = urllib.parse.urlsplit(path)
    base = urllib.parse.urlsplit(stream_base_url)
    try:
        same_origin = (url.scheme, url.hostname, url.port) == (base.scheme, base.hostname, base.port)
    except ValueError:
        return result
    match = re.fullmatch('/stream/([a-f0-9]{24})', url.path)
    if same_origin and match and (match[1] in entries):
        size = entries[match[1]].get('size')
        if isinstance(size, int) and size > 0:
            result[keys['size']] = size
            if 'name' in keys:
                result[keys['name']] = source_label(entries[match[1]])
    return result

class Catalog:

    def __init__(self, root=ROOT):
        self.root = root
        self.lock = threading.Lock()
        self.checked_at = 0
        self.entries = {}
        self.base_url = DEFAULT_STREAM_BASE
        self.people_rules = {}
        self.ratings = CachedRatings(root)
        self.media_probe = ProbeCache(root)

    def snapshot(self):
        with self.lock:
            if time.monotonic() - self.checked_at >= 1:
                try:
                    entries = json.loads((self.root / 'data/cache-state.json').read_text(encoding='utf-8'))
                    config = json.loads((self.root / 'data/cache-config.json').read_text(encoding='utf-8'))
                    if isinstance(entries, dict):
                        self.entries = entries
                        self.base_url = config['stream_base_url']
                    rules = self.root / 'data/people-localization.json'
                    if rules.exists():
                        value = json.loads(rules.read_text('utf-8'))
                        if isinstance(value, dict):
                            self.people_rules = value
                except (OSError, ValueError, KeyError):
                    logging.warning('Catalogue refresh unavailable; retaining last known sizes')
                self.checked_at = time.monotonic()
            return (self.entries, self.base_url)

def handler_for(catalog, upstream_port=8096):

    class Handler(BaseHTTPRequestHandler):

        def log_message(self, *_):
            pass

        def do_GET(self):
            self.proxy()

        def do_HEAD(self):
            self.proxy()

        def do_POST(self):
            self.proxy()

        def do_DELETE(self):
            self.proxy()

        def do_PUT(self):
            self.proxy()

        def do_PATCH(self):
            self.proxy()

        def do_OPTIONS(self):
            self.proxy()

        def read_body(self):
            if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
                result = bytearray()
                while True:
                    line = self.rfile.readline(128)
                    size = int(line.split(b';')[0].strip(), 16)
                    if size == 0:
                        while self.rfile.readline(8192).strip():
                            pass
                        return bytes(result)
                    if len(result) + size > MAX_REQUEST:
                        raise ValueError('Request too large')
                    data = self.rfile.read(size)
                    if len(data) != size or self.rfile.read(2) != b'\r\n':
                        raise ValueError('Invalid chunked request')
                    result.extend(data)
            length = int(self.headers.get('Content-Length', '0'))
            if length < 0 or length > MAX_REQUEST:
                raise ValueError('Request too large')
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError('Incomplete request')
            return data

        def tunnel_websocket(self):
            self.close_connection = True
            with _cinema_jellyfin_socket(timeout=15) as target:
                base = urllib.parse.urlsplit(_cinema_endpoint('jellyfin'))
                request = f'{self.command} {base.path.rstrip("/")}{self.path} HTTP/1.1\r\n'
                request += f'Host: {base.netloc}\r\n'
                request += ''.join((f'{name}: {value}\r\n' for name, value in self.headers.items() if name.lower() != 'host')) + '\r\n'
                target.sendall(request.encode('iso-8859-1'))
                target.settimeout(None)
                self.connection.settimeout(None)
                while True:
                    readable, _, _ = select.select([target, self.connection], [], [], 60)
                    for source in readable:
                        data = source.recv(64 * 1024)
                        if not data:
                            return
                        (self.connection if source is target else target).sendall(data)

        def proxy(self):
            if not self.path.startswith('/') or self.path.startswith('//'):
                self.send_error(400)
                return
            if task_route(self, catalog, upstream_port):
                return
            if urllib.parse.urlsplit(self.path).path == '/MediaCache/Status':
                if self.command not in ('GET', 'HEAD'):
                    self.send_error(405)
                    return
                serve_status(self, catalog, upstream_port)
                return
            if self.headers.get('Upgrade', '').lower() == 'websocket':
                try:
                    self.tunnel_websocket()
                except (OSError, ValueError):
                    self.close_connection = True
                return
            connection = None
            started = False
            try:
                body = self.read_body()
                conditional_api = any((part in urllib.parse.urlsplit(self.path).path.lower() for part in ('/items', '/useritems', '/shows')))
                headers = {name: value for name, value in self.headers.items() if name.lower() not in HOP_HEADERS | {'host', 'content-length', 'accept-encoding'} and (not (conditional_api and name.lower() in {'if-none-match', 'if-modified-since'}))}
                headers['Accept-Encoding'] = 'identity'
                connection = _cinema_jellyfin_connection(timeout=120)
                connection.request(self.command, self.path, body=body if body else None, headers=headers)
                response = connection.getresponse()
                is_json = 'application/json' in (response.getheader('Content-Type') or '').lower() and (not response.getheader('Content-Encoding'))
                content = None
                if is_json and self.command != 'HEAD':
                    content = response.read(MAX_JSON + 1)
                    if len(content) <= MAX_JSON:
                        try:
                            original = json.loads(content)
                            entries, base_url = catalog.snapshot()
                            corrected = correct_sizes(original, entries, base_url)
                            corrected = catalog.media_probe.apply(corrected, entries, base_url, self.path, enqueue=response.status == 200)
                            corrected = localize_people(corrected, catalog.people_rules)
                            corrected = catalog.ratings.apply(corrected)
                            if response.status == 200:
                                if urllib.parse.urlsplit(self.path).path.lower() == '/userviews' or re.fullmatch('/Users/[^/]+/Views', urllib.parse.urlsplit(self.path).path, re.I):
                                    try:
                                        user = task_authorize(catalog.root, upstream_port, task_headers(self.headers, urllib.parse.urlsplit(self.path).query))
                                        corrected = augment_views(corrected, self.path, catalog.root, user['Id'])
                                    except APIError:
                                        pass
                                else:
                                    corrected = augment_views(corrected, self.path, catalog.root)
                            content = json.dumps(corrected, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                        except (ValueError, RecursionError):
                            pass
                self.send_response(response.status, response.reason)
                for name, value in response.getheaders():
                    lower = name.lower()
                    if lower in HOP_HEADERS or (content is not None and lower == 'content-length') or (is_json and lower in {'etag', 'last-modified', 'cache-control'}):
                        continue
                    self.send_header(name, value)
                if is_json:
                    self.send_header('Cache-Control', 'no-store')
                if content is not None and len(content) <= MAX_JSON:
                    self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                started = True
                if self.command != 'HEAD':
                    if content is not None:
                        self.wfile.write(content)
                    while True:
                        data = response.read(256 * 1024)
                        if not data:
                            break
                        self.wfile.write(data)
            except ValueError:
                if not started:
                    self.send_error(400, 'Invalid request')
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except OSError:
                logging.warning('Jellyfin upstream connection unavailable')
                if not started:
                    self.send_error(502, 'Media server temporarily unavailable')
            finally:
                if connection:
                    connection.close()
    return Handler
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', filename=ROOT / 'logs/metadata-proxy.log', encoding='utf-8')
    from personal_cinema.runtime_paths import bind_host
    ThreadingHTTPServer((bind_host('gateway'), _cinema_port('gateway')), handler_for(Catalog())).serve_forever()
