"""Authenticated cache controls and ingestion observations for the management page."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import http.client
import json
import re
import threading
import time
import os
import subprocess
import urllib.parse
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_status_api import public_status
from library_tasks import read, write, safe_event
from library_routing import catalog_relative
from catalog_intelligence import RULES

def credits_reason(record):
    labels = {'version-not-confirmed': '已找到页面，但作品译名／年份核对未通过', 'cast-not-readable': '页面已读取，但配音／演员字段未能解析', 'character-names-need-review': '已提取演员，但角色中文名待核对', 'unavailable': '百科页面请求失败'}
    details = []
    for attempt in record.get('attempts', []):
        source = '百度百科' if attempt.get('source') == 'baidu_baike' else '中文维基百科'
        details.append(source + '：' + labels.get(attempt.get('status'), attempt.get('status', '未完成')))
    return '；'.join(details) or record.get('reason') or '尚未核对中文演职员资料；仅在手动检查时重试'

def poster_state(item, record, root=None):
    """An arbitrary existing image is not proof of a correctly matched poster."""
    primary = (item.get('ImageTags') or {}).get('Primary')
    complete = bool(primary and item.get('Overview'))
    if complete and (record.get('status') == 'manual-lock' or item.get('IsLocked') or 'Images' in (item.get('LockedFields') or [])):
        return 'done'
    if record.get('status') == 'needs-review':
        return 'review'
    if not complete:
        return 'waiting'
    current = str((item.get('ProviderIds') or {}).get('Tmdb') or '')
    matched = current and str(record.get('provider_id') or '') == current
    if root is not None:
        from verified_artwork import proof_matches
        proof = item.get('_verified_poster') or record.get('native_verification')
        return 'done' if record.get('status') == 'applied' and matched and proof_matches(root, item, record, proof) else 'waiting'
    if record.get('status') == 'applied' and matched and (not record.get('image_tag') or record['image_tag'] == primary):
        return 'done'
    return 'waiting'

def batch_event(batch, prior, now):
    """A finished manual batch supersedes the scanner's intermediate heartbeat."""
    if not batch or batch.get('state') not in ('running', 'complete', 'partial', 'error'):
        return safe_event(prior, now) if prior else None
    state = batch['state']
    stage = batch.get('stage', 'discovery')
    labels = {'discovery': '发现文件', 'identity': '匹配作品', 'classify': '分类整理', 'organize': '分类整理', 'index': '建立媒体索引', 'poster': '海报与简介', 'credits': '中文演职人员', 'complete': '本批检查完成'}
    scope = '单作品整理' if batch.get('scope') == 'selected' else '全局扫描' if batch.get('scope') == 'full' else '新入库扫描'
    title = scope + ' · ' + labels.get(stage, '入库整理')
    if state == 'partial':
        title = scope + ' · ' + (batch.get('note') or '部分目录未完成；已处理可用资源，请重新扫描')
    elif state == 'error':
        title = scope + ' · ' + (batch.get('note') or '本批检查未完成，请重新扫描')
    return safe_event({'worker': 'scan', 'stage': stage, 'state': state, 'title': title[:300], 'started_at': batch.get('started_at'), 'updated_at': batch.get('updated_at'), 'count': batch.get('changed_files'), 'item_id': None}, now)

def resolved_batch(batch, groups, source_errors):
    """Reflect completion of deferred media work without rewriting scan history."""
    if batch.get('state') != 'partial' or batch.get('stage') != 'complete' or source_errors or batch.get('source_errors') or set(batch.get('library_errors') or {}) - {'metadata'}:
        return batch
    index = batch.get('index_progress') or {}
    if not index.get('complete') or index.get('pending_paths'):
        return batch
    affected = set(batch.get('affected_work_ids') or [])
    relevant = [g for g in groups if g['id'] in affected] if affected else groups
    if not relevant or (affected and (not affected.issubset({g['id'] for g in relevant}))) or any((group.get('state') != 'complete' for group in relevant)):
        return batch
    return {**batch, 'state': 'complete', 'note': '本批资料整理已完成；暂无照片的人员已标明并结束待办', 'pending_works': [], 'library_errors': {}, 'resolved_from_current_library': True, 'updated_at': max([batch.get('updated_at') or 0] + [g.get('updated_at') or 0 for g in groups])}

