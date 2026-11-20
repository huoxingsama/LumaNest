from personal_cinema.runtime_paths import proxy_handler
from personal_cinema.network_security import credential_opener, media_opener
"""Small adapter between OpenList, a bounded local cache and Jellyfin STRM files.

Cloud data is read-only: this program never issues a remote write/delete request.
The HTTP listener is loopback-only and is published privately using Tailscale Serve.
"""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import argparse
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from personal_cinema.runtime_paths import RuntimePath as Path
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from cache_policy import cache_path, delete_cached, eviction_ids, remote_path
from library_routing import catalog_relative, load as load_routes, route_to_anime, restore_progress
from smart_cache import cache_plan, apply_playback, session_entries
from cache_report import render_report
from range_cache import download_ranges, DownloadPaused
from steam_gate import SteamGate
from absolute_series import classify_absolute, write_absolute_metadata
from catalog_rules import classify_override, retain_cache_path
from library_tasks import observe, movie_title, write as atomic_write
from catalog_intelligence import episode_label, season_folder, directory_evidence, RULES
from source_bindings import Bindings, classify_normalized
from cache_subtitles import SUBTITLE_EXTENSIONS, subtitle_suffix, sync_ready as sync_cached_subtitles
ROOT = Path(os.environ.get('MEDIA_LIBRARY_ROOT', _cinema_path('')))
VIDEO = {'.mp4', '.mkv', '.mov', '.avi', '.m4v', '.ts', '.m2ts', '.webm', '.rmvb', '.rm', '.flv'}
SUBTITLE = SUBTITLE_EXTENSIONS
API_JSON_LIMIT = 16 * 1024 * 1024


def read_api_json(response):
    content = response.read(API_JSON_LIMIT + 1)
    if len(content) > API_JSON_LIMIT:
        raise ValueError('服务 JSON 响应超过 16 MiB，已停止读取')
    return json.loads(content) if content else None

def clean_name(value):
    value = re.sub('[<>:"：/\\\\|?*\\x00-\\x1f]', '_', value).strip(' .')
    if not value or re.match('^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\\.|$)', value, re.I):
        value = '_' + value
    return value[:160]

def configured_sources(config):
    roots = config.get('source_roots', [config.get('source_root', '/夸克/movie')])
    if not isinstance(roots, list) or not roots or any((not isinstance(value, str) for value in roots)):
        raise ValueError('Configure a nonempty list of media source folders')
    roots = list(dict.fromkeys((remote_path(value) for value in roots)))
    if '/' in roots or any((left != right and right.startswith(left + '/') for left in roots for right in roots)):
        raise ValueError('Choose separate media folders, not an entire drive or nested roots')
    return roots

def classify(path, source_root, series_rules=(), numbered_directory=False, multiple_movies=False):
    confirmed = classify_absolute(path, series_rules)
    if confirmed:
        return confirmed
    suffix = Path(path).suffix.lower()
    parts = path[len(source_root):].strip('/').split('/')
    title = clean_name(parts[0] if len(parts) > 1 else Path(parts[0]).stem)
    episode = re.search('S(\\d{1,2})E(\\d{1,5})(?!\\d)', Path(path).stem, re.I)
    if episode:
        season, number = map(int, episode.groups())
        from season_release_groups import series_release_title
        title = clean_name(series_release_title(title) or title)
        extra_title = None
        for folder in parts[1:-1]:
            spinoff = re.search('(?:番外|衍生).*?《([^》]+)》', folder)
            if spinoff:
                extra_title = spinoff[1]
                season = 0
        result = {'kind': 'Shows', 'series': title, 'season': season, 'episode': number, 'relative': f'Shows/{title}/Season {season:02}/{title} S{season:02}E{number:02}{suffix}'}
        if extra_title:
            result['extra_title'] = extra_title
        return result
    inferred = episode_label(Path(path).name, numbered_directory or season_folder(parts[:-1]) is not None)
    if inferred:
        from season_release_groups import series_release_title
        title = clean_name(series_release_title(title) or title)
        season = inferred['season'] if inferred['season'] is not None else season_folder(parts[:-1])
        review = season is None
        season = 99 if review else season
        number = inferred['episode']
        return {'kind': 'Shows', 'series': title, 'season': season, 'episode': number, 'identity_needs_review': review, 'recognition_rule': 'episode-labels', 'recognition_evidence': inferred['format'] + '；' + ('总集数／季内集数待核对' if review else '季目录已确认'), 'relative': f'Shows/{title}/Season {season:02}/{title} S{season:02}E{number:04}{suffix}'}
    if multiple_movies:
        title = clean_name(movie_title(path, [parts[0], Path(path).name] if len(parts) > 1 else parts, force_file=True))
    else:
        title = clean_name(movie_title(path, parts))
    from franchise_context import context, verified_source_identity
    evidence = context(path)
    identity = verified_source_identity(path)
    if identity:
        title = clean_name(identity['title'])
    elif evidence.get('titles'):
        title = clean_name(evidence['titles'][0])
    relative = f'Movies/{title}/{title}{suffix}'
    return {'kind': 'Movies', 'series': title, 'relative': relative, 'catalog_relative': relative, 'recognition_rule': 'movie-boundary', 'recognition_evidence': '同目录存在多部电影，按文件名拆分' if multiple_movies else '最近的作品目录／独立影片文件名'}

def unique_relative(relative, ident):
    path = Path(relative)
    return path.with_name(path.stem + ' [' + ident[:12] + ']' + path.suffix).as_posix()

def valid_ready(path, entry):
    return bool(entry.get('cached_at') is not None and path.is_file() and (path.stat().st_size == entry['size']))

def reclaim_partial(root, relative):
    delete_cached(root, relative)

def admission_evictions(items, now, max_bytes, disk_free, reserve_bytes, retention_seconds, incoming):
    return eviction_ids(items, now, max_bytes - incoming, disk_free, reserve_bytes + incoming, retention_seconds)

def byte_range(header, size):
    if not header:
        return None
    match = re.fullmatch('bytes=(\\d*)-(\\d*)', header)
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError('Invalid range')
    left, right = match.groups()
    if left:
        start, end = (int(left), min(int(right), size - 1) if right else size - 1)
    else:
        start, end = (max(0, size - int(right)), size - 1)
    if start > end or start >= size:
        raise ValueError('Unsatisfiable range')
    return (start, end)

