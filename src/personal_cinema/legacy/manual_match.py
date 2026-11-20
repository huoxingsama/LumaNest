"""Owner-bound candidate tickets and durable user-confirmed work identities."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import contextlib
import json
import re
import secrets
import shutil
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from personal_cinema.runtime_paths import RuntimePath as Path
from library_tasks import read, write
from library_routing import catalog_relative
from cache_policy import cache_path
HEX = re.compile('^[a-f0-9]{32}$')
_LOCK = threading.RLock()
TTL = 3600

@contextlib.contextmanager
def _identity_guard(root, ident):
    import msvcrt
    folder = Path(root) / 'data/manual-match-locks'
    folder.mkdir(parents=True, exist_ok=True)
    if not HEX.fullmatch(ident):
        raise ValueError('作品编号无效')
    handle = (folder / (ident + '.lock')).open('a+b')
    if handle.seek(0, 2) == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise RuntimeError('Manual identity update already in progress') from None
    try:
        yield
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()

def _url(value):
    if value is None or value == '':
        return ''
    if not isinstance(value, str) or len(value) > 1800:
        raise ValueError('百科链接格式无效')
    p = urllib.parse.urlsplit(value.strip())
    allowed = p.hostname == 'baike.baidu.com' and p.path.startswith('/item/') and (len(p.path) > 6) or (p.hostname == 'zh.wikipedia.org' and any((p.path.startswith(s) and len(p.path) > len(s) for s in ('/wiki/', '/zh-hans/', '/zh-cn/', '/zh-tw/'))))
    if p.scheme != 'https' or p.username or p.password or (p.port not in (None, 443)) or (not allowed):
        raise ValueError('请使用百度百科词条或中文维基百科的 HTTPS 链接')
    return urllib.parse.urlunsplit(('https', p.hostname, p.path, '', ''))

def _group(root, entries, items, group_id):
    from task_center_api import Center
    if not isinstance(group_id, str) or not HEX.fullmatch(group_id):
        raise ValueError('该目录仍等待建立媒体索引，请稍后再手动匹配')
    item = next((i for i in items if i.get('Id') == group_id and i.get('Type') in ('Movie', 'Series')), None)
    if item is None:
        raise ValueError('当前账号无法访问这部作品，或作品索引已变化')
    visible = Center(Path(root)).visible_entries(entries, items)
    selected = {eid: e for eid, (e, i) in visible.items() if (i.get('SeriesId') if i.get('Type') == 'Episode' else i.get('Id')) == group_id}
    if not selected:
        raise ValueError('这部作品尚未关联视频文件，请先完成索引')
    return (item, selected)

def search(root, entries, items, user, payload):
    from metadata_matcher import Jellyfin
    root = Path(root)
    item, selected = _group(root, entries, items, payload.get('group_id'))
    kind = payload.get('kind') or item['Type']
    if kind != item['Type']:
        raise ValueError('请选择与当前作品一致的电影或剧集类型')
    query = payload.get('query')
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 160:
        raise ValueError('请输入作品名称，最长160字')
    try:
        found = Jellyfin(root).json('/Items/RemoteSearch/' + kind, {'SearchInfo': {'Name': query.strip(), 'MetadataLanguage': 'zh-CN'}, 'ItemId': item['Id'], 'SearchProviderName': 'TheMovieDb', 'IncludeDisabledProviders': False})
    except (OSError, RuntimeError):
        raise ValueError('作品搜索服务暂不可用，请稍后重试') from None
    out = []
    seen = set()
    folder = root / 'data/manual-match-tickets'
    folder.mkdir(parents=True, exist_ok=True)
    for candidate in found if isinstance(found, list) else []:
        ident = str((candidate.get('ProviderIds') or {}).get('Tmdb') or '')
        if not ident.isdecimal() or ident in seen:
            continue
        seen.add(ident)
        token = secrets.token_hex(16)
        write(folder / (token + '.json'), {'token': token, 'owner': user['Id'], 'group_id': item['Id'], 'kind': kind, 'entry_ids': sorted(selected), 'created_at': time.time(), 'expires_at': time.time() + TTL, 'candidate': candidate})
        out.append({'token': token, 'name': candidate.get('Name') or query.strip(), 'original_name': candidate.get('OriginalTitle') or candidate.get('OriginalName') or '', 'year': candidate.get('ProductionYear'), 'kind': kind, 'provider_id': ident})
        if len(out) >= 12:
            break
    provider = str((item.get('ProviderIds') or {}).get('Tmdb') or '')
    override = read(root / 'data/manual-metadata-overrides.json').get('works', {}).get(kind + ':' + provider, {})
    return {'candidates': out, 'current': {'name': item['Name'], 'kind': kind, 'provider_id': provider, 'reference_url': override.get('reference_url', '')}}

def queue(root, entries, items, user, payload):
    root = Path(root)
    rid = payload.get('request_id')
    if not isinstance(rid, str) or not HEX.fullmatch(rid):
        raise ValueError('请求编号无效，请重新提交')
    url = _url(payload.get('reference_url'))
    submission_hash = hashlib.sha256(json.dumps({'group_id': payload.get('group_id'), 'candidate_token': payload.get('candidate_token') or '', 'reference_url': url}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    receipt = root / 'data/task-receipts' / (rid + '.json')
    request = root / 'data/requests' / ('manual-match-' + rid + '.json')
    submission = root / 'data/manual-match-submissions' / (rid + '.json')
    with _LOCK:
        old = read(receipt)
        if old and old.get('submission_hash'):
            if old.get('owner') != user['Id'] or old['submission_hash'] != submission_hash:
                raise ValueError('请求编号已被其他操作使用，请重新提交')
            if old.get('state') == 'accepted' and (not request.exists()):
                saved = read(submission)
                if saved.get('user_id') != user['Id'] or saved.get('request_id') != rid:
                    raise ValueError('未能恢复排队内容，请重新提交手动匹配')
                write(request, saved)
            return old
    item, selected = _group(root, entries, items, payload.get('group_id'))
    candidate = None
    token = payload.get('candidate_token')
    if token:
        if not isinstance(token, str) or not HEX.fullmatch(token):
            raise ValueError('候选已失效，请重新搜索')
        ticket = read(root / 'data/manual-match-tickets' / (token + '.json'))
        if ticket.get('owner') != user['Id'] or ticket.get('group_id') != item['Id'] or ticket.get('kind') != item['Type'] or (ticket.get('expires_at', 0) < time.time()) or (ticket.get('entry_ids') != sorted(selected)):
            raise ValueError('候选已过期或作品内容已变化，请重新搜索')
        candidate = ticket.get('candidate')
    if not candidate and (not str((item.get('ProviderIds') or {}).get('Tmdb') or '').isdecimal()):
        raise ValueError('请先选择正确作品，再补充百科链接')
    record = {'action': 'manual-match', 'request_id': rid, 'group_id': item['Id'], 'ids': sorted(selected), 'user_id': user['Id'], 'kind': item['Type'], 'candidate': candidate, 'reference_url': url, 'queued_at': time.time()}
    fingerprint = hashlib.sha256(json.dumps({k: record[k] for k in ('group_id', 'ids', 'kind', 'candidate', 'reference_url')}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    receipt = root / 'data/task-receipts' / (rid + '.json')
    with _LOCK:
        old = read(receipt)
        if old:
            if old.get('owner') != user['Id'] or old.get('fingerprint') != fingerprint:
                raise ValueError('请求编号已被其他操作使用，请重新提交')
            return old
        result = {'request_id': rid, 'owner': user['Id'], 'action': 'manual-match', 'ids': record['ids'], 'group_id': item['Id'], 'state': 'accepted', 'stage': 'queued', 'message': '手动匹配已排队，正在等待处理', 'fingerprint': fingerprint, 'submission_hash': submission_hash, 'created_at': time.time(), 'updated_at': time.time()}
        write(submission, record)
        write(receipt, result)
        write(request, record)
    return result

def override_for_item(root, item, entries):
    rules = read(Path(root) / 'data/manual-metadata-overrides.json')
    matched = []
    try:
        path = Path(item['Path']).resolve()
        catalog = (Path(root) / 'catalog').resolve()
        path.relative_to(catalog)
    except (KeyError, ValueError):
        return None
    for ident, entry in entries.items():
        if entry.get('catalog_hidden') or entry.get('source_missing'):
            continue
        rel = Path(catalog_relative(entry))
        p = (catalog / rel).with_suffix('.strm').resolve()
        applies = p == path if item['Type'] == 'Movie' else p.is_relative_to(path)
        record = rules.get('entries', {}).get(ident)
        if applies and record and (record.get('kind') == item['Type']):
            matched.append(record)
    identities = {str((r.get('provider_ids') or {}).get('Tmdb')) for r in matched}
    if len(identities) > 1:
        raise ValueError('本目录包含不同的手动作品规则，请先拆分对应内容')
    if matched:
        return max(matched, key=lambda r: r.get('confirmed_at', 0))
    key = item['Type'] + ':' + str((item.get('ProviderIds') or {}).get('Tmdb') or '')
    return rules.get('works', {}).get(key)

def apply_identity(root, client, item, record, request_id='automatic'):
    """Keep the item's ID/path; user data stays attached to the same item."""
    with _identity_guard(root, item['Id']):
        return _apply_identity(root, client, client.json('/Items/' + item['Id']), record, request_id)