def virtual_id(text):
    return 'ca5e0000' + hashlib.md5(text.encode()).hexdigest()[8:]
ROOT_ID = virtual_id('private-media-task-center-v1')
GROUPS = {virtual_id('media-task-' + s): s for s in ('cache', 'queue', 'ingest', 'help')}
LABELS = {'cache': '当前缓存', 'queue': '缓存队列', 'ingest': '入库进度', 'help': '手动缓存与操作说明'}
STAGES = [('discovery', '发现文件'), ('classify', '分类整理'), ('identity', '匹配作品'), ('poster', '海报与简介'), ('credits', '中文演职人员'), ('portraits', '演职员头像')]
PHASES = {'downloading': '正在缓存', 'idle': '缓存待命', 'steam': '为 Steam 下载让路', 'paused': '缓存已手动暂停', 'playback': '当前播放已就绪，其他任务等待', 'error': '下载遇到问题，等待重试', 'unavailable': '后台状态过期或暂不可用'}
NATIVE_PREFIX = '/MediaCache'

class APIError(Exception):

    def __init__(self, status, message):
        self.status = status
        self.message = message

def launch_background(root):
    from personal_cinema.runtime_paths import application_home, vault
    from personal_cinema.configuration import ConfigStore
    from personal_cinema.runtime import Runtime
    home = application_home()
    try:
        Runtime(home, ConfigStore(home), vault(), None).start()
    except (OSError, ValueError) as error:
        raise APIError(503, str(error)) from None


def auth_headers(headers, query=''):
    result = {k: v for k, v in headers.items() if k.lower() in ('authorization', 'x-emby-token', 'x-mediabrowser-token')}
    normalized = {key.lower(): value for key, value in result.items()}
    authorization = normalized.get('authorization', '')
    if not re.search(r'\bToken\s*=', authorization, flags=re.I):
        token = normalized.get('x-emby-token') or normalized.get('x-mediabrowser-token') or urllib.parse.parse_qs(query).get('api_key', [''])[0]
        if token:
            from personal_cinema.network_security import jellyfin_authorization
            try: result = {'Authorization': jellyfin_authorization(token)}
            except ValueError: raise APIError(401, '媒体库凭据格式无效，请重新登录') from None
    return result

def upstream_json(port, headers, path):
    c = _cinema_jellyfin_connection(timeout=12)
    try:
        c.request('GET', path, headers=headers)
        r = c.getresponse()
        body = r.read(16 * 1024 * 1024 + 1)
        if r.status in (401, 403):
            raise APIError(r.status, '请先登录影视库')
        if r.status != 200 or len(body) > 16 * 1024 * 1024:
            raise APIError(503, '影视库暂时无法读取，请稍后重试')
        return json.loads(body)
    except (OSError, ValueError, http.client.HTTPException):
        raise APIError(503, '影视库暂时无法读取，请稍后重试') from None
    finally:
        c.close()

def authorize(root, port, headers):
    if not headers:
        raise APIError(401, '请先登录影视库')
    user = upstream_json(port, headers, '/Users/Me')
    policy = user.get('Policy') or {}
    if not user.get('Id') or policy.get('IsDisabled'):
        raise APIError(401, '登录已失效')
    allowed = read(root / 'data/task-center-config.json').get('user_ids', [])
    if not policy.get('IsAdministrator') and user['Id'] not in allowed:
        raise APIError(403, '此账号没有任务中心权限')
    return user