def next_downloads(entries, next_episodes, now=None):
    now = time.time() if now is None else now
    result = sorted((item for item in entries if item.get('queued') and (not item.get('ready')) and (item.get('watched_at') is None) and (item.get('retry_download_at', 0) <= now)), key=lambda item: item.get('queued_at', 0), reverse=True)
    selected = {item['id'] for item in result}
    shows = {}
    for item in entries:
        if item['kind'] == 'Shows' and item.get('watched_at') is None:
            shows.setdefault(item['series'], []).append(item)
    for group in shows.values():
        for item in sorted(group, key=lambda value: (value.get('season', 0) == 0, value.get('season', 0), value.get('episode', 0)))[:next_episodes]:
            if not item.get('ready') and item['id'] not in selected and (item.get('retry_download_at', 0) <= now):
                result.append(item)
                selected.add(item['id'])
    return result

def write_catalog(root, entry, base_url, key):
    if entry.get('catalog_hidden') or entry.get('attachment_role') == 'bonus':
        return
    changed = False
    path = cache_path(root, str(Path(catalog_relative(entry)).with_suffix('.strm')).replace('\\', '/'))
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"{base_url.rstrip('/')}/stream/{entry['id']}?key={key}\n"
    if not path.exists() or path.read_text(encoding='utf-8') != text:
        path.write_text(text, encoding='utf-8')
        changed = True
    write_absolute_metadata(root, path, entry)
    from episode_review_metadata import clear_local_review
    if clear_local_review(root, entry):
        changed = True
    if entry.get('identity_needs_review') and entry.get('kind') == 'Shows' and isinstance(entry.get('episode'), int):
        for p, tag, fields in [(path.parent / 'season.nfo', 'season', [('title', '待核对集数'), ('seasonnumber', 99)]), (path.with_suffix('.nfo'), 'episodedetails', [('title', '第' + str(entry['episode']) + '集（编号待核对）'), ('season', 99), ('episode', entry['episode'])])]:
            if not p.exists():
                node = ET.Element(tag)
                for name, value in fields:
                    ET.SubElement(node, name).text = str(value)
                ET.SubElement(node, 'lockdata').text = 'true'
                ET.ElementTree(node).write(p, encoding='utf-8', xml_declaration=True)
    if entry.get('extra_title'):
        nfo = cache_path(root, path.parent.relative_to(root).as_posix() + '/season.nfo')
        if not nfo.exists():
            node = ET.Element('season')
            ET.SubElement(node, 'title').text = '番外 · ' + entry['extra_title']
            ET.SubElement(node, 'seasonnumber').text = '0'
            ET.SubElement(node, 'lockdata').text = 'true'
            try:
                with nfo.open('xb') as output:
                    ET.ElementTree(node).write(output, encoding='utf-8', xml_declaration=True)
            except FileExistsError:
                pass
    return changed

def json_write(path, value):
    atomic_write(path, value)

