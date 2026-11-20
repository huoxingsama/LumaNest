"""Bounded portrait enrichment for real Jellyfin people, independent of billing.

Person records are deduplicated and retried separately from a work's Chinese
cast list. A failed portrait lookup never changes names, characters or order.
"""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import base64
import concurrent.futures
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import secrets
import ssl
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from personal_cinema.runtime_paths import RuntimePath as Path
from PIL import Image
from chinese_credits import clean
from library_tasks import read, write
VERSION = '2026-11-09.10'
MAX_IMAGE_BYTES = 8 * 1024 * 1024
PHOTO_HOSTS = ('tmdb.org', 'thetvdb.com', 'wikimedia.org', 'media-amazon.com', 'imdb.com', 'myanimelist.net', 'anilist.co', 'cdn.hk01.com', 'bkimg.cdn.bcebos.com', 'baike.bdimg.com', 'www.unitedagents.co.uk', 'corocoro-news.jp')
PERSON_KEYS = ('Tmdb', 'Imdb', 'Tvdb')

def close_without_photo(record, reason='no-reliable-photo-after-search'):
    """A real completed lookup with no portrait, never a fabricated image."""
    return {**record, 'status': 'no_photo', 'display_status': '暂无照片', 'closed_at': time.time(), 'closure_reason': reason, 'previous_status': record.get('previous_status') or record.get('status'), 'next_attempt_at': None}

def summarize_work(title, ids, people_state, now=None):
    now = time.time() if now is None else now
    counts = dict.fromkeys(('applied', 'no_photo', 'pending', 'retry', 'unavailable', 'ambiguous'), 0)
    missing_people = []
    for ident in ids:
        person = people_state.get(ident, {})
        status = person.get('status', 'pending')
        counts[status if status in counts else 'pending'] += 1
        if status != 'applied':
            missing_people.append({'id': ident, 'name': person.get('name', ''), 'status': status, 'reason': person.get('closure_reason') or person.get('reason'), 'next_attempt_at': person.get('next_attempt_at')})
    missing = len(ids) - counts['applied']
    actionable = missing - counts['no_photo']
    return {'title': title, 'total': len(ids), 'missing': missing, 'actionable_missing': actionable, **counts, 'person_ids': ids, 'missing_people': missing_people, 'status': 'applied' if missing == 0 else 'complete' if actionable == 0 else 'review' if counts['ambiguous'] else 'pending', 'version': VERSION, 'next_attempt_at': min((p.get('next_attempt_at') or now for p in missing_people if p['status'] != 'no_photo'), default=None), 'updated_at': now}

def _error_reason(error):
    reason = getattr(error, 'reason', None) or getattr(error, 'code', None)
    return str(reason)[:120] if isinstance(reason, (str, int, float)) else type(reason or error).__name__

def _open_photo(request):
    try:
        return urllib.request.urlopen(request, timeout=18)
    except urllib.error.URLError as error:
        if not isinstance(error.reason, ssl.SSLCertVerificationError):
            raise
        try:
            import certifi
        except ImportError:
            raise error
        return urllib.request.urlopen(request, timeout=18, context=ssl.create_default_context(cafile=certifi.where()))

def _api(client, path, data=None, method=None):
    if hasattr(client, 'json'):
        return client.json(path, data, method=method)
    return client.api('jellyfin', path, data, method=method)

def _key(value):
    text = clean(unicodedata.normalize('NFKC', str(value or '')))
    text = unicodedata.normalize('NFKD', text)
    return ''.join((c for c in text.casefold() if c.isalnum()))

def _providers(person):
    return {k: str(v) for k, v in (person.get('ProviderIds') or {}).items() if k in PERSON_KEYS and v}

def _photo(person):
    return bool(person.get('PrimaryImageTag') or (person.get('ImageTags') or {}).get('Primary'))