def _apply_identity(root, client, item, record, request_id):
    root = Path(root)
    ids = record['provider_ids']
    tmdb = str(ids.get('Tmdb') or '')
    if item['Type'] != record['kind'] or not tmdb.isdecimal():
        raise ValueError('手动规则与作品类型不一致')
    p = Path(item['Path']).resolve()
    relative = p.relative_to((root / 'catalog').resolve()).as_posix()
    cache_path(root / 'catalog', relative)
    nfo = p / 'tvshow.nfo' if item['Type'] == 'Series' else p.with_suffix('.nfo')
    mismatch = str((item.get('ProviderIds') or {}).get('Tmdb') or '') != tmdb
    identity_nfo = {}
    if nfo.is_file():
        try:
            identity_nfo = {n.get('type'): n.text for n in ET.parse(nfo).getroot().findall('uniqueid')}
        except ET.ParseError:
            pass
    if not mismatch and str(identity_nfo.get('tmdb') or '') == tmdb:
        return item
    backup = root / 'data/manual-match-backups' / request_id / item['Id']
    backup.mkdir(parents=True, exist_ok=True)
    if not (backup / 'item-before.json').exists():
        write(backup / 'item-before.json', item)
    if mismatch:
        candidate = record.get('candidate') or {'Name': record.get('name') or item['Name'], 'ProductionYear': record.get('year'), 'ProviderIds': ids, 'SearchProviderName': 'TheMovieDb'}
        latest = {**item, 'IsLocked': False, 'LockedFields': [], 'ProviderIds': dict(ids)}
        client.json('/Items/' + item['Id'], latest, method='POST')
        client.json('/Items/RemoteSearch/Apply/' + item['Id'] + '?replaceAllImages=true', candidate)
        item = client.json('/Items/' + item['Id'])
        if str((item.get('ProviderIds') or {}).get('Tmdb') or '') != tmdb:
            raise ValueError('媒体库尚未接受所选作品，已保留手动规则待继续')
    if nfo.is_file():
        shutil.copy2(nfo, backup / ('old-' + str(time.time_ns()) + '.nfo'))
    node = ET.Element('tvshow' if item['Type'] == 'Series' else 'movie')
    for key, val in (('title', item.get('Name') or record.get('name')), ('originaltitle', item.get('OriginalTitle')), ('year', item.get('ProductionYear') or record.get('year')), ('plot', item.get('Overview'))):
        if val is not None:
            ET.SubElement(node, key).text = str(val)
    for provider, value in ids.items():
        ET.SubElement(node, 'uniqueid', {'type': provider.lower(), 'default': 'true' if provider == 'Tmdb' else 'false'}).text = str(value)
    ET.SubElement(node, 'lockdata').text = 'false'
    nfo.parent.mkdir(parents=True, exist_ok=True)
    temp = nfo.with_name(nfo.name + '.manual.tmp')
    ET.ElementTree(node).write(temp, encoding='utf-8', xml_declaration=True)
    temp.replace(nfo)
    return item