class Worker:

    def __init__(self, root=ROOT):
        self.root = root
        self.config = json.loads((root / 'data/cache-config.json').read_text(encoding='utf-8'))
        self.accounts = json.loads((root / 'secrets/accounts.json').read_text(encoding='utf-8'))
        self.stream_key = (root / 'secrets/stream-key.txt').read_text().strip()
        state = root / 'data/cache-state.json'
        self.entries = json.loads(state.read_text(encoding='utf-8')) if state.exists() else {}
        self.routes = load_routes(root / 'data/library-routing.json', {})
        self.series_rules = load_routes(root / 'data/absolute-series.json', [])
        self.catalog_rules = load_routes(root / 'data/catalog-rules.json', [])
        self.catalog_hidden = load_routes(root / 'data/catalog-hidden.json', {})
        self.source_bindings = Bindings(root)
        self.source_bindings.apply(self.entries)
        for entry in self.entries.values():
            if self.source_bindings.hidden(entry['id'], entry['remote'], entry.get('source_fid'), self.catalog_hidden):
                entry['catalog_hidden'] = True
        self.lock = threading.RLock()
        self.transfer_lock = threading.Lock()
        self.active = {}
        self.playback_stream_activity = {}
        self.downloading = None
        self.download_progress = 0
        self.last_scan = load_routes(root / 'data/cache-status.json', {}).get('last_scan_at', 0)
        self.last_sync = 0
        self.token = (root / 'secrets/jellyfin-admin-token.txt').read_text()
        self.auth_lock = threading.Lock()
        self.jellyfin_auth_lock = threading.RLock()
        self.jellyfin_authorization = 'MediaBrowser Client="MediaCache", Device="Windows", DeviceId="media-cache-worker-' + str(os.getpid()) + '-' + secrets.token_hex(6) + '", Version="1.0"'
        self.last_error = None
        self.source_errors = {}
        self.playback_present = False
        self.playback_status_unknown = False
        self.last_session_poll = 0
        self.steam_downloading = False
        self.steam_gate = SteamGate(self.config.get('steam_roots'))
        self.api_http = credential_opener(proxy_handler())
        self.http = media_opener(proxy_handler())

    def api(self, service, path, data=None, method=None):
        for attempt in range(2):
            try:
                result, used_token = self.api_once(service, path, data, method)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code != 401 or attempt:
                    raise
                if service == 'openlist':
                    self.renew_openlist_token()
                continue
            if service != 'openlist' or not isinstance(result, dict) or result.get('code') != 401:
                return result
            if attempt:
                raise RuntimeError('OpenList authentication failed after retry')
            self.renew_openlist_token(used_token)

    def renew_openlist_token(self, rejected_token=None):
        with self.auth_lock:
            path = self.root / 'secrets/openlist-token.txt'
            if rejected_token and path.read_text().strip() != rejected_token:
                return
            request = urllib.request.Request(_cinema_endpoint('openlist') + '/api/auth/login', data=json.dumps(self.accounts['openlist']).encode(), headers={'Content-Type': 'application/json'})
            try:
                with self.api_http.open(request, timeout=30) as response:
                    result = read_api_json(response)
            except urllib.error.HTTPError as error:
                error.close()
                raise RuntimeError('OpenList authentication failed') from None
            token = (result.get('data') or {}).get('token')
            if result.get('code') != 200 or not isinstance(token, str) or (not token.strip()):
                raise RuntimeError('OpenList authentication failed')
            path.write_text(token, encoding="utf-8")
            logging.info('Renewed local OpenList API login')

    def api_once(self, service, path, data=None, method=None):
        headers = {'Content-Type': 'application/json'}
        if service == 'openlist':
            headers['Authorization'] = (self.root / 'secrets/openlist-token.txt').read_text().strip()
            base = _cinema_endpoint('openlist')
        else:
            lock = self.__dict__.setdefault('jellyfin_auth_lock', threading.RLock())
            authorization = self.__dict__.setdefault('jellyfin_authorization', 'MediaBrowser Client="MediaCache", Device="Windows", DeviceId="media-cache-worker-' + str(os.getpid()) + '-' + secrets.token_hex(6) + '", Version="1.0"')
            with lock:
                if not self.token:
                    account = self.accounts['jellyfin_admin']
                    auth = {'Username': account['username'], 'Pw': account['password']}
                    request = urllib.request.Request(_cinema_endpoint('jellyfin') + '/Users/AuthenticateByName', data=json.dumps(auth).encode(), headers={'Content-Type': 'application/json', 'Authorization': authorization})
                    with self.api_http.open(request, timeout=30) as response:
                        self.token = read_api_json(response)['AccessToken']
                used_jellyfin_token = self.token
            headers['Authorization'] = authorization + ', Token="' + used_jellyfin_token + '"'
            base = _cinema_endpoint('jellyfin')
        request = urllib.request.Request(base + path, data=None if data is None else json.dumps(data).encode(), headers=headers, method=method)
        try:
            with self.api_http.open(request, timeout=5 if service == 'jellyfin' and path == '/Sessions' else 40) as response:
                return (read_api_json(response), headers.get('Authorization'))
        except urllib.error.HTTPError as error:
            error.close()
            if service == 'jellyfin' and error.code == 401:
                with self.jellyfin_auth_lock:
                    if self.token == used_jellyfin_token:
                        self.token = None
            raise

    def cloud_request(self, path, method='GET', range_header=None, entry_id=None):
        bindings = getattr(self, 'source_bindings', None)
        if bindings:
            path = bindings.resolve(path, entry_id)
        account = self.accounts['openlist']
        basic = base64.b64encode((account['username'] + ':' + account['password']).encode()).decode()
        headers = {'Authorization': 'Basic ' + basic}
        if range_header:
            headers['Range'] = range_header
        return urllib.request.Request(_cinema_endpoint('openlist') + '/dav' + urllib.parse.quote(remote_path(path), safe='/'), headers=headers, method=method)

    def save(self):
        with self.lock:
            json_write(self.root / 'data/cache-state.json', self.entries)
            ready = [value for value in self.entries.values() if value.get('ready')]
            status = {'catalog_items': len(self.entries), 'ready_items': len(ready), 'cache_bytes': sum((value['size'] for value in ready)), 'max_cache_bytes': self.config['max_cache_bytes'], 'reserve_free_bytes': self.config['reserve_free_bytes'], 'downloading': self.downloading, 'downloaded_bytes': self.download_progress, 'last_error': self.last_error, 'updated_at': time.time()}
            status['source_errors'] = getattr(self, 'source_errors', {})
            status['downloads_paused'] = bool(self.config.get('downloads_paused'))
            status['source_roots'] = configured_sources(self.config)
            status['download_errors'] = {entry['id']: entry['download_error'] for entry in self.entries.values() if entry.get('download_error')}
            status['last_scan_at'] = self.last_scan
            status['scan_interval_seconds'] = self.config.get('scan_interval_seconds', 1800)
            from playback_buffer import status as playback_buffer_status
            status['playback_buffer'] = playback_buffer_status(self)
            if self.config.get('smart_cache'):
                plan = self.plan_downloads()
                status['cache_mode'] = 'playback-and-favorites'
                status['playback_exclusive'] = getattr(self, 'playback_present', False)
                status['playback_network_priority'] = self.playback_needs_network()
                status['playback_paused'] = any((e.get('playback_active') and e.get('playback_paused') for e in self.entries.values()))
                status['playback_buffering'] = any(self.active.values())
                status['playback_status_unknown'] = getattr(self, 'playback_status_unknown', False)
                status['steam_downloading'] = getattr(self, 'steam_downloading', False)
                status['paused_downloads'] = [{'id': e['id'], 'title': e.get('display_title') or e['series'], 'completed_bytes': e.get('partial_bytes_completed', 0), 'size': e['size']} for e in self.entries.values() if e.get('download_paused')]
                status['plan'] = [{'id': e['id'], 'title': e.get('display_title') or e['series'], 'season': e.get('season'), 'episode': e.get('episode'), 'size': e['size'], 'progress_percent': e.get('progress_percent'), 'reason': e.get('cache_reason')} for e in plan]
            json_write(self.root / 'data/cache-status.json', status)
            if self.config.get('smart_cache') and time.time() - getattr(self, 'last_report', 0) >= 3:
                report = self.root / 'data/cache-report.html'
                temp = report.with_suffix('.tmp')
                temp.write_text(render_report(status, self.entries.values()), encoding='utf-8')
                os.replace(temp, report)
                self.last_report = time.time()

    def scan(self):
        self.scan_scope = {}
        self.batch_discovered_ids = set()
        self.batch_catalog_changes = set()
        observe(self.root, 'scan', 'discovery', '扫描网盘目录')
        errors = {}
        succeeded = 0
        discovered = 0
        successful_sources = []
        for source_root in configured_sources(self.config):
            try:
                discovered += self.scan_source(source_root)
                succeeded += 1
                successful_sources.append(source_root)
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                errors[source_root] = type(error).__name__ + ': ' + str(error).split('?')[0][:160]
                logging.warning('Media source unavailable: %s (%s)', source_root, type(error).__name__)
        self.source_errors = errors
        if not succeeded:
            observe(self.root, 'scan', 'discovery', '本批网盘读取未完成，请重新开始检查', 'error')
            self.save()
            raise RuntimeError('All configured media source scans failed')
        self.last_scan = time.time()
        from catalog_availability import reconcile_missing
        reconcile_missing(self, successful_sources)
        observe(self.root, 'scan', 'index', '已扫描，等待媒体库建立索引', 'partial' if errors else 'complete', count=discovered)
        self.save()
        if getattr(self, 'full_maintenance', False) or not self.config.get('manual_ingestion_only'):
            self.api('jellyfin', '/Library/Refresh', {}, method='POST')
        elif self.batch_catalog_changes:
            from jellyfin_ingestion import notify_changed
            notify_changed(self, entries=[self.entries[i] for i in self.batch_catalog_changes])
        logging.info('Catalog scanned: %s media items across %s sources (%s unavailable)', discovered, succeeded, len(errors))

    def scan_source(self, source_root):
        from incremental_discovery import managed_prefixes, managed
        incremental = getattr(self, 'config', {}).get('manual_ingestion_only') and (not getattr(self, 'full_maintenance', False))
        skip = managed_prefixes(self.root, source_root) if incremental else set()
        skipped = 0
        from cloud_identity import CloudIdentity
        cloud_ids = CloudIdentity(self, source_root)
        found_ids = set()
        completed_directories = {}
        pending = [source_root]
        seen = set()
        discovered = 0
        while pending:
            path = pending.pop(0)
            observe(self.root, 'scan', 'discovery', path[len(source_root):].strip('/') or source_root, count=discovered)
            if path in seen:
                continue
            seen.add(path)
            if len(seen) > getattr(self, 'config', {}).get('max_scan_directories', 10000):
                raise RuntimeError('目录数量达到保护上限，请检查目录嵌套')
            page = 1
            directory_listing = []
            while True:
                for attempt in range(3):
                    try:
                        result = self.api('openlist', '/api/fs/list', {'path': path, 'page': page, 'per_page': 100, 'refresh': True})
                        if result['code'] != 200:
                            raise RuntimeError('Cloud directory listing failed: ' + str(result['code']))
                        break
                    except (OSError, ValueError, RuntimeError):
                        if attempt == 2:
                            raise
                        time.sleep(attempt + 1)
                listing = result['data']['content'] or []
                object_rows = cloud_ids.directory(path)
                if cloud_ids.failed and getattr(self, 'config', {}).get('cloud_normalization_enabled'):
                    raise RuntimeError('Cloud identity unavailable; discovery mutations deferred')
                if getattr(self, 'config', {}).get('cloud_normalization_enabled'):
                    cloud_ids.validate_listing(path, listing, object_rows)
                directory_listing.extend(listing)
                directory_info = directory_evidence(listing)
                numbered_directory = directory_info['numbered']
                for item in listing:
                    remote = remote_path(path + '/' + item['name'])
                    if item['is_dir']:
                        if not managed(remote, skip):
                            pending.append(remote)
                        else:
                            skipped += 1
                        continue
                    extension = Path(item['name']).suffix.lower()
                    if extension not in VIDEO:
                        continue
                    bindings = getattr(self, 'source_bindings', None)
                    bound_id, fid = cloud_ids.identify(remote, item['size'], object_rows)
                    if not bound_id:
                        bound_id, fid = bindings.identity(remote, item['size']) if bindings else (None, None)
                    ident = bound_id or hashlib.sha256(remote.encode()).hexdigest()[:24]
                    if not bound_id and ident in self.entries and (self.entries[ident].get('remote') != remote or (self.entries[ident].get('source_fid') and self.entries[ident].get('size') != item['size'])):
                        ident = hashlib.sha256((remote + '|' + str(item['size']) + '|' + str(item.get('modified'))).encode()).hexdigest()[:24]
                    found_ids.add(ident)
                    if hasattr(self, 'batch_discovered_ids'):
                        self.batch_discovered_ids.add(ident)
                    with self.lock:
                        entry = self.entries.setdefault(ident, {'id': ident, 'remote': remote, 'created_at': time.time()})
                        if entry.get('relative'):
                            entry.setdefault('cache_relative', entry['relative'])
                        binding = cloud_ids.rows.get(ident, {})
                        classified_remote = entry.get('classified_remote') or next(iter(reversed(binding.get('previous_paths', []))), entry.get('remote'))
                        if bound_id and entry.get('kind') == 'Movies' and entry.get('catalog_relative') and (classified_remote != remote) and (not binding.get('normalized')):
                            from smart_metadata import derive_hints
                            before = derive_hints([classified_remote], kind='Movie')
                            after = derive_hints([remote], kind='Movie')
                            if after['titles'] and before['titles'] != after['titles']:
                                proposal = classify(remote, source_root, getattr(self, 'series_rules', ()), numbered_directory=numbered_directory, multiple_movies=directory_info['multiple_movies'])
                                if proposal.get('kind') == 'Movies':
                                    entry['source_rename_pending'] = proposal
                                    entry['source_rename_year'] = after.get('year') or before.get('year')
                        entry['classified_remote'] = remote
                        preserve = bool(bound_id and entry.get('catalog_relative'))
                        classified = {} if preserve else classify_override(remote, getattr(self, 'catalog_rules', ())) or classify_normalized(remote, self.root) or classify(remote, source_root, getattr(self, 'series_rules', ()), numbered_directory=numbered_directory, multiple_movies=directory_info['multiple_movies'])
                        if not preserve:
                            entry.update(retain_cache_path(entry, classified, ident, unique_relative))
                        entry['remote'] = remote
                        from confirmed_series_episodes import apply_mapping
                        apply_mapping(self.root, entry)
                        from franchise_context import context
                        evidence = context(remote)
                        if evidence.get('family'):
                            entry.update(franchise_family=evidence['family'], media_role=evidence['role'])
                        if fid:
                            entry['source_fid'] = fid
                        entry['catalog_hidden'] = bindings.hidden(ident, remote, fid, getattr(self, 'catalog_hidden', {})) if bindings else bool(getattr(self, 'catalog_hidden', {}).get(remote))
                        if not preserve and self.routes.get(entry.get('series')) == 'Anime' and (entry['kind'] == 'Shows') and (not classified.get('catalog_relative')):
                            entry['catalog_relative'] = 'Anime/' + entry['relative'][len('Shows/'):]
                        entry['size'] = item['size']
                        entry['recognition_policy_version'] = RULES['version']
                        entry['source_missing_scans'] = 0
                        entry['source_missing'] = False
                        entry.pop('source_missing_evidence', None)
                        if entry.get('catalog_unavailable_archived'):
                            from catalog_availability import restore_link
                            restore_link(self, entry)
                        local = cache_path(_cinema_cache_path('ready'), entry['relative'])
                        entry['ready'] = valid_ready(local, entry)
                        if local.exists() and (not entry['ready']):
                            quarantine = cache_path(_cinema_cache_path('partial'), ident + '.invalid')
                            if quarantine.exists():
                                raise IOError('Invalid cache already quarantined; inspect local cache before retrying')
                            os.rename(local, quarantine)
                            entry['cached_at'] = None
                        from media_attachments import mark_discovered_bonus
                        mark_discovered_bonus(entry, remote, source_root, listing)
                        changed = write_catalog(self.root / 'catalog', entry, self.config['stream_base_url'], self.stream_key)
                        if changed and hasattr(self, 'batch_catalog_changes'):
                            self.batch_catalog_changes.add(ident)
                        discovered += 1
                    for sidecar in listing:
                        if sidecar['is_dir'] or Path(sidecar['name']).suffix.lower() not in SUBTITLE or sidecar['size'] > 5000000:
                            continue
                        video_stem = Path(item['name']).stem
                        suffix = subtitle_suffix(sidecar['name'], video_stem)
                        if suffix is None:
                            continue
                        destination = cache_path(self.root / 'catalog', str(Path(catalog_relative(entry)).with_suffix('')).replace('\\', '/') + suffix)
                        if not destination.exists():
                            with self.http.open(self.cloud_request(path + '/' + sidecar['name']), timeout=40) as response:
                                data = response.read(5000001)
                            if len(data) == sidecar['size']:
                                destination.write_bytes(data)
                    sync_cached_subtitles(self.root, [entry])
                observe(self.root, 'scan', 'classify', path[len(source_root):].strip('/') or source_root, count=discovered)
                total = result['data'].get('total', len(listing))
                if page * 100 >= total or not listing:
                    break
                page += 1
            if getattr(self, 'config', {}).get('cloud_normalization_enabled') or (not cloud_ids.failed and path in cloud_ids.directory_rows):
                try:
                    cloud_ids.validate_listing(path, directory_listing, object_rows, complete=True, total=total)
                except RuntimeError:
                    if getattr(self, 'config', {}).get('cloud_normalization_enabled'):
                        raise
                else:
                    if not cloud_ids.failed and path in cloud_ids.directory_rows:
                        completed_directories[path] = list(cloud_ids.directory_rows[path])
        cloud_ids.flush()
        if cloud_ids.failed and getattr(self, 'config', {}).get('cloud_normalization_enabled'):
            raise RuntimeError('Cloud object identity temporarily unavailable; deletion reconciliation deferred')
        from source_presence import confirm_scanned_absences
        absent = confirm_scanned_absences(cloud_ids, self.entries, found_ids, completed_directories)
        with self.lock:
            for ident, parent in absent.items():
                entry = self.entries[ident]
                entry['source_missing_scans'] = max(2, entry.get('source_missing_scans', 0) + 1)
                entry['source_missing'] = True
                entry['source_missing_evidence'] = {'parent': parent, 'confirmed_at': time.time(), 'method': 'complete-directory-and-native-refresh'}
        if not hasattr(self, 'scan_scope'):
            self.scan_scope = {}
        self.scan_scope[source_root] = {'directories_read': len(seen), 'managed_folders_skipped': skipped, 'videos_discovered': discovered}
        return discovered

    def sync_playback(self):
        users = self.api('jellyfin', '/Users')
        all_items = []
        for user in users:
            if user.get('Policy', {}).get('IsDisabled'):
                continue
            query = urllib.parse.urlencode({'UserId': user['Id'], 'Recursive': 'true', 'IncludeItemTypes': 'Movie,Episode,Series', 'Fields': 'Path,MediaSources', 'EnableUserData': 'true'})
            items = self.api('jellyfin', '/Items?' + query).get('Items', [])
            all_items.extend(items)
        Worker.refresh_sessions(self)
        with self.lock:
            apply_playback(self.entries.values(), self.root, all_items, self.session_snapshot, self.active, time.time())
        self.last_sync = time.time()

    def refresh_sessions(self):
        with self.lock:
            self.session_request_sequence = getattr(self, 'session_request_sequence', 0) + 1
            sequence = self.session_request_sequence
        try:
            sessions = self.api('jellyfin', '/Sessions')
        except (OSError, ValueError, RuntimeError):
            with self.lock:
                if sequence >= getattr(self, 'session_applied_sequence', 0):
                    self.playback_status_unknown = True
            raise
        playing = {s['NowPlayingItem']['Id']: s for s in sessions if s.get('NowPlayingItem')}
        with self.lock:
            if sequence < getattr(self, 'session_applied_sequence', 0):
                return
            self.session_applied_sequence = sequence
            self.session_snapshot = sessions
            self.playback_present = bool(playing)
            self.playback_status_unknown = False
            self.last_session_poll = time.time()
            selected = session_entries(list(self.entries.values()), sessions, self.active)
            for e in self.entries.values():
                session = selected.get(e['id'])
                e['playback_active'] = bool(session)
                e['playback_paused'] = bool(session and (session.get('PlayState') or {}).get('IsPaused'))
                e['active'] = bool(session) or bool(self.active.get(e['id']))
                if session:
                    e['last_played_at'] = time.time()
                    e['last_access'] = time.time()
                    position = (session.get('PlayState') or {}).get('PositionTicks')
                    if position is not None:
                        e['playback_position_ticks'] = position
                        if e.get('runtime_ticks'):
                            e['progress_percent'] = round(min(100, 100 * position / e['runtime_ticks']), 1)

    def monitor_playback(self, stop_event=None):
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            self.reload_cache_config()
            self.update_download_gate()
            interrupt = getattr(self, 'transfer_interrupt', None)
            if interrupt:
                interrupt()
            try:
                self.refresh_sessions()
                self.ensure_foreground_cache()
            except (OSError, ValueError, RuntimeError, KeyError):
                self.playback_status_unknown = True
            interrupt = getattr(self, 'transfer_interrupt', None)
            if interrupt:
                interrupt()
            stop_event.wait(self.config.get('session_poll_seconds', 5))

    def ensure_foreground_cache(self):
        """Starting playback during ingestion must not wait for metadata work."""
        if not getattr(self, 'ingestion_running', False) or self.playback_needs_network() or self.config.get('downloads_paused') or getattr(self, 'steam_downloading', False) or getattr(self, 'playback_status_unknown', False):
            return
        with self.lock:
            thread = getattr(self, 'foreground_cache_thread', None)
            if thread and thread.is_alive() or getattr(self, 'downloading', None):
                return
            entry = next((e for e in self.entries.values() if e.get('playback_active') and e.get('playback_paused') and (not e.get('ready')) and (not e.get('cache_blocked')) and (e.get('retry_download_at', 0) <= time.time())), None)
            if not entry:
                return

            def transfer():
                try:
                    self.sync_playback()
                    if entry.get('playback_active') and (not self.should_preempt(entry, True)):
                        self.download(entry)
                except DownloadPaused:
                    pass
                except (OSError, ValueError, RuntimeError) as error:
                    entry.update(download_error=type(error).__name__, retry_download_at=time.time() + 60)
                finally:
                    self.save()
            thread = threading.Thread(target=transfer, name='current-playback-cache', daemon=True)
            self.foreground_cache_thread = thread
            thread.start()

    def update_download_gate(self):
        if not hasattr(self, 'steam_gate'):
            return
        if not self.config.get('pause_cache_for_steam', True):
            self.steam_downloading = False
            return
        try:
            self.steam_downloading = self.steam_gate.sample()
        except (OSError, RuntimeError):
            logging.warning('Steam activity read failed; keeping previous cache gate')

    def reload_cache_config(self):
        now = time.monotonic()
        if now - getattr(self, 'last_config_check', 0) < 1:
            return
        self.last_config_check = now
        try:
            path = self.root / 'data/cache-config.json'
            stamp = path.stat().st_mtime_ns
            if stamp == getattr(self, 'cache_config_stamp', None):
                return
            value = json.loads(path.read_text('utf-8'))
            if isinstance(value, dict):
                with self.lock:
                    self.config.update(value)
                self.cache_config_stamp = stamp
        except (OSError, ValueError):
            pass

    def playback_needs_network(self):
        """Pause keeps the sequential playback buffer, not a second full download."""
        return getattr(self, 'playback_present', False) or any((e.get('playback_active') for e in self.entries.values())) or any(getattr(self, 'active', {}).values())

    def should_preempt(self, entry, started_for_playback=False):
        from library_activity import background_blocked
        self.reload_cache_config()
        if self.playback_needs_network():
            return True
        if entry.get('cache_blocked'):
            return True
        if getattr(self, 'root', None) and (not entry.get('playback_active')) and any((self.root / 'data/requests').glob('*.json')):
            from library_tasks import read
            if any((read(p).get('action') in ('scan', 'scan-new', 'start-automation') for p in (self.root / 'data/requests').glob('*.json'))):
                return True
        if not entry.get('playback_active') and background_blocked(self):
            return True
        if self.config.get('downloads_paused'):
            return True
        if getattr(self, 'steam_downloading', False):
            return True
        if getattr(self, 'playback_status_unknown', False):
            return True
        if entry.get('manual_cancelled') and entry['id'] not in {p['id'] for p in cache_plan(self.entries.values(), self.config['next_episodes'], time.time(), self.config.get('recent_playback_seconds', 14 * 86400))}:
            return True
        present = getattr(self, 'playback_present', False) or any((e.get('playback_active') for e in self.entries.values()))
        return present and (not entry.get('playback_active')) or (started_for_playback and (not present))

    def plan_downloads(self):
        self.reload_cache_config()
        with self.lock:
            if self.playback_needs_network():
                for e in self.entries.values():
                    e.pop('cache_reason', None)
                return []
            if self.config.get('downloads_paused'):
                return []
            if not self.config.get('smart_cache'):
                return next_downloads(list(self.entries.values()), self.config['next_episodes'])
            if getattr(self, 'steam_downloading', False):
                return []
            if getattr(self, 'playback_status_unknown', False):
                return []
            if getattr(self, 'playback_present', False) and (not any((e.get('playback_active') for e in self.entries.values()))):
                return []
            plan = cache_plan(self.entries.values(), self.config['next_episodes'], time.time(), self.config.get('recent_playback_seconds', 14 * 86400))
            for e in self.entries.values():
                e.pop('cache_reason', None)
            for p in plan:
                self.entries[p['id']]['cache_reason'] = p['reason']
            from library_activity import background_blocked
            return [self.entries[p['id']] for p in plan if not background_blocked(self) or self.entries[p['id']].get('playback_active')]

    def cleanup(self, incoming=0, disk_incoming=None):
        if time.time() - self.last_sync > 90:
            raise RuntimeError('Playback status is stale; cleanup suspended')
        with self.lock:
            candidates = [dict(entry, active=entry.get('active') or bool(self.active.get(entry['id'])) or self.downloading == entry['id'] or (getattr(self, 'transfer_entry_id', None) == entry['id'])) for entry in self.entries.values() if entry.get('ready')]
            partial_bytes = sum((path.stat().st_size for path in _cinema_cache_path('partial').glob('*') if path.is_file()))
            retention = None if self.config.get('cleanup_only_on_pressure') else self.config['retention_seconds']
            if disk_incoming is None:
                disk_incoming = incoming
            remove = eviction_ids(candidates, time.time(), self.config['max_cache_bytes'] - partial_bytes - incoming, shutil.disk_usage(self.root).free, self.config['reserve_free_bytes'] + disk_incoming, retention)
            for ident in remove:
                entry = self.entries[ident]
                delete_cached(_cinema_cache_path('ready'), entry['relative'])
                entry['ready'] = False
                logging.info('Evicted local cache: %s (cloud and catalog preserved)', ident)
        self.save()

    def process_requests(self):
        folder = self.root / 'data/requests'
        folder.mkdir(exist_ok=True)
        scans = []
        manual_waiting = False
        for request in sorted(folder.glob('*.json'), key=lambda p: (not p.name.startswith('manual-match-'), p.name)):
            payload = json.loads(request.read_text(encoding='utf-8'))
            if payload.get('action') in ('organizing-plan', 'organizing-apply'):
                if self.playback_present or getattr(self, 'ingestion_running', False):
                    continue
                from source_policy import process_request
                result = {'state': 'applied', 'message': '整理操作已处理'}
                try:
                    process_request(self, request, payload)
                except (OSError, ValueError, RuntimeError, KeyError) as error:
                    result = {'state': 'error', 'message': '整理未完成，请检查来源授权与计划后重试', 'error_type': type(error).__name__}
                if payload.get('request_id'):
                    json_write(self.root / 'data/task-receipts' / (payload['request_id'] + '.json'),
                               {**payload, **result, 'updated_at': time.time()})
                request.unlink(missing_ok=True)
                continue
            if payload.get('action') == 'normalize-cache-names':
                from cache_naming import process_request
                if process_request(self, request, payload):
                    request.unlink(missing_ok=True)
                continue
            if payload.get('action') == 'manual-match':
                if getattr(self, 'ingestion_running', False):
                    continue
                from manual_match import process_request
                if process_request(self, request, payload):
                    request.unlink(missing_ok=True)
                else:
                    manual_waiting = True
                continue
            if payload.get('action') == 'remove-cache':
                try:
                    self.refresh_sessions()
                except (OSError, ValueError, RuntimeError):
                    continue
            if payload.get('action') in ('scan', 'scan-new', 'start-automation'):
                if payload['action'] == 'start-automation':
                    self.config['cloud_normalization_enabled'] = bool(self.config.get('cloud_normalization_enabled'))
                    self.last_cloud_normalize = 0
                    self.last_empty_cleanup = 0
                scans.append(request)
                continue
            if payload.get('action') == 'route-anime':
                if route_to_anime(self, payload['series']):
                    request.unlink()
                continue
            if payload.get('action') == 'canonical-movie':
                from catalog_reconcile import reconcile
                if reconcile(self, payload):
                    request.unlink()
                continue
            with self.lock:
                for entry in self.entries.values():
                    if payload.get('id') not in (entry['id'], 'all-movies') and entry['id'] not in payload.get('ids', []):
                        continue
                    if payload.get('id') == 'all-movies' and entry['kind'] != 'Movies':
                        continue
                    if payload.get('action') == 'queue':
                        entry.pop('cache_blocked', None)
                        entry.pop('cache_remove_pending', None)
                        entry['queued'] = not entry.get('ready', False)
                        entry.pop('retry_download_at', None)
                        entry.pop('download_error', None)
                        entry['manual_cancelled'] = False
                        entry['queued_at'] = payload.get('queued_at', time.time())
                    if payload.get('action') == 'unqueue':
                        entry['queued'] = False
                        entry['manual_cancelled'] = True
                    if payload.get('action') == 'remove-cache':
                        if entry.get('playback_active') or getattr(self, 'active', {}).get(entry['id']):
                            payload['_error'] = '当前视频正在播放或暂停播放，请退出播放页后删除缓存'
                            continue
                        entry.update(cache_blocked=True, queued=False, manual_cancelled=True)
                        entry.pop('cache_reason', None)
                        entry.pop('pinned', None)
                        if getattr(self, 'downloading', None) == entry['id'] or getattr(self, 'transfer_entry_id', None) == entry['id']:
                            entry['cache_remove_pending'] = True
                            continue
                        delete_cached(_cinema_cache_path('ready'), entry.get('cache_relative') or entry['relative'])
                        for suffix in ('.part', '.invalid'):
                            delete_cached(_cinema_cache_path('partial'), entry['id'] + suffix)
                        delete_cached(self.root / 'data/range-resume', entry['id'] + '.json')
                        entry.update(ready=False, cached_at=None)
                        for key in ('partial_bytes_completed', 'download_paused', 'pause_kind', 'paused_at', 'download_error', 'retry_download_at', 'cache_remove_pending'):
                            entry.pop(key, None)
                    if payload.get('action') == 'pin':
                        entry['pinned'] = True
                    if payload.get('action') == 'unpin':
                        entry['pinned'] = False
                    if payload.get('action') == 'prefer-version':
                        entry['quality_override_at'] = time.time()
                        self.last_cloud_normalize = 0
            if payload.get('action') == 'remove-cache' and any((self.entries[i].get('cache_remove_pending') for i in payload.get('ids', []) if i in self.entries)):
                self.save()
                interrupt = getattr(self, 'transfer_interrupt', None)
                if interrupt:
                    interrupt()
                continue
            self.save()
            if payload.get('request_id'):
                receipt_path = self.root / 'data/task-receipts' / (payload['request_id'] + '.json')
                receipt = json.loads(receipt_path.read_text('utf-8')) if receipt_path.exists() else {}
                json_write(receipt_path, {**receipt, 'request_id': payload['request_id'], 'state': 'error' if payload.get('_error') else 'applied', 'message': payload.get('_error', ''), 'updated_at': time.time()})
            request.unlink()
        self.manual_match_pending = manual_waiting
        if scans and (not manual_waiting) and (not getattr(self, 'ingestion_running', False)):
            transfer = getattr(self, 'downloading', None) or getattr(self, 'transfer_entry_id', None)
            if transfer and (not self.entries.get(transfer, {}).get('playback_active')):
                interrupt = getattr(self, 'transfer_interrupt', None)
                if interrupt:
                    interrupt()
            else:
                from manual_ingest import execute_batch
                execute_batch(self, scans)
        restore_progress(self)
        if getattr(self, 'config', {}).get('cloud_normalization_enabled'):
            from cloud_sync import normalize_pending
            normalize_pending(self)
        if not getattr(self, 'ingestion_running', False) and time.time() - getattr(self, 'last_cache_name_sync', 0) > 30:
            from cache_naming import follow
            self.last_cache_name_sync = time.time()
            follow(self, limit=24)
            sync_cached_subtitles(self.root, self.entries.values())

    def scan_if_due(self):
        if getattr(self, 'config', {}).get('manual_ingestion_only'):
            return
        if time.time() - self.last_scan > self.config.get('scan_interval_seconds', 1800):
            self.scan()

    def wait_downloads(self, futures, on_tick=None):
        pending = set(futures)
        interval = max(0.01, self.config.get('poll_seconds', 30))
        last_check = time.monotonic()
        while pending:
            done, pending = wait(pending, timeout=min(1, interval), return_when=FIRST_COMPLETED)
            for future in done:
                future.result()
            if pending and self.config.get('smart_cache') and (time.time() - getattr(self, 'last_session_poll', 0) >= self.config.get('session_poll_seconds', 5)):
                try:
                    self.refresh_sessions()
                except (OSError, ValueError, RuntimeError):
                    logging.warning('Playback status unavailable; downloads will wait')
            if pending and on_tick:
                on_tick()
            if pending and time.monotonic() - last_check >= interval:
                last_check = time.monotonic()
                if threading.current_thread() is not getattr(self, 'foreground_cache_thread', None):
                    self.process_requests()
                    self.scan_if_due()
                if self.config.get('smart_cache'):
                    self.sync_playback()
                    self.save()

    def download(self, entry):
        with self.lock:
            from cache_naming import rename_entry
            if entry.get('catalog_relative') and entry.get('relative') != entry['catalog_relative']:
                rename_entry(self, entry)
            if not hasattr(self, 'transfer_lock'):
                self.transfer_lock = threading.Lock()
            owner = self.transfer_lock
            if not owner.acquire(blocking=False):
                return False
            self.transfer_entry_id = entry['id']
        try:
            if entry.get('cache_blocked') or self.should_preempt(entry):
                return False
            return download_ranges(self, entry)
        finally:
            with self.lock:
                self.transfer_entry_id = None
                owner.release()

    def process_downloads(self, queue):
        foreground = getattr(self, 'foreground_cache_thread', None)
        if foreground and foreground.is_alive():
            return
        attempted = set()
        while queue:
            self.process_requests()
            foreground = getattr(self, 'foreground_cache_thread', None)
            if foreground and foreground.is_alive():
                return
            self.scan_if_due()
            self.sync_playback()
            self.cleanup()
            if self.config.get('smart_cache'):
                queue = [e for e in self.plan_downloads() if e['id'] not in attempted]
                if not queue:
                    break
            entry = queue[0]
            attempted.add(entry['id'])
            foreground = getattr(self, 'foreground_cache_thread', None)
            if foreground and foreground.is_alive():
                return
            try:
                if self.download(entry):
                    entry.pop('download_error', None)
                    entry.pop('retry_download_at', None)
            except DownloadPaused:
                logging.info('Download suspended for playback priority: %s', entry['id'])
            except (OSError, ValueError, RuntimeError) as error:
                entry['download_error'] = type(error).__name__ + ': ' + str(error).split('?')[0][:160]
                entry['retry_download_at'] = time.time() + 600
                logging.warning('Original download deferred: %s (%s)', entry['id'], type(error).__name__)
            self.save()
            if self.config.get('smart_cache'):
                queue = [e for e in self.plan_downloads() if e['id'] not in attempted]
            else:
                queue = queue[1:]

    def run(self):
        from library_tasks import read
        batch = read(self.root / 'data/manual-ingestion.json')
        if batch.get('state') == 'running' and (not any((read(p).get('action') in ('scan', 'scan-new', 'start-automation') for p in (self.root / 'data/requests').glob('*.json')))):
            atomic_write(self.root / 'data/manual-ingestion.json', {**batch, 'state': 'error', 'updated_at': time.time(), 'note': '上批因程序重启中断，请重新点击扫描；已完成结果保留'})
        while True:
            try:
                if self.config.get('downloads_paused'):
                    self.process_requests()
                    self.scan_if_due()
                    self.sync_playback()
                    self.last_error = None
                    self.save()
                    time.sleep(self.config.get('poll_seconds', 30))
                    continue
                self.process_requests()
                self.scan_if_due()
                from cloud_sync import normalize_pending
                normalize_pending(self)
                if time.time() - self.last_sync >= self.config.get('poll_seconds', 30):
                    self.sync_playback()
                elif self.config.get('smart_cache'):
                    self.refresh_sessions()
                self.cleanup()
                self.last_error = None
                with self.lock:
                    queue = [] if getattr(self, 'manual_match_pending', False) else self.plan_downloads()
                self.process_downloads(queue)
                self.save()
            except Exception as error:
                self.last_error = type(error).__name__ + ': ' + str(error).split('?')[0][:180]
                logging.error('Worker cycle failed: %s', self.last_error)
                self.save()
            time.sleep(min(self.config.get('poll_seconds', 30), self.config.get('session_poll_seconds', 5)) if self.config.get('smart_cache') else self.config.get('poll_seconds', 30))