def _same_provider(left, right):
    a, b = (_providers(left), _providers(right))
    return bool(a and b and any((k in b and v == b[k] for k, v in a.items())) and (not any((k in b and v != b[k] for k, v in a.items()))))

def _context_key(work):
    return str(work.get('Type')) + ':' + str((work.get('ProviderIds') or {}).get('Tmdb') or work.get('Path') or work.get('Id'))

@contextlib.contextmanager
def _state_lock(path):
    """One portrait writer per library, without blocking ingestion hooks."""
    handle = path.open('a+b')
    if handle.seek(0, 2) == 0:
        handle.write(b'\x00')
        handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        handle.seek(0)
        if os.name == 'nt':
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

def _aliases(root):
    try:
        from repair_cast_portraits import ALIASES
    except ImportError:
        ALIASES = {}
    values = dict(ALIASES)
    legacy = read(root / 'data/cast-portrait-repairs.json', [])
    for row in legacy if isinstance(legacy, list) else []:
        if row.get('status') == 'applied' and row.get('name') and row.get('verified_alias'):
            values.setdefault(row['name'], row['verified_alias'])
    values.update(read(root / 'data/person-portrait-aliases.json'))
    return values

def _backup_index(root):
    """Index small cast records once per batch, including retired native IDs."""
    folder = root / 'data/chinese-credits-backups'
    cache_path = root / 'data/person-portrait-backups-index.json'
    cached = read(cache_path)
    previous = cached.get('files', {}) if cached.get('version') == 1 else {}
    files, by_work = ({}, {})
    for path in folder.glob('*/item-before.json'):
        try:
            stat = path.stat()
        except OSError:
            continue
        key = path.parent.name
        entry = previous.get(key, {})
        if entry.get('mtime_ns') != stat.st_mtime_ns or entry.get('size') != stat.st_size:
            old = read(path)
            work = {'Type': old.get('Type'), 'ProviderIds': _providers(old), 'People': [{field: person[field] for field in ('Id', 'Name', 'ProviderIds', 'PrimaryImageTag', 'ImageTags') if field in person} for person in old.get('People') or [] if isinstance(person, dict)]}
            entry = {'mtime_ns': stat.st_mtime_ns, 'size': stat.st_size, 'work': work}
        files[key] = entry
        work = entry.get('work') or {}
        tmdb = _providers(work).get('Tmdb')
        if tmdb and work.get('Type') in ('Movie', 'Series', 'Season'):
            by_work.setdefault((work['Type'], tmdb), []).append(work)
    if files != previous or cached.get('version') != 1:
        write(cache_path, {'version': 1, 'files': files, 'updated_at': time.time()})
    return by_work

def _allowed_photo_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    return parsed.scheme == 'https' and any((host == h or host.endswith('.' + h) for h in PHOTO_HOSTS))

def _cache_image(folder, key, data, group=False):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Portrait bytes outside limit')
    with Image.open(io.BytesIO(data)) as original:
        if original.width * original.height > 24000000:
            raise ValueError('Portrait pixel count outside limit')
        if original.width < 90 or original.height < 100 or (not 0.32 <= original.width / original.height <= (3.2 if group else 1.7)):
            raise ValueError('Image is not a usable person portrait')
        original.load()
        image = original.convert('RGB')
        image.thumbnail((1200, 1600), Image.Resampling.LANCZOS)
        path = folder / (hashlib.sha256(key.encode()).hexdigest()[:32] + '.jpg')
        temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
        image.save(temporary, format='JPEG', quality=93)
        temporary.replace(path)
    return path