class Center:

    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        self.libraries = {}

    def library(self, port, headers, user):
        key = (user['Id'], json.dumps(user.get('Policy') or {}, sort_keys=True))
        with self.lock:
            now = time.monotonic()
            prior = self.libraries.get(key)
            if prior and now - prior[0] < 12:
                return prior[1]
        query = urllib.parse.urlencode({'UserId': user['Id'], 'Recursive': 'true', 'IncludeItemTypes': 'Movie,Episode,Series', 'Fields': 'Path,Overview,ProviderIds,DateCreated', 'EnableUserData': 'true'})
        rows = upstream_json(port, headers, '/Items?' + query).get('Items', [])
        from verified_artwork import verify_native_poster, ART_ERRORS
        art = read(self.root / 'data/verified-artwork-state.json')
        for item in rows:
            if item.get('Type') not in ('Movie', 'Series'):
                continue
            try:
                item['_verified_poster'] = verify_native_poster(self.root, item, art.get(item['Id'], {}), lambda path: upstream_json(port, headers, path))
            except (APIError, *ART_ERRORS):
                item['_verified_poster'] = None
        with self.lock:
            if len(self.libraries) > 20:
                self.libraries.clear()
            self.libraries[key] = (now, rows)
        return rows

    def visible_entries(self, entries, items):
        by_path = {str(Path(i['Path']).absolute()).casefold(): i for i in items if i.get('Path')}
        by_stream = {}
        series_by_folder = {Path(i['Path']).name: i['Id'] for i in items if i.get('Type') == 'Series' and i.get('Path')}
        episode_keys = {(i.get('SeriesId'), i.get('ParentIndexNumber'), i.get('IndexNumber')): i for i in items if i.get('Type') == 'Episode' and i.get('IndexNumber') is not None}
        for item in items:
            match = re.search('\\[([a-f0-9]{12})\\]', Path(item.get('Path', '')).stem)
            if match:
                by_stream[match[1]] = item
        result = {}
        for ident, e in entries.items():
            if e.get('catalog_hidden') or e.get('catalog_unavailable_archived'):
                continue
            path = str((self.root / 'catalog' / catalog_relative(e)).with_suffix('.strm').absolute()).casefold()
            item = by_path.get(path)
            if not item:
                item = by_stream.get(ident[:12])
            if not item and e.get('kind') == 'Shows':
                parts = Path(catalog_relative(e)).parts
                series_id = e.get('series_id') if e.get('series_id') in series_by_folder.values() else series_by_folder.get(parts[1] if len(parts) > 1 else '') or series_by_folder.get(e.get('series'))
                item = episode_keys.get((series_id, e.get('season'), e.get('episode')))
            if not item and e.get('kind') == 'Movies':
                item = next((i for i in items if i.get('Type') == 'Movie' and i.get('Path') and (Path(i['Path']).parent == Path(path).parent)), None)
            if item:
                result[ident] = (e, item)
        return result

    def snapshot(self, entries, items, now=None):
        now = time.time() if now is None else now
        status = public_status(self.root, entries, now)
        raw = read(self.root / 'data/cache-status.json')
        meta = read(self.root / 'data/metadata-state.json')
        planned = {row.get('id') for row in raw.get('plan', []) if isinstance(row, dict)}
        credits = read(self.root / 'data/chinese-credits-state.json')
        artwork = read(self.root / 'data/verified-artwork-state.json')
        portrait_works = read(self.root / 'data/person-portraits-state.json').get('works', {})
        batch = read(self.root / 'data/manual-ingestion.json')
        visible = self.visible_entries(entries, items)
        permitted_ids = {i['Id'] for i in items}
        if status.get('current') and raw.get('downloading') not in visible:
            status['current'] = None
            status['phase'] = 'idle'
        events = []
        for worker in ('scan', 'metadata', 'credits', 'artwork', 'portraits', 'organizer', 'versions', 'cleanup'):
            v = read(self.root / 'data/task-events' / (worker + '.json'))
            if worker == 'scan':
                event = batch_event(batch, v, now)
                if event:
                    events.append(event)
                continue
            if v and (not v.get('item_id') or v['item_id'] in permitted_ids):
                events.append(safe_event(v, now))
        rows = []
        groups = {}
        pending = {}
        for p in (self.root / 'data/requests').glob('console-*.json'):
            request = read(p)
            for ident in request.get('ids', []):
                pending[ident] = request.get('action')
        for ident, (e, item) in visible.items():
            done = e.get('size', 0) if e.get('ready') else min(e.get('size', 0), raw.get('downloaded_bytes', 0) if ident == raw.get('downloading') else e.get('partial_bytes_completed', 0))
            wanted = bool(e.get('queued') or e.get('cache_reason') or ident in planned) and (not e.get('cache_blocked'))
            phase = 'ready' if e.get('ready') else 'downloading' if ident == raw.get('downloading') and (not raw.get('playback_network_priority')) else 'buffered' if e.get('playback_active') and done > 0 else 'retry' if wanted and e.get('download_error') else 'waiting' if wanted else 'none'
            row = {'id': ident, 'item_id': item['Id'], 'title': item['Name'], 'series': item.get('SeriesName') or '', 'series_id': item.get('SeriesId'), 'season': item.get('ParentIndexNumber', e.get('season')), 'episode': item.get('IndexNumber', e.get('episode')), 'kind': e.get('kind'), 'source': '百度' if e.get('remote', '').startswith('/百度/') else '夸克', 'size': e.get('size', 0), 'bytes': done, 'phase': phase, 'manual': bool(e.get('queued')), 'percent': round(done * 100 / e['size'], 1) if e.get('size') else 0, 'reason': e.get('cache_reason') or ('手动指定' if e.get('queued') else ''), 'retry_at': e.get('retry_download_at') if phase == 'retry' else None, 'error': '源文件暂时无法下载，将按时间重试' if phase == 'retry' else '', 'poster': bool((item.get('ImageTags') or {}).get('Primary')), 'poster_tag': (item.get('ImageTags') or {}).get('Primary'), 'pending_action': pending.get(ident), 'cache_blocked': bool(e.get('cache_blocked'))}
            row['source_missing'] = bool(e.get('source_missing') and (not e.get('ready')))
            row['preferred'] = bool(e.get('quality_preferred'))
            row['recognition'] = e.get('recognition_evidence') or ('已确认的作品映射' if e.get('catalog_rule_applied') or e.get('series_tmdb') else '')
            rows.append(row)
            group_id = item.get('SeriesId') if item.get('Type') == 'Episode' else item['Id']
            if group_id not in groups:
                parent = next((i for i in items if i['Id'] == group_id), item)
                m = meta.get(group_id, {})
                c = credits.get(group_id, {})
                identified = bool((parent.get('ProviderIds') or {}).get('Tmdb') or m.get('status') in ('matched', 'already_matched', 'locked'))
                ident_state = 'done' if identified else 'review' if m.get('status') in ('needs_review', 'needs-review') else 'retry' if m.get('status') == 'retry' else 'waiting'
                cast_state = 'done' if c.get('status') == 'applied' else 'review' if c.get('status') in ('needs-review', 'manual-lock') else 'retry' if c.get('status') == 'retry' else 'waiting'
                portraits = portrait_works.get(group_id, {})
                portrait_closed = portraits.get('status') in ('applied', 'complete', 'no_photo')
                no_photo = portraits.get('no_photo', 0)
                if portrait_closed:
                    portrait_state = 'no_photo' if no_photo or portraits.get('missing') else 'done'
                else:
                    portrait_state = 'retry' if portraits.get('retry') else 'waiting'
                    if portraits.get('status') == 'review':
                        portrait_state = 'review'
                    if portraits.get('status') == 'gap':
                        portrait_state = 'missing'
                portrait_people = portraits.get('missing_people') or []
                portrait_note = ''
                if portraits.get('missing') or no_photo:
                    portrait_note = '头像已补 ' + str(portraits.get('applied', 0)) + '/' + str(portraits.get('total', 0))
                    if portrait_closed:
                        count = no_photo or portraits.get('missing', 0)
                        names = list(dict.fromkeys((p.get('name') for p in portrait_people if p.get('name'))))
                        portrait_note += '；' + str(count) + ' 位标为“暂无照片”，本项已处理，不再重复排队'
                        if names:
                            portrait_note += '。暂无照片：' + '、'.join(names[:6]) + ('等' if len(names) > 6 else '')
                    else:
                        portrait_note += '；本轮已检索，' + str(portraits.get('missing', 0)) + ' 位暂未找到可靠照片，保留后续补配' if portraits.get('status') == 'gap' else '；剩余 ' + str(portraits.get('missing', 0)) + ' 位已保留自动待办'
                        if portraits.get('retry'):
                            portrait_note += '，' + str(portraits['retry']) + ' 位来源连接待重试'
                        if portraits.get('unavailable') and portraits.get('status') != 'gap':
                            portrait_note += '，' + str(portraits['unavailable']) + ' 位继续查找可靠照片'
                        if portraits.get('ambiguous'):
                            portrait_note += '，' + str(portraits['ambiguous']) + ' 位身份尚有歧义'
                groups[group_id] = {'id': group_id, 'kind': parent.get('Type'), 'title': parent['Name'], 'source': row['source'], 'files': 0, 'updated_at': max(m.get('checked_at', 0), c.get('updated_at', 0), artwork.get(group_id, {}).get('updated_at', 0), portraits.get('updated_at', 0)), 'retry_at': c.get('next_attempt_at') or m.get('next_attempt_at') or (portraits.get('next_attempt_at') if not portrait_closed else None), 'step_updated_at': {'identity': m.get('checked_at', 0), 'credits': c.get('updated_at', 0), 'poster': artwork.get(group_id, {}).get('updated_at', 0), 'portraits': portraits.get('updated_at', 0)}, 'steps': [{'key': 'discovery', 'title': '发现文件', 'state': 'done'}, {'key': 'classify', 'title': '分类整理', 'state': 'review' if e.get('identity_needs_review') else 'done'}, {'key': 'identity', 'title': '匹配作品', 'state': ident_state}, {'key': 'poster', 'title': '海报与简介', 'state': poster_state(parent, artwork.get(group_id, {}), self.root)}, {'key': 'credits', 'title': '中文演职人员', 'state': cast_state}, {'key': 'portraits', 'title': '演职员头像', 'state': portrait_state}], 'portraits': {**{k: portraits.get(k, 0) for k in ('total', 'applied', 'missing', 'no_photo', 'actionable_missing', 'pending', 'retry', 'unavailable', 'ambiguous')}, 'missing_people': portrait_people, 'resolved': portrait_closed, 'updated_at': portraits.get('updated_at', 0)}, 'recognition': row['recognition'], 'note': credits_reason(c) if cast_state == 'review' else '作品匹配存在歧义，需要核对' if ident_state == 'review' else portrait_note}
            groups[group_id]['files'] += 1
        for g in groups.values():
            g['state'] = 'review' if any((s['state'] == 'review' for s in g['steps'])) else 'error' if any((s['state'] == 'error' for s in g['steps'])) else 'retry' if any((s['state'] == 'retry' for s in g['steps'])) else 'complete' if all((s['state'] in ('done', 'no_photo') for s in g['steps'])) else 'partial' if all((s['state'] in ('done', 'no_photo', 'missing') for s in g['steps'])) else 'waiting'
            event = next((v for v in events if v.get('item_id') == g['id'] and v['state'] in ('running', 'stalled', 'error') and (not (any((s['key'] == v.get('stage') and s['state'] in ('done', 'no_photo') for s in g['steps'])) and v.get('updated_at', 0) <= g['step_updated_at'].get(v.get('stage'), 0)))), None)
            if event:
                g['state'] = event['state']
                g['updated_at'] = event['updated_at']
                for s in g['steps']:
                    if s['key'] == event['stage']:
                        s['state'] = event['state']
        unindexed = {}
        for ident, e in entries.items():
            if ident in visible or e.get('catalog_hidden') or e.get('catalog_unavailable_archived') or e.get('source_missing') or (e.get('source_missing_scans', 0) > 0):
                continue
            folder = Path(e.get('catalog_relative') or e.get('relative', '')).parent.as_posix()
            if e.get('kind') == 'Shows':
                folder = e.get('series', folder)
            g = unindexed.setdefault(folder, {'id': 'pending-' + hashlib.md5(folder.encode()).hexdigest(), 'title': e.get('display_title') or re.sub('\\s*\\[tmdbid-\\d+\\]', '', e.get('series') or folder), 'files': 0, 'source': '百度' if e.get('remote', '').startswith('/百度/') else '夸克', 'state': 'indexing', 'updated_at': e.get('created_at'), 'steps': [{'key': k, 'title': t, 'state': 'done' if k in ('discovery', 'classify') else 'waiting'} for k, t in STAGES], 'note': '文件已发现，等待 Jellyfin 建立索引；此阶段尚未匹配海报或演员'})
            g['files'] += 1
        ingestion = list(groups.values()) + list(unindexed.values())
        current_batch = resolved_batch(batch, ingestion, raw.get('source_errors'))
        if current_batch is not batch:
            batch = current_batch
            events = [e for e in events if e.get('worker') != 'scan']
            events.append(batch_event(batch, None, now))
        versions = read(self.root / 'data/auto-version-state.json')
        allowed_work_ids = {(i.get('ProviderIds') or {}).get('Tmdb') for i in items if i.get('Type') == 'Movie'}
        version_rows = [v for key, v in versions.items() if key in allowed_work_ids]
        automation = read(self.root / 'data/automation-config.json')
        return {'cache': status, 'events': events, 'items': rows, 'versions': version_rows, 'ingestion': ingestion, 'automation': {'enabled': automation.get('enabled', True), 'startup': automation.get('supervised', False), 'manual_only': automation.get('manual_only', False), 'batch': batch, 'interval': raw.get('scan_interval_seconds', 120)}, 'generated_at': now, 'scan_interval': raw.get('scan_interval_seconds'), 'last_scan_at': raw.get('last_scan_at'), 'source_errors': len(raw.get('source_errors') or {}), 'rules': RULES, 'native_help': '缓存管理、扫描和入库进度统一在管理网页操作：设置页中的工作台链接。手机可用 Safari 打开并添加到主屏幕。'}

    def command(self, entries, items, user, payload):
        if not isinstance(payload, dict):
            raise APIError(400, '无效请求')
        action = payload.get('action')
        rid = payload.get('request_id')
        if action not in ('queue', 'unqueue', 'remove-cache', 'scan', 'scan-new', 'prefer-version', 'start-automation', 'open-settings') or not isinstance(rid, str) or (not re.fullmatch('[a-f0-9]{32}', rid)):
            raise APIError(400, '无效操作或请求编号')
        if action == 'open-settings':
            return self.open_settings(user, rid)
        ids = payload.get('ids', [])
        if not isinstance(ids, list) or not 0 <= len(ids) <= 50 or any((not isinstance(i, str) for i in ids)):
            raise APIError(400, '一次最多选择 50 个视频')

        def validate_current():
            if action not in ('scan', 'scan-new', 'start-automation') and (not ids or not set(ids).issubset(self.visible_entries(entries, items))):
                raise APIError(403, '只能缓存当前账号可见的视频')
            if action == 'queue' and any((entries[i].get('source_missing') and (not entries[i].get('ready')) for i in ids)):
                raise APIError(409, '源文件已移动或删除，请选择另一来源，或把资源重新放入网盘后扫描')
            if action == 'prefer-version' and (len(ids) != 1 or entries[ids[0]].get('kind') != 'Movies' or entries[ids[0]].get('source_missing')):
                raise APIError(409, '请选择一个可用的电影版本')
        record = {'request_id': rid, 'action': action, 'ids': list(dict.fromkeys(ids)), 'user_id': user['Id'], 'queued_at': time.time()}
        path = self.root / 'data/requests' / ('console-' + rid + '.json')
        receipt = self.root / 'data/task-receipts' / (rid + '.json')
        with self.lock:
            prior = read(receipt)
            if prior:
                if prior.get('owner') != user['Id'] or prior.get('action') != action or prior.get('ids') != record['ids']:
                    raise APIError(409, '请求编号已使用，请刷新后重试')
                if prior.get('state') == 'accepted' and (not path.exists()):
                    latest = read(receipt)
                    if latest.get('state') == 'accepted':
                        try:
                            validate_current()
                        except APIError as error:
                            result = {**latest, 'state': 'error', 'message': error.message, 'updated_at': time.time()}
                            write(receipt, result)
                            return result
                        write(path, {**record, 'queued_at': prior.get('updated_at', record['queued_at'])})
                    else:
                        return latest
                return prior
            validate_current()
            batch = read(self.root / 'data/manual-ingestion.json')
            if action in ('scan', 'scan-new', 'start-automation') and batch.get('state') == 'running' and (time.time() - batch.get('updated_at', 0) < 180):
                raise APIError(409, '本批整理正在进行，无需重复点击')
            if action in ('scan', 'scan-new', 'start-automation') and any((read(p).get('action') in ('scan', 'scan-new', 'start-automation') for p in (self.root / 'data/requests').glob('console-*.json'))):
                raise APIError(409, '已有扫描请求等待执行，无需重复点击')
            result = {'request_id': rid, 'owner': user['Id'], 'action': action, 'ids': record['ids'], 'state': 'accepted', 'updated_at': time.time()}
            write(receipt, result)
            write(path, record)
            if action == 'start-automation':
                config = read(self.root / 'data/automation-config.json')
                config['enabled'] = True
                write(self.root / 'data/automation-config.json', config)
                launch_background(self.root)
        return result

    def open_settings(self, user, rid):
        # Settings controls all accounts and credentials. Workbench access alone
        # is deliberately insufficient; only an authenticated administrator may
        # request the local desktop action. No bootstrap key or URL is returned.
        if not (user.get('Policy') or {}).get('IsAdministrator'):
            raise APIError(403, '只有管理员可以在影院电脑上打开系统设置')
        receipt = self.root / 'data/task-receipts' / (rid + '.json')
        with self.lock:
            prior = read(receipt)
            if prior:
                if prior.get('owner') != user['Id'] or prior.get('action') != 'open-settings':
                    raise APIError(409, '请求编号已使用，请刷新后重试')
                return prior
            from personal_cinema.settings_launcher import open_settings
            from personal_cinema.runtime_paths import config_home_for_runtime
            try:
                launched = open_settings(config_home_for_runtime(self.root))
            except (OSError, ValueError):
                raise APIError(503, '系统设置暂时无法打开，请通过影院电脑的桌面入口重试') from None
            result = {'request_id': rid, 'owner': user['Id'], 'action': 'open-settings',
                      'ids': [], 'state': 'applied', 'updated_at': time.time(),
                      'message': launched['message']}
            write(receipt, result)
            return result

    def receipt(self, user, rid):
        if not re.fullmatch('[a-f0-9]{32}', rid):
            raise APIError(400, '无效请求编号')
        record = read(self.root / 'data/task-receipts' / (rid + '.json'))
        if not record or record.get('owner') != user['Id']:
            raise APIError(404, '请求记录不存在')
        return record