def handler_for(worker):

    class Handler(BaseHTTPRequestHandler):

        def log_message(self, *_):
            pass

        def do_HEAD(self):
            self.serve(head=True)

        def do_GET(self):
            self.serve(head=False)

        def serve(self, head):
            url = urllib.parse.urlsplit(self.path)
            key = urllib.parse.parse_qs(url.query).get('key', [''])[0]
            if not hmac.compare_digest(key, worker.stream_key):
                self.send_error(403)
                return
            match = re.fullmatch('/stream/([a-f0-9]{24})', url.path)
            if not match:
                self.send_error(404)
                return
            ident = match[1]
            with worker.lock:
                item = worker.entries.get(ident)
                if not item:
                    self.send_error(404)
                    return
                item['last_access'] = time.time()
                if not head:
                    worker.active[ident] = worker.active.get(ident, 0) + 1
                    worker.playback_stream_activity[ident] = time.time()
            interrupt = getattr(worker, 'transfer_interrupt', None)
            if interrupt and (not head):
                interrupt()
            started = False
            try:
                if head:
                    selected = byte_range(self.headers.get('Range'), item['size'])
                    start, end = selected if selected else (0, item['size'] - 1)
                    self.send_response(206 if selected else 200)
                    self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('Content-Length', str(end - start + 1))
                    self.send_header('Content-Type', mimetypes.guess_type(item['relative'])[0] or 'application/octet-stream')
                    if selected:
                        self.send_header('Content-Range', f"bytes {start}-{end}/{item['size']}")
                    self.end_headers()
                    started = True
                    return
                local = cache_path(_cinema_cache_path('ready'), item['relative'])
                if item.get('ready') and local.is_file() and (local.stat().st_size == item['size']):
                    sync_cached_subtitles(worker.root, [item])
                    selected = byte_range(self.headers.get('Range'), item['size'])
                    start, end = selected if selected else (0, item['size'] - 1)
                    self.send_response(206 if selected else 200)
                    self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('X-Media-Cache', 'local')
                    self.send_header('Content-Length', str(end - start + 1))
                    self.send_header('Content-Type', mimetypes.guess_type(local.name)[0] or 'application/octet-stream')
                    if selected:
                        self.send_header('Content-Range', f"bytes {start}-{end}/{item['size']}")
                    self.end_headers()
                    started = True
                    if not head:
                        with local.open('rb') as source:
                            source.seek(start)
                            remaining = end - start + 1
                            while remaining:
                                data = source.read(min(256 * 1024, remaining))
                                if not data:
                                    break
                                self.wfile.write(data)
                                remaining -= len(data)
                else:
                    from playback_buffer import stream
                    selected = byte_range(self.headers.get('Range'), item['size'])
                    start, end = selected if selected else (0, item['size'] - 1)
                    self.send_response(206 if selected else 200)
                    self.send_header('X-Media-Cache', 'playback-disk-buffer')
                    self.send_header('Content-Length', str(end - start + 1))
                    self.send_header('Content-Type', mimetypes.guess_type(item['relative'])[0] or 'application/octet-stream')
                    self.send_header('Accept-Ranges', 'bytes')
                    if selected:
                        self.send_header('Content-Range', f"bytes {start}-{end}/{item['size']}")
                    self.end_headers()
                    started = True
                    stream(worker, item, start, end, self.wfile)
            except ValueError:
                if not started:
                    self.send_response(416)
                    self.send_header('Content-Range', 'bytes */' + str(item['size']))
                    self.end_headers()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception as error:
                logging.warning('Playback %s failed: %s', ident, type(error).__name__)
                if not started:
                    self.send_error(502, 'Source temporarily unavailable')
            finally:
                with worker.lock:
                    if not head:
                        worker.active[ident] -= 1
    return Handler

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['run', 'scan', 'status', 'queue', 'pin', 'unpin'])
    parser.add_argument('id', nargs='?')
    args = parser.parse_args()
    if args.command == 'scan':
        json_write(ROOT / 'data/requests' / (secrets.token_hex(8) + '.json'), {'action': 'scan'})
        print('Library refresh requested; the running worker will scan both drives.')
        return
    if args.command in ('queue', 'pin', 'unpin'):
        if not args.id:
            parser.error('Media ID is required')
        json_write(ROOT / 'data/requests' / (secrets.token_hex(8) + '.json'), {'action': args.command, 'id': args.id})
        print('Request queued')
        return
    if args.command == 'status':
        print((ROOT / 'data/cache-status.json').read_text(encoding='utf-8'))
        return
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[logging.FileHandler(ROOT / 'logs/cache-worker.log', encoding='utf-8')])
    worker = Worker()
    worker.update_download_gate()
    import msvcrt
    lockfile = (ROOT / 'data/cache-worker.lock').open('a+b')
    lockfile.seek(0)
    lockfile.write(b'0')
    lockfile.flush()
    lockfile.seek(0)
    msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
    from cache_naming import recover
    recover(worker)
    if worker.config.get('smart_cache'):
        threading.Thread(target=worker.monitor_playback, daemon=True).start()
    threading.Thread(target=worker.run, daemon=True).start()
    from personal_cinema.runtime_paths import bind_host
    ThreadingHTTPServer((bind_host('stream'), _cinema_port('stream')), handler_for(worker)).serve_forever()
if __name__ == '__main__':
    main()