def _download(folder, match, client):
    ids = _providers(match)
    identity = json.dumps(ids, sort_keys=True) if ids else str(match['ImageUrl'])
    crop = match.get('_portrait_crop')
    if crop:
        identity += ':crop:' + json.dumps(crop)
    path = folder / (hashlib.sha256(identity.encode()).hexdigest()[:32] + '.jpg')
    if path.is_file():
        return path
    url = match['ImageUrl']
    if '.tmdb.org/' in url:
        url = re.sub('/t/p/[^/]+/', '/t/p/original/', url)
    data = None
    if match.get('_portrait_source_file'):
        original = Path(match['_portrait_source_file'])
        original = (original if original.is_absolute() else folder.parent / original).resolve()
        original.relative_to((folder.parent / 'portrait-source-originals').resolve())
        if original.is_file():
            if original.stat().st_size > MAX_IMAGE_BYTES:
                raise ValueError('Portrait original exceeds limit')
            data = original.read_bytes()
            if hashlib.sha256(data).hexdigest() != match.get('_portrait_source_sha256'):
                raise ValueError('Portrait original checksum changed')
    if data is None:
        if not _allowed_photo_url(url):
            raise ValueError('Untrusted portrait host')
        headers = {'User-Agent': 'PersonalMediaLibrary/1.0'}
        referer = (match.get('_photo_evidence') or {}).get('source_url', '')
        if urllib.parse.urlsplit(referer).hostname == 'baike.baidu.com':
            headers['Referer'] = referer
        request = urllib.request.Request(url, headers=headers)
        with _open_photo(request) as response:
            if not _allowed_photo_url(response.geturl()):
                raise ValueError('Portrait redirect host changed')
            data = response.read(MAX_IMAGE_BYTES + 1)
    if crop:
        with Image.open(io.BytesIO(data)) as original:
            if list(original.size) != match.get('_portrait_original_size'):
                raise ValueError('Portrait crop source dimensions changed')
            if len(crop) != 4 or not all((isinstance(v, int) for v in crop)):
                raise ValueError('Invalid portrait crop')
            left, top, right, bottom = crop
            if not (0 <= left < right <= original.width and 0 <= top < bottom <= original.height):
                raise ValueError('Portrait crop outside source image')
            output = io.BytesIO()
            original.crop(crop).convert('RGB').save(output, format='JPEG', quality=95)
            data = output.getvalue()
    return _cache_image(folder, identity, data, group=bool(match.get('_portrait_group')))

def _upload(client, ident, path):
    """Use the existing authenticated person image endpoint, without renaming it."""
    for attempt in range(2):
        if not getattr(client, 'token', None):
            _api(client, '/System/Info')
        request = urllib.request.Request(_cinema_endpoint('jellyfin') + '/Items/' + urllib.parse.quote(ident, safe='') + '/Images/Primary', data=base64.b64encode(path.read_bytes()), headers={'Authorization': 'MediaBrowser Token="' + client.token + '"', 'Content-Type': 'image/jpeg'}, method='POST')
        try:
            with client.http.open(request, timeout=30) as response:
                if response.status != 204:
                    raise ValueError('Portrait upload not accepted')
            return
        except urllib.error.HTTPError as error:
            if error.code != 401 or attempt:
                raise
            client.token = None