def respond(handler, status, value, content_type='application/json; charset=utf-8'):
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Referrer-Policy', 'no-referrer')
    handler.end_headers()
    if handler.command != 'HEAD':
        handler.wfile.write(data)

def route(handler, catalog, port):
    url = urllib.parse.urlsplit(handler.path)
    path = url.path.rstrip('/')
    q = urllib.parse.parse_qs(url.query)
    q = {**q, **{k: next((v for old, v in q.items() if old.lower() == k.lower()), ['']) for k in ('ParentId',)}}
    center = getattr(catalog, 'task_center', None)
    if center is None:
        with catalog.lock:
            if not hasattr(catalog, 'task_center'):
                catalog.task_center = Center(catalog.root)
            center = catalog.task_center
    if path in ('/MediaCache', '/MediaCache/app.js', '/MediaCache/app.css', '/MediaCache/manual-match.js', '/MediaCache/icon.png', '/MediaCache/manifest.json'):
        if handler.command not in ('GET', 'HEAD'):
            respond(handler, 405, {'error': '此地址只读'})
            return True
        names = {'/MediaCache': 'task-center.html', '/MediaCache/app.js': 'task-center.js', '/MediaCache/app.css': 'task-center.css', '/MediaCache/manual-match.js': 'task-manual-match.js', '/MediaCache/icon.png': 'task-center.png', '/MediaCache/manifest.json': 'task-center-manifest.json'}
        mime = {'/MediaCache': 'text/html; charset=utf-8', '/MediaCache/app.js': 'text/javascript; charset=utf-8', '/MediaCache/manual-match.js': 'text/javascript; charset=utf-8', '/MediaCache/app.css': 'text/css; charset=utf-8', '/MediaCache/icon.png': 'image/png', '/MediaCache/manifest.json': 'application/manifest+json'}
        from personal_cinema.runtime_paths import resource_path
        file = resource_path(names[path])
        if not file.is_file():
            respond(handler, 503, {'error': '任务中心资源尚未安装'})
            return True
        respond(handler, 200, file.read_bytes(), mime[path])
        return True
    receipt_match = re.fullmatch('/MediaCache/Receipts/([a-f0-9]{32})', path)
    is_api = path in ('/MediaCache/Tasks', '/MediaCache/Commands', '/MediaCache/MatchSearch', '/MediaCache/Match') or bool(receipt_match)
    match = re.fullmatch('(?:/Users/[^/]+)?/Items/(ca5e0000[a-f0-9]{24})(?:/(Images)/Primary)?', path, re.I)
    native = bool(match) or ((path.lower() in ('/items',) or re.fullmatch('/Users/[^/]+/Items', path, re.I)) and q.get('ParentId', [''])[0].startswith('ca5e0000'))
    if native:
        respond(handler, 404, {'error': '此状态入口已移到缓存管理网页', 'management_url': '/MediaCache/'})
        return True
    if not is_api:
        return False
    try:
        headers = auth_headers(handler.headers, url.query)
        user = authorize(catalog.root, port, headers)
        entries, _ = catalog.snapshot()
        items = center.library(port, headers, user)
        if is_api:
            if receipt_match:
                if handler.command not in ('GET', 'HEAD'):
                    raise APIError(405, '请求记录只读')
                respond(handler, 200, center.receipt(user, receipt_match[1]))
                return True
            if path == '/MediaCache/Tasks':
                if handler.command not in ('GET', 'HEAD'):
                    raise APIError(405, '此地址只读')
                result = center.snapshot(entries, items)
                result['capabilities'] = {'open_settings': bool((user.get('Policy') or {}).get('IsAdministrator'))}
                respond(handler, 200, result)
                return True
            if handler.command != 'POST':
                raise APIError(405, '操作需要 POST 请求')
            origin = handler.headers.get('Origin')
            if origin and origin != 'http://' + handler.headers.get('Host', '') and (origin != 'https://' + handler.headers.get('Host', '')):
                raise APIError(403, '操作来源无效')
            if 'application/json' not in handler.headers.get('Content-Type', ''):
                raise APIError(415, '需要 JSON 请求')
            if int(handler.headers.get('Content-Length', '0')) > 16384:
                raise APIError(413, '请求过大')
            try:
                payload = json.loads(handler.read_body())
            except (ValueError, UnicodeError):
                raise APIError(400, '请求格式错误')
            if path in ('/MediaCache/MatchSearch', '/MediaCache/Match'):
                if not isinstance(payload, dict):
                    raise APIError(400, '请求格式错误')
                from manual_match import search, queue
                try:
                    result = (search if path.endswith('MatchSearch') else queue)(catalog.root, entries, items, user, payload)
                except ValueError as error:
                    raise APIError(400, str(error)) from None
                except (OSError, RuntimeError):
                    raise APIError(503, '作品匹配服务暂不可用，请稍后重试') from None
                respond(handler, 200 if path.endswith('MatchSearch') else 202, result)
                return True
            respond(handler, 202, center.command(entries, items, user, payload))
            return True
    except APIError as e:
        respond(handler, e.status, {'error': e.message})
        return True
    except (OSError, ValueError, TypeError, KeyError):
        respond(handler, 503, {'error': '任务状态暂不可用，请刷新后重试'})
        return True

def augment_views(value, path, root, user_id=None):
    """Management is a webpage, never a synthetic playable media library."""
    url = urllib.parse.urlsplit(path)
    legacy = re.fullmatch('/Users/([^/]+)/Views', url.path, re.I)
    if not (legacy or url.path.lower() == '/userviews') or not isinstance(value, dict) or (not isinstance(value.get('Items'), list)):
        return value
    items = [item for item in value['Items'] if item.get('Id') != ROOT_ID]
    if len(items) == len(value['Items']):
        return value
    result = dict(value)
    result['Items'] = items
    result['TotalRecordCount'] = max(0, value.get('TotalRecordCount', len(value['Items'])) - (len(value['Items']) - len(items)))
    return result