def process_request(worker, path, payload):
    if getattr(worker, 'ingestion_running', False):
        return False
    from metadata_matcher import Jellyfin
    root = worker.root
    rid = payload['request_id']
    receipt = root / 'data/task-receipts' / (rid + '.json')

    def phase(stage, message, state='running', **extra):
        write(receipt, {**read(receipt), 'state': state, 'stage': stage, 'message': message, 'updated_at': time.time(), **extra})
    if payload.get('next_attempt_at', 0) > time.time():
        return False
    if any((worker.entries.get(i, {}).get('playback_active') or worker.active.get(i) for i in payload['ids'])):
        phase('waiting', '本片正在播放，退出播放页后继续匹配', 'waiting')
        return False
    transfer = getattr(worker, 'downloading', None) or getattr(worker, 'transfer_entry_id', None)
    if transfer:
        interrupt = getattr(worker, 'transfer_interrupt', None)
        if interrupt:
            interrupt()
        phase('waiting', '正在让后台下载暂停，随后处理手动匹配', 'waiting')
        return False
    was_running = getattr(worker, 'ingestion_running', False)
    if was_running:
        return False
    import importlib, manual_ingest
    stamp = Path(manual_ingest.__file__).stat().st_mtime_ns
    if getattr(manual_ingest, '_manual_match_loaded_mtime', None) != stamp:
        manual_ingest = importlib.reload(manual_ingest)
        manual_ingest._manual_match_loaded_mtime = stamp
    manual_ingest.prepare_runtime()
    worker.ingestion_running = True
    try:
        client = Jellyfin(root)
        uid = payload['user_id']
        user = client.json('/Users/' + uid)
        if user.get('Policy', {}).get('IsDisabled'):
            raise ValueError('当前账号已停用')
        item = client.json('/Users/' + uid + '/Items/' + payload['group_id'])
        if item.get('Type') != payload['kind']:
            raise ValueError('作品类型已变化，请重新选择')
        selected = [worker.entries[i] for i in payload['ids'] if i in worker.entries and (not worker.entries[i].get('source_missing')) and (not worker.entries[i].get('catalog_hidden'))]
        if len(selected) != len(payload['ids']):
            raise ValueError('部分源文件已移除，请刷新工作台后重新选择')
        catalog = (root / 'catalog').resolve()
        native = Path(item['Path']).resolve()
        native.relative_to(catalog)
        for e in selected:
            p = (catalog / Path(catalog_relative(e))).with_suffix('.strm').resolve()
            if not (p == native if item['Type'] == 'Movie' else p.is_relative_to(native)):
                raise ValueError('文件所属作品已变化，请重新提交手动匹配')
        candidate = payload.get('candidate')
        ids = (candidate or {}).get('ProviderIds') or item.get('ProviderIds') or {}
        ids = {k: str(v) for k, v in ids.items() if k in ('Tmdb', 'Imdb', 'Tvdb') and v}
        if not str(ids.get('Tmdb') or '').isdecimal():
            raise ValueError('尚未取得有效作品编号')
        phase('identity', '正在应用已确认作品并保存长期识别规则')
        target = root / 'data/manual-metadata-overrides.json'
        rules = read(target)
        rules.setdefault('entries', {})
        rules.setdefault('works', {})
        key = item['Type'] + ':' + ids['Tmdb']
        prior = rules['works'].get(key, {})
        record = {'kind': item['Type'], 'provider_ids': ids, 'name': (candidate or {}).get('Name') or item['Name'], 'year': (candidate or {}).get('ProductionYear') or item.get('ProductionYear'), 'reference_url': _url(payload.get('reference_url')) or prior.get('reference_url', ''), 'confirmed_by': uid, 'confirmed_at': time.time(), 'request_id': rid}
        backup = root / 'data/manual-match-backups' / rid
        backup.mkdir(parents=True, exist_ok=True)
        if target.is_file() and (not (backup / target.name).exists()):
            shutil.copy2(target, backup / target.name)
        for e in selected:
            rules['entries'][e['id']] = {**record, 'candidate': candidate}
        rules['works'][key] = record
        rules['version'] = 1
        rules['updated_at'] = time.time()
        write(target, rules)
        updated = apply_identity(root, client, client.json('/Items/' + item['Id']), {**record, 'candidate': candidate}, rid)
        for e in selected:
            if e.get('season') != 99:
                e['identity_needs_review'] = False
            e['manual_identity_confirmed_at'] = record['confirmed_at']
        if str((item.get('ProviderIds') or {}).get('Tmdb') or '') != ids['Tmdb']:
            bindings_path = root / 'data/source-bindings.json'
            bindings = read(bindings_path)
            for e in selected:
                if e['id'] in bindings:
                    bindings[e['id']]['normalized'] = False
            write(bindings_path, bindings)
        worker.save()
        phase('metadata', '身份已保存，正在补齐海报、中文演职员、评分和目录分类')
        outcome = manual_ingest.repair_selected(worker, [e['id'] for e in selected], {updated['Id']}) or {}
        state = 'complete' if outcome.get('state') == 'complete' else 'partial'
        phase('complete' if state == 'complete' else 'metadata', outcome.get('message') or ('手动匹配及本片资料整理已完成' if state == 'complete' else '手动身份已保存，部分资料仍待补齐'), 'applied' if state == 'complete' else 'partial', result=outcome)
        return True
    except Exception as error:
        attempts = int(payload.get('attempts', 0)) + 1
        message = str(error) if isinstance(error, ValueError) else '资料服务暂不可用，已保留手动选择与已完成结果'
        if not isinstance(error, ValueError) and attempts < 3:
            write(path, {**payload, 'attempts': attempts, 'next_attempt_at': time.time() + 60 * attempts})
            phase('retry', message, 'waiting', retry_at=time.time() + 60 * attempts)
            return False
        phase('metadata', message[:240], 'partial', error_type=type(error).__name__)
        return True
    finally:
        worker.ingestion_running = was_running