def _search_match(client, person, names, backup_casts=None):
    wanted = {_key(n) for n in names if n}
    providers = _providers(person)
    candidates = {}
    work_candidates = {}
    for name in list(dict.fromkeys(names))[:4]:
        info = {'Name': name, 'MetadataLanguage': 'zh-CN'}
        if providers:
            info['ProviderIds'] = providers
        found = _api(client, '/Items/RemoteSearch/Person', {'SearchInfo': info, 'IncludeDisabledProviders': False}) or []
        for match in found:
            if not providers and re.search('[\\u3400-\\u9fff]', name):
                for work_id, cast in (backup_casts or {}).items():
                    same_name = [p for p in cast if _key(p.get('Name')) == _key(match.get('Name')) and _key(p.get('Name'))]
                    compatible = []
                    for old in same_name:
                        a, b = (_providers(old), _providers(match))
                        if not any((k in b and v != b[k] for k, v in a.items())):
                            compatible.append(old)
                    if compatible:
                        ids = _providers(match)
                        key = json.dumps(ids, sort_keys=True) if ids else _key(match.get('Name'))
                        group = work_candidates.setdefault(work_id, {})
                        shared = next((k for k, old in group.items() if _same_provider(old, match)), None)
                        if shared is not None:
                            old = group[shared]
                            group[shared] = {**old, **match, 'ProviderIds': {**_providers(old), **ids}}
                        else:
                            group[key] = match
            if providers:
                valid = _same_provider(person, match)
            else:
                valid = _key(match.get('Name')) in wanted
            if not valid:
                continue
            ids = _providers(match)
            shared = next((key for key, old in candidates.items() if _same_provider(old, match)), None)
            if shared is not None:
                old = candidates[shared]
                candidates[shared] = {**old, 'ProviderIds': {**_providers(old), **ids}}
                continue
            key = json.dumps(ids, sort_keys=True) if ids else _key(match.get('Name'))
            candidates[key] = match
        if len(candidates) == 1:
            match = next(iter(candidates.values()))
            if match.get('ImageUrl'):
                return (match, None)
        if len(candidates) > 1:
            return (None, 'ambiguous')
    members = {}
    for work_id, group in work_candidates.items():
        if len(group) > 1:
            return (None, 'ambiguous')
        for match in group.values():
            shared = next((k for k, old in members.items() if _same_provider(old, match)), None)
            key = json.dumps(_providers(match), sort_keys=True) if _providers(match) else _key(match.get('Name'))
            if shared is None:
                members[key] = {**match, '_verified_work_id': work_id}
    if len(members) > 1:
        return (None, 'ambiguous')
    if members:
        match = next(iter(members.values()))
        if match.get('ImageUrl'):
            return (match, None)
    return (None, 'unavailable')

def _repair(root, client, person, backups, aliases, prior, works=None):
    ident = person['Id']
    now = time.time()
    path = None
    base = {'name': person.get('Name', ''), 'checked_at': now, 'version': VERSION, 'checked_contexts': sorted(set(prior.get('checked_contexts') or []) | {_context_key(w) for w in works or []})}
    try:
        full = _api(client, '/Items/' + urllib.parse.quote(ident, safe=''))
        if _photo(full):
            return {**prior, **base, 'status': 'applied', 'source': prior.get('source') or 'existing-person', 'provider_ids': _providers(full) or prior.get('provider_ids', {})}
        current = {**person, **full}
        base['provider_ids'] = _providers(current)
        names = [person.get('Name', '')]
        for record in (person, full):
            if record.get('Name'):
                names.append(record['Name'])
            for field in ('Aliases', 'OriginalName'):
                value = record.get(field)
                if isinstance(value, str):
                    names.append(value)
                elif isinstance(value, list):
                    names.extend((v for v in value if isinstance(v, str)))
        alias = aliases.get(person.get('Name', ''))
        if isinstance(alias, str):
            names.append(alias)
        elif isinstance(alias, list):
            names.extend((v for v in alias if isinstance(v, str)))
        names.extend((re.sub('[<〈（(][^>〉）)]*[>〉）)]', '', name).strip() for name in list(names)))
        names = list(dict.fromkeys((name for name in names if name)))
        if prior.get('identity_verified'):
            names.extend(prior.get('verified_aliases') or [])
            if prior.get('provider_ids'):
                current = {**current, 'ProviderIds': prior['provider_ids']}
        eligible = [p for cast in backups.values() for p in cast if p.get('Id') and p['Id'] != ident and (_same_provider(current, p) or _key(p.get('Name')) in {_key(n) for n in names})]
        source_ids = {p['Id'] for p in eligible if _photo(p)}
        if len(source_ids) == 1 and hasattr(client, 'image'):
            source_id = next(iter(source_ids))
            data = client.image('/Items/' + source_id + '/Images/Primary')
            path = _cache_image(root / 'data/person-portraits', 'native:' + source_id, data)
            _upload(client, ident, path)
            return {**base, 'status': 'applied', 'source': 'same-work-person', 'source_person_id': source_id, 'file': path.name, 'provider_ids': _providers(current)}
        lookup_errors = []
        path = None
        reason = 'unavailable'
        failed_photo_urls = set(prior.get('failed_photo_urls') or []) if prior.get('failed_photo_retry_at', 0) > now else set()
        if failed_photo_urls:
            base.update(failed_photo_urls=sorted(failed_photo_urls), failed_photo_retry_at=prior['failed_photo_retry_at'])
        base['attempted_sources'] = []

        def record_error(label, error):
            lookup_errors.append(error)
            base.setdefault('source_errors', []).append({'source': label, 'reason': _error_reason(error)})
            if getattr(error, 'source_results', None):
                base['source_results'] = error.source_results

        def accept(candidate, label):
            nonlocal path
            if label not in base['attempted_sources']:
                base['attempted_sources'].append(label)
            if not candidate:
                return None
            if candidate.get('ImageUrl') in failed_photo_urls and (not candidate.get('_portrait_source_file')):
                return None
            try:
                path = _download(root / 'data/person-portraits', candidate, client)
                if candidate.get('_photo_evidence'):
                    base['photo_evidence'] = candidate['_photo_evidence']
                if candidate.get('source_results'):
                    base['source_results'] = candidate['source_results']
                return candidate
            except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException, Image.DecompressionBombError) as error:
                if candidate.get('ImageUrl'):
                    failed_photo_urls.add(candidate['ImageUrl'])
                base.update(failed_photo_urls=sorted(failed_photo_urls), failed_photo_retry_at=now + 3600)
                record_error(label, error)
                return None
        match = None
        try:
            from portrait_sources import _registered_photo
            match = accept(_registered_photo(root, _providers(current), failed_photo_urls, person, works), '已核对的官方人物照片')
        except (OSError, ValueError, TypeError, KeyError) as error:
            record_error('已核对的官方人物照片', error)
        try:
            from portrait_baike import resolve as resolve_baike
            for _ in range(0 if match else 2):
                found = resolve_baike(root, client, person, works or [], exclude_urls=failed_photo_urls)
                match = accept(found, '百科演员卡与人物页')
                if match or not found:
                    break
        except (OSError, ValueError, TypeError, KeyError) as error:
            record_error('百科演员卡与人物页', error)
        if match is None:
            try:
                found, reason = _search_match(client, current, names, backups)
                match = accept(found, '影视资料源')
            except (OSError, ValueError, TypeError, KeyError) as error:
                record_error('影视资料源', error)
        if match is None:
            from portrait_identity import resolve
            try:
                evidence = resolve(root, client, person, works or [])
            except (OSError, ValueError, TypeError, KeyError) as error:
                record_error('百科人物身份', error)
                evidence = None
            if evidence and evidence.get('verified'):
                failed_photo_urls.update(evidence.get('excluded_photo_urls') or (evidence.get('evidence') or {}).get('excluded_photo_urls') or [])
                verified_names = list(dict.fromkeys(evidence.get('aliases') or []))
                verified_ids = evidence.get('provider_ids') or {}
                base.update(identity_verified=True, verified_aliases=verified_names, identity_evidence=evidence.get('evidence') or {}, provider_ids=verified_ids)
                confirmed = {**current, 'ProviderIds': verified_ids}
                source_people = {p['Id']: p for cast in backups.values() for p in cast if p.get('Id') and p['Id'] != ident and (_same_provider(confirmed, p) or _key(p.get('Name')) in {_key(n) for n in verified_names})}
                if len(source_people) == 1:
                    source_id = next(iter(source_people))
                    try:
                        source_person = _api(client, '/Items/' + source_id)
                    except urllib.error.HTTPError as error:
                        if error.code != 404:
                            raise
                        source_person = {}
                    if _photo(source_person):
                        data = client.image('/Items/' + source_id + '/Images/Primary')
                        path = _cache_image(root / 'data/person-portraits', 'native:' + source_id, data)
                        _upload(client, ident, path)
                        return {**base, 'status': 'applied', 'source': 'verified-alias-same-work-person', 'source_person_id': source_id, 'file': path.name, 'provider_ids': _providers(source_person)}
                    if not verified_ids and _providers(source_person):
                        verified_ids = _providers(source_person)
                        confirmed = {**confirmed, 'ProviderIds': verified_ids}
                        base['provider_ids'] = verified_ids
                if verified_ids or verified_names:
                    try:
                        found, reason = _search_match(client, confirmed, verified_names or names, backups)
                        if found:
                            found['_verified_chinese_source'] = True
                        match = accept(found, '已核对原文名的影视资料源')
                    except (OSError, ValueError, TypeError, KeyError) as error:
                        record_error('已核对原文名的影视资料源', error)
                if match is None and (evidence.get('image_url') or evidence.get('original_file')):
                    match = accept({'Name': next(iter(verified_names), person.get('Name', '')), 'ProviderIds': verified_ids, 'ImageUrl': evidence.get('image_url') or evidence.get('source_url') or 'urn:portrait:' + str(evidence.get('original_sha256', '')), '_portrait_source_file': evidence.get('original_file'), '_portrait_source_sha256': evidence.get('original_sha256'), '_photo_evidence': evidence.get('evidence') or {}, '_verified_chinese_source': True}, '百科人物照片')
                if match:
                    match['_verified_chinese_source'] = True
        if match is None:
            from portrait_sources import resolve_photo
            external_ids = base.get('provider_ids', _providers(current))
            verified_qid = (base.get('identity_evidence') or {}).get('wikidata_id') if base.get('identity_verified') else None
            if external_ids or verified_qid:
                try:
                    for _ in range(2):
                        found = resolve_photo(root, client, external_ids, base.get('verified_aliases') or names, exclude_urls=failed_photo_urls, exclude_filenames=(base.get('identity_evidence') or {}).get('excluded_photo_filenames') or [], verified_qid=verified_qid)
                        match = accept(found, '其他百科与人物资料页')
                        if match or not found:
                            break
                except (OSError, ValueError, TypeError, KeyError) as error:
                    record_error('其他百科与人物资料页', error)
        if match is None and lookup_errors:
            raise lookup_errors[-1]
        if match is None:
            attempts = (prior.get('attempts', 0) if prior.get('version') == VERSION else 0) + 1
            return close_without_photo({**base, 'status': reason or 'unavailable', 'attempts': attempts, 'next_attempt_at': now + min(7 * 86400, 21600 * 2 ** min(attempts - 1, 5)), 'reason': 'identity-ambiguous' if reason == 'ambiguous' else 'photo-not-found-in-searched-sources' if base.get('identity_verified') or base.get('provider_ids') or _providers(current) else 'identity-not-confirmed', 'provider_ids': base.get('provider_ids', _providers(current))})
        if path is None:
            path = _download(root / 'data/person-portraits', match, client)
        if match.get('_verified_chinese_source') and _providers(match):
            latest = _api(client, '/Items/' + urllib.parse.quote(ident, safe=''))
            if latest.get('Type') == 'Person' and _key(latest.get('Name')) == _key(full.get('Name')):
                providers = {**(latest.get('ProviderIds') or {}), **_providers(match)}
                if providers != latest.get('ProviderIds'):
                    _api(client, '/Items/' + urllib.parse.quote(ident, safe=''), {**latest, 'ProviderIds': providers}, method='POST')
        _upload(client, ident, path)
        return {**base, 'status': 'applied', 'source': 'chinese-encyclopedia-photo' if match.get('_photo_evidence', {}).get('kind', '').startswith('baike') else 'verified-external-person-photo' if match.get('_verified_photo_source') else 'chinese-work-person-identity' if match.get('_verified_chinese_source') else 'same-work-cast-search' if match.get('_verified_work_id') else 'verified-person-search', 'verified_work_id': match.get('_verified_work_id'), 'provider_ids': _providers(match) or base.get('provider_ids', {}), 'file': path.name, 'verified_name': match.get('Name')}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, http.client.HTTPException, Image.DecompressionBombError) as error:
        attempts = (prior.get('attempts', 0) if prior.get('version') == VERSION else 0) + 1
        if attempts >= 3 and path is None and base.get('source_errors'):
            return close_without_photo({**base, 'status': 'retry', 'attempts': attempts, 'diagnostic_reason': _error_reason(error)}, 'sources-unavailable-after-limited-retries')
        delay = min(86400, 300 * 2 ** min(attempts - 1, 8))
        return {**base, 'status': 'retry', 'attempts': attempts, 'reason': 'temporary-source-error', 'diagnostic_reason': _error_reason(error), 'http_status': getattr(error, 'http_status', None), 'error_type': type(error).__name__, 'next_attempt_at': now + delay}

def evidence_revisions(root, people):
    """Notice new reviewed evidence without rescanning media or polling providers."""
    root = Path(root)
    confirmed = []
    for filename in ('portrait-identity-confirmed.json', 'portrait-cast-identity-findings.json'):
        confirmed.extend(read(root / 'data' / filename).get('people', []))
    sources = read(root / 'data/person-portrait-sources.json').get('people', [])
    result = {}
    for ident, person in people.items():
        ids = person.get('provider_ids') or person.get('ProviderIds') or {}
        relevant = [r for r in confirmed if r.get('person_id') == ident]
        relevant.extend((r for r in sources if r.get('person_id') == ident or _same_provider({'ProviderIds': ids}, {'ProviderIds': r.get('provider_ids') or {}})))
        if relevant:
            result[ident] = hashlib.sha256(json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result

def _sync_portraits_locked(root, client, items, target_ids=None, max_people=120, max_seconds=180):
    """Enrich missing portraits and persist truthful per-work coverage.

    ``items`` contains Movie/Series/Season DTOs including People. Calls from
    ingestion pass only this batch's work IDs. A one-off user repair can pass
    all existing work DTOs without touching cloud directories or media files.
    """
    root = Path(root)
    folder = root / 'data/person-portraits'
    folder.mkdir(parents=True, exist_ok=True)
    state_path = root / 'data/person-portraits-state.json'
    state = read(state_path)
    people_state = state.setdefault('people', {})
    works_state = state.setdefault('works', {})
    aliases = _aliases(root)
    selected = [i for i in items if i.get('Type') in ('Movie', 'Series', 'Season') and (target_ids is None or i.get('Id') in target_ids)]
    backup_index = _backup_index(root) if selected else {}
    people, work_people, backups, contexts = ({}, {}, {}, {})
    now = time.time()
    for item in selected:
        ids = []
        tmdb = _providers(item).get('Tmdb')
        if tmdb:
            old_items = backup_index.get((item.get('Type'), tmdb), [])
        else:
            old_items = [read(root / 'data/chinese-credits-backups' / item['Id'] / 'item-before.json')]
        old_people = [person for old in old_items if old.get('Type') == item.get('Type') and _same_provider(old, item) for person in old.get('People') or []]
        for person in item.get('People') or []:
            ident = person.get('Id')
            if not ident:
                ident = 'missing-id:' + hashlib.sha256(str(person.get('Name', '')).encode()).hexdigest()[:20]
                people_state[ident] = {'name': person.get('Name'), 'status': 'pending', 'reason': 'person-id-not-created'}
            ids.append(ident)
            if _photo(person):
                people_state[ident] = {**people_state.get(ident, {}), 'name': person.get('Name'), 'status': 'applied', 'checked_at': now, 'version': VERSION}
                people_state[ident].setdefault('source', 'existing-person')
                continue
            if not ident.startswith('missing-id:'):
                people.setdefault(ident, person)
                contexts.setdefault(ident, []).append({k: item[k] for k in ('Id', 'Type', 'Name', 'ProviderIds', 'Path', 'SeriesId', 'ParentIndexNumber', 'IndexNumber') if k in item})
                if old_people:
                    backups.setdefault(ident, {})[item['Id']] = old_people
        work_people[item['Id']] = list(dict.fromkeys(ids))
    revisions = evidence_revisions(root, {ident: {**people_state.get(ident, {}), **person} for ident, person in people.items()})
    pending = []
    for ident, person in people.items():
        prior = people_state.get(ident, {})
        if prior.get('status') == 'applied' and prior.get('version') == VERSION and (now - prior.get('checked_at', 0) < 30):
            continue
        new_context = any((_context_key(w) not in (prior.get('checked_contexts') or []) for w in contexts.get(ident, [])))
        new_evidence = bool(revisions.get(ident) and revisions[ident] != prior.get('evidence_revision'))
        if prior.get('status') == 'no_photo' and (not new_evidence):
            continue
        if prior.get('version') == VERSION and prior.get('next_attempt_at', 0) > now and (not new_context) and (not new_evidence):
            continue
        if new_evidence:
            prior = {**prior, 'failed_photo_urls': [], 'failed_photo_retry_at': 0}
        pending.append((ident, person, prior))
    pending.sort(key=lambda row: -len(contexts.get(row[0], [])))
    attempted = applied = 0
    started = time.monotonic()
    iterator = iter(pending[:max(0, int(max_people))])
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        running = {}

        def enqueue():
            if time.monotonic() - started >= max_seconds:
                return False
            try:
                ident, person, prior = next(iterator)
            except StopIteration:
                return False
            future = pool.submit(_repair, root, client, person, backups.get(ident, {}), aliases, prior, contexts.get(ident, []))
            running[future] = ident
            return True
        for _ in range(3):
            enqueue()
        while running:
            done, _ = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                ident = running.pop(future)
                result = future.result()
                if ident in revisions:
                    result['evidence_revision'] = revisions[ident]
                people_state[ident] = result
                attempted += 1
                applied += result.get('status') == 'applied'
                write(state_path, {**state, 'updated_at': time.time(), 'version': VERSION})
                enqueue()
    for item in selected:
        works_state[item['Id']] = summarize_work(item.get('Name', ''), work_people[item['Id']], people_state)
    write(state_path, {**state, 'updated_at': time.time(), 'version': VERSION})
    selected_states = {i['Id']: works_state[i['Id']] for i in selected}
    return {'works': len(selected), 'attempted': attempted, 'applied': applied, 'missing': sum((v['missing'] for v in selected_states.values())), 'state_path': str(state_path), 'work_states': selected_states}

def sync_portraits(root, client, items, target_ids=None, max_people=120, max_seconds=180):
    """Bounded portrait repair; concurrent callers get a retryable deferral.

    The process lock covers state reads, downloads, uploads and writes. A busy
    result never marks the requested works complete or overwrites newer state;
    the caller can keep its portrait task pending for the next bounded run.
    """
    root = Path(root)
    (root / 'data/person-portraits').mkdir(parents=True, exist_ok=True)
    with _state_lock(root / 'data/person-portraits.lock') as acquired:
        if acquired:
            return _sync_portraits_locked(root, client, items, target_ids, max_people, max_seconds)
        state_path = root / 'data/person-portraits-state.json'
        state = read(state_path)
        selected = [i for i in items if i.get('Type') in ('Movie', 'Series', 'Season') and (target_ids is None or i.get('Id') in target_ids)]
        works = state.get('works', {})
        selected_states = {i['Id']: works[i['Id']] for i in selected if i['Id'] in works}
        return {'works': len(selected), 'attempted': 0, 'applied': 0, 'missing': sum((v.get('missing', 0) for v in selected_states.values())), 'state_path': str(state_path), 'work_states': selected_states, 'deferred': True, 'reason': 'portrait-worker-busy'}
