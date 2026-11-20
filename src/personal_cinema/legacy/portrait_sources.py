"""Find a real portrait through confirmed external person IDs, never name search.

Wikidata's identity links often reach a MAL/AniList staff portrait even when
TMDB has no profile photo. Provider pages are a final fallback, not authority
for mixing an unrelated same-name person's image into the library.
"""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import io
import ipaddress
import json
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from personal_cinema.runtime_paths import RuntimePath as Path
from chinese_credits import Document
from library_tasks import read, write
VERSION = 3
_LOCK = threading.RLock()
_KEY_LOCKS = {}
_STATE_CACHE = {}
_MAX_BYTES = 4 * 1024 * 1024
_PAGE_HOSTS = {'www.wikidata.org', 'www.themoviedb.org', 'www.imdb.com', 'myanimelist.net', 'graphql.anilist.co', 'zh.wikipedia.org', 'ja.wikipedia.org', 'en.wikipedia.org', 'bangumi.tv', 'bgm.tv'}
_PHOTO_HOSTS = ('tmdb.org', 'media-amazon.com', 'wikimedia.org', 'myanimelist.net', 'anilist.co')
_EXACT_PHOTO_HOSTS = {'cdn.hk01.com', 'www.unitedagents.co.uk', 'corocoro-news.jp'}
_PORTRAIT_HINT = re.compile('photo|portrait|headshot|head[-_ ]?image|profile[-_/]|プロフィール写真|顔写真|人物写真|肖像|个人照片|個人照片', re.I)
_NOT_PORTRAIT = re.compile('logo|favicon|placeholder|no[-_ ]?(?:image|photo)|default|banner|signature|illust|cartoon|avatar|自画像|似顔絵|插画|插畫|イラスト', re.I)

class PhotoSourceUnavailable(OSError):
    """A retryable public metadata failure, with no raw response or credentials."""

    def __init__(self, reason='source-temporarily-unavailable', sources=None):
        self.reason = reason
        self.source_results = sources or []
        super().__init__(reason)

def _public_https(url, allowed_hosts=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or (parsed.port not in (None, 443)) or (allowed_hosts is not None and parsed.hostname not in allowed_hosts):
        raise ValueError('Unsupported official portrait URL')
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any((not ipaddress.ip_address(a[4][0]).is_global for a in addresses)):
        raise ValueError('Official portrait URL is not public')
    return parsed

class _PublicRedirect(urllib.request.HTTPRedirectHandler):

    def __init__(self, allowed_hosts=None):
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_https(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def _ids(values):
    result = {}
    for key, pattern in (('Tmdb', '\\d+'), ('Imdb', 'nm\\d+')):
        value = str((values or {}).get(key) or '')
        if re.fullmatch(pattern, value):
            result[key] = value
    return result

def _compatible(left, right):
    return bool(any((right.get(k) == v for k, v in left.items())) and (not any((k in right and v != right[k] for k, v in left.items()))))

def _photo_url(value):
    if not isinstance(value, str):
        return None
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or ''
    if parsed.scheme != 'https' or not (host in _EXACT_PHOTO_HOSTS or any((host == h or host.endswith('.' + h) for h in _PHOTO_HOSTS))):
        return None
    if any((word in parsed.path.lower() for word in ('questionmark', 'placeholder', 'noimage', 'no_image', 'default.jpg'))):
        return None
    return value

def _key_guard(key):
    with _LOCK:
        return _KEY_LOCKS.setdefault(key, threading.Lock())

class _Reader:

    def __init__(self, root):
        self.root = root
        self.folder = root / 'data/person-photo-sources/http'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.opener = urllib.request.build_opener()
        self.remaining = 10
        self.deadline = time.monotonic() + 40
        self.failures = []

    def get(self, url, body=None, official_hosts=None):
        parsed = urllib.parse.urlsplit(url)
        hosts = official_hosts if official_hosts is not None else _PAGE_HOSTS
        if parsed.scheme != 'https' or parsed.hostname not in hosts:
            raise ValueError('Unsupported portrait identity source')
        raw = json.dumps(body, sort_keys=True).encode() if body is not None else None
        key = hashlib.sha256(url.encode() + (raw or b'')).hexdigest()
        path = self.folder / (key + '.json')
        with _key_guard('http:' + key):
            saved = read(path)
            if saved.get('url') == url and saved.get('expires_at', 0) > time.time():
                if saved.get('error'):
                    self.failures.append(saved['error'])
                    raise PhotoSourceUnavailable(saved['error'])
                return saved.get('content', '')
            if body is None:
                shared = read(self.root / 'data/person-identity-cache' / (hashlib.sha256(url.encode()).hexdigest() + '.json'))
                if shared.get('url') == url and shared.get('expires_at', 0) > time.time() and isinstance(shared.get('content'), str):
                    return shared['content']
            if self.remaining <= 0 or time.monotonic() >= self.deadline:
                raise PhotoSourceUnavailable('lookup-budget-reached')
            self.remaining -= 1
            try:
                if official_hosts is not None:
                    _public_https(url, official_hosts)
                opener = urllib.request.build_opener(_PublicRedirect(official_hosts)) if official_hosts is not None else self.opener
                headers = {'User-Agent': 'PersonalMediaLibrary/1.0 (cached public person metadata)', 'Accept': 'application/json,text/html;q=0.9'}
                if raw is not None:
                    headers['Content-Type'] = 'application/json'
                request = urllib.request.Request(url, data=raw, headers=headers)
                with opener.open(request, timeout=max(1, min(9, self.deadline - time.monotonic()))) as response:
                    final = urllib.parse.urlsplit(response.geturl())
                    if final.scheme != 'https' or final.hostname not in hosts:
                        raise ValueError('Portrait identity redirect changed host')
                    data = response.read(_MAX_BYTES + 1)
                    if len(data) > _MAX_BYTES:
                        raise ValueError('Portrait identity response exceeds limit')
                    content = data.decode('utf-8')
                write(path, {'url': url, 'content': content, 'expires_at': time.time() + 7 * 86400})
                return content
            except (OSError, ValueError) as error:
                code = getattr(error, 'code', None)
                reason = 'http-' + str(code) if code else type(error).__name__
                self.failures.append(reason)
                write(path, {'url': url, 'error': reason, 'expires_at': time.time() + 900})
                raise PhotoSourceUnavailable(reason) from None

def _values(entity, prop):
    return [c.get('mainsnak', {}).get('datavalue', {}).get('value') for c in (entity.get('claims') or {}).get(prop, []) if c.get('rank') != 'deprecated' and c.get('mainsnak', {}).get('datavalue')]

def _entity_ids(entity):
    result = {}
    for prop, key, pattern in (('P4985', 'Tmdb', '\\d+'), ('P345', 'Imdb', 'nm\\d+')):
        values = {str(v) for v in _values(entity, prop) if re.fullmatch(pattern, str(v))}
        if len(values) > 1:
            return None
        if values:
            result[key] = values.pop()
    return result

def _known_qids(root, ids):
    path = root / 'data/person-portraits-state.json'
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return []
    with _LOCK:
        saved = _STATE_CACHE.get(str(path))
        if not saved or saved[0] != stamp:
            saved = (stamp, read(path).get('people', {}))
            _STATE_CACHE[str(path)] = saved
        qids = set()
        for value in saved[1].values():
            qid = (value.get('identity_evidence') or {}).get('wikidata_id')
            if value.get('identity_verified') and re.fullmatch('Q\\d+', str(qid)) and _compatible(ids, _ids(value.get('provider_ids'))):
                qids.add(qid)
        return sorted(qids)

def _entity(reader, root, ids, verified_qid=None):
    candidates = [verified_qid] if verified_qid else _known_qids(root, ids)
    if not candidates:
        if not ids:
            return None
        filters = '|'.join((prop + '=' + ids[key] for key, prop in (('Tmdb', 'P4985'), ('Imdb', 'P345')) if key in ids))
        url = 'https://www.wikidata.org/w/api.php?' + urllib.parse.urlencode({'action': 'query', 'format': 'json', 'list': 'search', 'srsearch': 'haswbstatement:' + filters, 'srlimit': 4, 'srprop': ''})
        data = json.loads(reader.get(url))
        if data.get('error'):
            raise PhotoSourceUnavailable('wikidata-query-error')
        query = data.get('query') or {}
        if (query.get('searchinfo') or {}).get('totalhits', 0) > 4:
            return None
        candidates = [row['title'] for row in query.get('search', []) if re.fullmatch('Q\\d+', row.get('title', ''))]
    found = []
    for qid in candidates[:3]:
        url = 'https://www.wikidata.org/wiki/Special:EntityData/' + qid + '.json'
        entity = json.loads(reader.get(url)).get('entities', {}).get(qid, {})
        providers = _entity_ids(entity)
        if entity.get('id') != qid or providers is None:
            continue
        if verified_qid:
            if any((key in providers and providers[key] != value for key, value in ids.items())):
                continue
        elif not providers or not _compatible(ids, providers):
            continue
        if not any((isinstance(v, dict) and v.get('id') == 'Q5' for v in _values(entity, 'P31'))):
            continue
        found.append((entity, providers))
    return found[0] if len(found) == 1 else None

def _meta(content):
    doc = Document(content)
    return ({n.attrs.get('property') or n.attrs.get('name'): n.attrs.get('content') for n in doc.root.find('meta')}, doc)

def _mal(reader, ident):
    url = 'https://myanimelist.net/people/' + ident
    meta, _ = _meta(reader.get(url))
    target = urllib.parse.urlsplit(meta.get('og:url') or '')
    if target.hostname != 'myanimelist.net' or not re.fullmatch('/people/' + ident + '(?:/[^/]*)?/?', target.path):
        raise PhotoSourceUnavailable('mal-profile-not-readable')
    photo = _photo_url(meta.get('og:image'))
    if photo and urllib.parse.urlsplit(photo).hostname == 'cdn.myanimelist.net' and ('/images/voiceactors/' in photo):
        return (photo, meta.get('og:title'), url)
    return None

def _anilist(reader, ident):
    payload = {'query': 'query ($id: Int!) { Staff(id: $id) { id name { full native } image { large } siteUrl } }', 'variables': {'id': int(ident)}}
    result = json.loads(reader.get('https://graphql.anilist.co', payload))
    value = (result.get('data') or {}).get('Staff') or {}
    if value.get('id') != int(ident):
        if result.get('errors'):
            raise PhotoSourceUnavailable('anilist-profile-unavailable')
        return None
    image = _photo_url((value.get('image') or {}).get('large'))
    if image and '/staff/' in image:
        return (image, (value.get('name') or {}).get('full'), 'https://anilist.co/staff/' + ident)
    return None

def _title_key(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value)).replace('_', ' ').split()).casefold()

def _classes(node):
    return set(node.attrs.get('class', '').split())

def _wiki_image(img):
    """Use a real-sized raster image, rejecting common non-portrait assets."""
    url = urllib.parse.urljoin('https://upload.wikimedia.org', img.attrs.get('src', ''))
    parsed = urllib.parse.urlsplit(url)
    if not _photo_url(url) or not (parsed.hostname or '').endswith('.wikimedia.org'):
        return None
    label = urllib.parse.unquote(parsed.path + ' ' + img.attrs.get('alt', '')).lower()
    if any((word in label for word in ('flag', 'signature', 'autograph', 'logo', 'coat_of_arms', 'coat-of-arms', 'emblem', 'icon', '国旗', '國旗', '旗帜', '旗幟', '签名', '簽名', '署名', 'no_portrait', 'no-photo'))):
        return None
    if not re.search('\\.(?:jpe?g|png|webp)(?:/|$)', parsed.path, re.I) or '.svg' in label:
        return None
    if img.attrs.get('data-file-type') == 'drawing':
        return None
    try:
        width = int(re.match('\\d+', img.attrs.get('width', '')).group())
        height = int(re.match('\\d+', img.attrs.get('height', '')).group())
    except (AttributeError, ValueError, TypeError):
        return None
    if width < 90 or height < 100 or (not 0.32 <= width / height <= 1.7):
        return None
    original = re.fullmatch('/wikipedia/(commons|en|ja|zh)/thumb/([a-f0-9]/[a-f0-9]{2}/[^/]+)/[^/]+', parsed.path)
    if original:
        return 'https://upload.wikimedia.org/wikipedia/' + original[1] + '/' + original[2]
    return url

def _wikipedia(reader, site, title, qid):
    """Read the actual linked biography, checking its own Wikidata identity."""
    language = {'zhwiki': 'zh', 'jawiki': 'ja', 'enwiki': 'en'}[site]
    host = language + '.wikipedia.org'
    url = 'https://' + host + '/w/api.php?' + urllib.parse.urlencode({'action': 'parse', 'format': 'json', 'formatversion': 2, 'page': title, 'prop': 'text|properties', 'redirects': 1, 'disableeditsection': 1})
    response = json.loads(reader.get(url))
    if response.get('error'):
        code = response['error'].get('code', 'unknown')
        if code in ('missingtitle', 'invalidtitle'):
            return None
        raise PhotoSourceUnavailable('wikipedia-' + language + '-page-error')
    page = response.get('parse') or {}
    properties = page.get('properties') or {}
    if isinstance(properties, list):
        properties = {p.get('name'): p.get('*', p.get('value')) for p in properties if isinstance(p, dict)}
    if properties.get('wikibase_item') != qid:
        raise PhotoSourceUnavailable('wikipedia-' + language + '-identity-mismatch')
    if _title_key(page.get('title', '')) != _title_key(title):
        raise PhotoSourceUnavailable('wikipedia-' + language + '-title-changed')
    content = page.get('text')
    if isinstance(content, dict):
        content = content.get('*')
    if not isinstance(content, str):
        raise PhotoSourceUnavailable('wikipedia-' + language + '-page-not-readable')
    doc = Document(content)
    boxes = [box for box in doc.root.find('table') if 'infobox' in _classes(box)]
    if not boxes:
        return None
    box = boxes[0]
    for row in box.find('tr')[:8]:
        cells = [n for n in row.children if not isinstance(n, str) and n.tag in ('td', 'th')]
        for cell in cells:
            images = cell.find('img')
            designated = 'infobox-image' in _classes(cell) or any(('photo' in _classes(img) for img in images))
            whole_row = len(cells) == 1 and str(cell.attrs.get('colspan', '')) in ('2', '3')
            if not designated and (not whole_row):
                continue
            candidates = [(img, _wiki_image(img)) for img in images]
            candidates = [(img, link) for img, link in candidates if link]
            if not candidates:
                continue
            caption = cell.text()
            captions = [n.text() for n in box.find() if 'infobox-caption' in _classes(n)]
            caption = ' '.join([caption, *captions[:1]])
            if len(candidates) != 1 or re.search('合影|合照|集合写真|集合寫真|from left|pictured with|group photo', caption, re.I):
                return None
            image = candidates[0][1]
            source = 'https://' + host + '/wiki/' + urllib.parse.quote(page['title'].replace(' ', '_'), safe='')
            return (image, page['title'], source)
        if any(('infobox-label' in _classes(cell) or cell.attrs.get('scope') == 'row' for cell in cells)):
            break
    return None

def _bangumi(reader, ident, results):
    """Record an exact linked person cover without trusting cartoons as photos."""
    url = 'https://bangumi.tv/person/' + ident
    content = reader.get(url)
    doc = Document(content)
    headings = doc.root.find('h1')
    exact_links = [n for h in headings for n in h.find('a') if urllib.parse.urlsplit(urllib.parse.urljoin(url, n.attrs.get('href', ''))).path == '/person/' + ident]
    if not exact_links:
        raise PhotoSourceUnavailable('bangumi-person-page-not-readable')
    columns = [n for n in doc.root.find() if n.attrs.get('id') == 'columnSubjectHomeA']
    for column in columns:
        for anchor in column.find('a'):
            if 'cover' not in _classes(anchor):
                continue
            for img in anchor.find('img'):
                photo = urllib.parse.urljoin(url, img.attrs.get('src', ''))
                parsed = urllib.parse.urlsplit(photo)
                if parsed.scheme == 'https' and parsed.hostname == 'lain.bgm.tv' and re.fullmatch('/pic/crt/[lms]/[a-f0-9]{2}/[a-f0-9]{2}/[^/]+\\.(?:jpe?g|png|webp)', parsed.path, re.I):
                    results.append({'source': 'bangumi:P6298:' + ident, 'outcome': 'photo-review-required', 'source_url': url, 'image_url': photo, 'name': exact_links[0].text()})
                    return None
    return None

def _provider_page(reader, kind, ident):
    url = 'https://www.imdb.com/name/' + ident + '/' if kind == 'Imdb' else 'https://www.themoviedb.org/person/' + ident + '?language=en-US'
    content = reader.get(url)
    _, doc = _meta(content)
    canonical = next((n.attrs.get('href') for n in doc.root.find('link') if n.attrs.get('rel') == 'canonical'), None)
    pattern = 'https://(?:www\\.)?imdb\\.com/name/' + ident + '/?' if kind == 'Imdb' else 'https://www\\.themoviedb\\.org/person/' + ident + '(?:-[^/?#]*)?/?'

    def exact(value):
        return bool(isinstance(value, str) and re.fullmatch(pattern, value.split('?')[0]))
    if canonical and (not exact(canonical)):
        raise ValueError('Provider person canonical identity mismatch')
    saw_person = False
    for raw in re.findall('<script\\b[^>]*type=[\\\'"]application/ld\\+json[\\\'"][^>]*>(.*?)</script>', content, re.I | re.S):
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        records = value if isinstance(value, list) else [value]
        for record in list(records):
            if isinstance(record, dict) and isinstance(record.get('@graph'), list):
                records.extend(record['@graph'])
        for record in records:
            if not isinstance(record, dict) or record.get('@type') != 'Person' or (not exact(record.get('url') or record.get('@id') or canonical)):
                continue
            saw_person = True
            image = record.get('image')
            if isinstance(image, dict):
                image = image.get('url') or image.get('contentUrl')
            image = _photo_url(image)
            if image:
                return (image, record.get('name'), url)
    if not saw_person:
        raise PhotoSourceUnavailable('provider-profile-not-readable')
    return None

def _official_original(reader, url):
    """Keep an explicitly identified public portrait for the existing uploader."""
    if reader.remaining <= 0 or time.monotonic() >= reader.deadline:
        raise PhotoSourceUnavailable('lookup-budget-reached')
    reader.remaining -= 1
    _public_https(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'PersonalMediaLibrary/1.0'})
    opener = urllib.request.build_opener(_PublicRedirect())
    with opener.open(request, timeout=max(1, min(9, reader.deadline - time.monotonic()))) as response:
        _public_https(response.geturl())
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('Official portrait exceeds limit')
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if image.format not in ('JPEG', 'PNG', 'WEBP') or getattr(image, 'n_frames', 1) != 1 or width < 160 or (height < 180) or (width * height > 32000000) or (not 0.4 <= width / height <= 1.6):
            raise ValueError('Official portrait dimensions unsuitable')
        image.load()
    checksum = hashlib.sha256(data).hexdigest()
    relative = 'portrait-source-originals/' + checksum + '.jpg'
    path = reader.root / 'data' / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        write_path = path.with_suffix('.download')
        write_path.write_bytes(data)
        write_path.replace(path)
    return {'_portrait_source_file': relative, '_portrait_source_sha256': checksum, 'original_size': [width, height]}

def _official_page(reader, website, entity, excluded, results, evidence, originals):
    """Use only the confirmed entity's own P856 page and explicit portrait field.

    No site-wide crawl, image search, social feed or related-person gallery is
    used. An official page that offers a drawing or logo remains without a photo.
    """
    parsed = urllib.parse.urlsplit(website)
    host = parsed.hostname or ''
    if not host or parsed.username or parsed.password or (parsed.port not in (None, 443)):
        return None
    url = urllib.parse.urlunsplit(('https', parsed.netloc, parsed.path or '/', parsed.query, ''))
    hosts = {host, host[4:] if host.startswith('www.') else 'www.' + host}
    content = reader.get(url, official_hosts=hosts)
    meta, doc = _meta(content)
    known = [row.get('value', '') for row in (entity.get('labels') or {}).values()]
    known += [row.get('value', '') for rows in (entity.get('aliases') or {}).values() for row in rows]

    def name_key(value):
        return ''.join((c for c in _title_key(value) if c.isalnum()))
    known = {name_key(name) for name in known if len(name_key(name)) >= 3}

    def exact_name(value):
        return name_key(value) in known
    titles = [n.text() for tag in ('h1', 'title') for n in doc.root.find(tag)]
    titles += [meta.get('og:title', '')]
    named_page = any((any((name in name_key(title) for name in known)) for title in titles))
    canonical = next((n.attrs.get('href') for n in doc.root.find('link') if n.attrs.get('rel') == 'canonical'), None)
    if canonical:
        canon = urllib.parse.urlsplit(urllib.parse.urljoin(url, canonical))
        if canon.scheme != 'https' or canon.hostname not in hosts:
            raise PhotoSourceUnavailable('official-page-canonical-mismatch')
        if parsed.path.strip('/') and (not canon.path.strip('/')):
            raise PhotoSourceUnavailable('official-profile-path-lost')
    candidates = []

    def add(value, caption, field, person_confirmed=False):
        if not isinstance(value, str):
            return
        image = urllib.parse.urljoin(url, value)
        target = urllib.parse.urlsplit(image)
        hints = urllib.parse.unquote(target.path) + ' ' + caption
        if target.scheme != 'https' or target.username or target.password or _NOT_PORTRAIT.search(hints) or (not _PORTRAIT_HINT.search(hints)) or (not (person_confirmed or named_page)) or (image in excluded):
            return
        if not any((old[0] == image for old in candidates)):
            candidates.append((image, caption[:500], field))
    for raw in re.findall('<script\\b[^>]*type=[\\\'"]application/ld\\+json[\\\'"][^>]*>(.*?)</script>', content, re.I | re.S):
        try:
            values = json.loads(raw)
        except ValueError:
            continue
        values = values if isinstance(values, list) else [values]
        for record in list(values):
            if isinstance(record, dict) and isinstance(record.get('@graph'), list):
                values.extend(record['@graph'])
        for record in values:
            if not isinstance(record, dict) or record.get('@type') != 'Person' or (not exact_name(record.get('name', ''))):
                continue
            image = record.get('image')
            if isinstance(image, dict):
                add(image.get('contentUrl') or image.get('url'), str(image.get('caption') or image.get('name') or ''), 'Person.image', True)
            elif isinstance(image, str):
                add(image, record.get('name', ''), 'Person.image', True)
    if named_page:
        for img in doc.root.find('img'):
            alt = img.attrs.get('alt', '')
            marker = ' '.join((str(img.attrs.get(k, '')) for k in ('class', 'id', 'title')))
            marker += ' ' + str(getattr(img, 'parent', img).attrs.get('class', ''))
            if not any((name in name_key(alt) for name in known)):
                continue
            add(img.attrs.get('src') or img.attrs.get('data-src'), alt + ' ' + marker, 'named-profile-photo')
        add(meta.get('og:image'), meta.get('og:image:alt') or meta.get('og:title', ''), 'named-profile-og-image')
    errors = []
    for image, caption, field in candidates[:2]:
        try:
            original = _official_original(reader, image)
        except (OSError, ValueError) as error:
            code = getattr(error, 'code', None)
            reason = 'http-' + str(code) if code else getattr(error, 'reason', type(error).__name__)
            errors.append(str(reason)[:160])
            results.append({'source': 'wikidata:P856:official-photo', 'source_url': url, 'image_url': image, 'outcome': 'unavailable', 'reason': errors[-1]})
            continue
        originals.update({key: value for key, value in original.items() if key.startswith('_')})
        evidence.update(linked_property='P856', official_website=website, image_field=field, caption=caption, original_size=original['original_size'], official_identity='same-confirmed-wikidata-entity-and-page-name')
        return (image, next((n for n in titles if n), ''), url)
    if errors:
        raise PhotoSourceUnavailable(errors[-1])
    return None

def _registered_photo(root, ids, exclude_urls=(), person=None, works=None):
    """Reuse reviewed source evidence independently of expiring lookup caches."""
    registry = read(root / 'data/person-portrait-sources.json')
    matches = []
    for entry in registry.get('people', []) if registry.get('version') == 1 else []:
        known = _ids(entry.get('provider_ids'))
        exact_work_person = bool(person and entry.get('person_id') == person.get('Id') and (entry.get('name') == person.get('Name')) and set(entry.get('work_ids') or []).intersection((w.get('Id') for w in works or [])))
        if not entry.get('verified_at') or not (_compatible(ids, known) or exact_work_person):
            continue
        url = entry.get('image_url')
        if url in exclude_urls and (not entry.get('original_file')):
            continue
        parsed = urllib.parse.urlsplit(url or '')
        source = urllib.parse.urlsplit(entry.get('source_url') or '')
        local_original = bool(re.fullmatch('portrait-source-originals/[a-f0-9]{64}\\.jpg', entry.get('original_file', '')) and re.fullmatch('[a-f0-9]{64}', entry.get('original_sha256', '')))
        if not (_photo_url(url) or (parsed.scheme == 'https' and parsed.hostname == 'cdn.hk01.com') or (local_original and parsed.scheme == 'https' and parsed.hostname)):
            continue
        if source.scheme != 'https' or not source.hostname or (not entry.get('caption')):
            continue
        evidence = {**(entry.get('identity_evidence') or {}), 'provider_ids': known, 'source_url': entry['source_url'], 'caption': entry['caption'], 'verified_at': entry['verified_at'], 'registry': 'person-portrait-sources.json'}
        match = {'Name': entry.get('name', ''), 'ProviderIds': {**known, **ids}, 'ImageUrl': url, '_photo_evidence': evidence, '_verified_photo_source': True}
        if entry.get('entity_type') == 'group':
            match['_portrait_group'] = True
        crop, size = (entry.get('crop'), entry.get('original_size'))
        if crop is not None:
            if not (isinstance(crop, list) and len(crop) == 4 and isinstance(size, list) and (len(size) == 2) and all((isinstance(v, int) and (not isinstance(v, bool)) for v in crop + size)) and (0 <= crop[0] < crop[2] <= size[0]) and (0 <= crop[1] < crop[3] <= size[1])):
                continue
            match['_portrait_crop'] = crop
            match['_portrait_original_size'] = size
            evidence.update(crop=crop, original_size=size)
        original = entry.get('original_file', '')
        checksum = entry.get('original_sha256', '')
        if re.fullmatch('portrait-source-originals/[a-f0-9]{64}\\.jpg', original) and re.fullmatch('[a-f0-9]{64}', checksum):
            match['_portrait_source_file'] = original
            match['_portrait_source_sha256'] = checksum
        matches.append(match)
    unique = {json.dumps([m['ImageUrl'], m.get('_portrait_crop')], sort_keys=True): m for m in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None

def resolve_photo(root, client, provider_ids, names=(), exclude_urls=(), exclude_filenames=(), verified_qid=None):
    """Return a provider-shaped photo match or None for no currently found photo.

    Input IDs must belong to the confirmed person. A verified_qid may be passed
    only from previously verified identity evidence, never from a name search.
    Public source failures are
    briefly cached and raise PhotoSourceUnavailable if nothing else succeeds;
    None is never a claim that this person has no photo anywhere.
    Excluded failed image URLs select other sources without replacing the
    ordinary lookup cache with a transient download failure.
    """
    root = Path(root)
    ids = _ids(provider_ids)
    verified_qid = str(verified_qid or '')
    verified_qid = verified_qid if re.fullmatch('Q[1-9]\\d*', verified_qid) else None
    if not ids and (not verified_qid):
        return None
    excluded = {url for url in exclude_urls if isinstance(url, str)}
    reviewed = _registered_photo(root, ids, excluded)
    if reviewed:
        return reviewed
    rejected_files = {str(value).casefold().replace('_', ' ') for value in exclude_filenames}
    cache_identity = {'provider_ids': ids, 'exclude_urls': sorted(excluded), 'exclude_filenames': sorted(rejected_files)} if excluded or rejected_files else ids
    if verified_qid:
        cache_identity = {'provider_ids': ids, 'verified_qid': verified_qid, 'exclude_urls': sorted(excluded), 'exclude_filenames': sorted(rejected_files)}
    key = hashlib.sha256(json.dumps(cache_identity, sort_keys=True).encode()).hexdigest()
    path = root / 'data/person-photo-sources' / (key + '.json')
    with _key_guard('person:' + key):
        saved = read(path)
        if saved.get('version') == VERSION and saved.get('expires_at', 0) > time.time():
            if saved.get('error'):
                raise PhotoSourceUnavailable(saved['error'], saved.get('source_results'))
            return saved.get('match')
        reader = _Reader(root)
        errors = []
        source_results = []

        def attempt(source, call):
            before = len(source_results)
            try:
                result = call()
                if isinstance(result, tuple) and len(result) == 3 and (result[0] in excluded):
                    source_results.append({'source': source, 'outcome': 'excluded-failed-image', 'image_url': result[0]})
                    return None
                if len(source_results) == before:
                    source_results.append({'source': source, 'outcome': 'matched' if result else 'no-usable-photo'})
                return result
            except (OSError, ValueError, TypeError, KeyError) as error:
                reason = str(getattr(error, 'reason', type(error).__name__))[:160]
                errors.append(reason)
                source_results.append({'source': source, 'outcome': 'unavailable', 'reason': reason})
                return None
        linked = attempt('wikidata:person-identity', lambda: _entity(reader, root, ids, verified_qid))
        evidence = {'provider_ids': ids}
        photo = None
        originals = {}
        matched_ids = ids
        if linked:
            entity, linked_ids = linked
            matched_ids = {**ids, **linked_ids}
            evidence.update(wikidata_id=entity['id'], identity_source_url='https://www.wikidata.org/wiki/' + entity['id'])
            labels = entity.get('labels') or {}
            label = next((labels.get(language, {}).get('value') for language in ('en', 'ja', 'zh') if labels.get(language, {}).get('value')), next(iter(names), ''))
            images = [v for v in _values(entity, 'P18') if isinstance(v, str) and v.casefold().replace('_', ' ') not in rejected_files]
            image_urls = ['https://commons.wikimedia.org/wiki/Special:FilePath/' + urllib.parse.quote(value, safe='') + '?width=1000' for value in images]
            usable_urls = [url for url in image_urls if url not in excluded]
            if usable_urls:
                photo = (usable_urls[0], label, evidence['identity_source_url'])
                source_results.append({'source': 'wikidata:P18', 'outcome': 'matched'})
            else:
                source_results.append({'source': 'wikidata:P18', 'outcome': 'excluded-failed-image' if image_urls else 'no-image-property'})
            websites = [u for u in _values(entity, 'P856') if isinstance(u, str) and urllib.parse.urlsplit(u).scheme in ('http', 'https') and urllib.parse.urlsplit(u).hostname]
            if websites:
                evidence['official_websites'] = websites[:3]
            for site in ('zhwiki', 'jawiki', 'enwiki'):
                if photo:
                    break
                entry = (entity.get('sitelinks') or {}).get(site) or {}
                title = entry.get('title')
                if entry.get('site') != site or not isinstance(title, str) or (not title):
                    continue
                photo = attempt(site + ':biography-infobox', lambda: _wikipedia(reader, site, title, entity['id']))
                if photo:
                    evidence.update(linked_site=site, linked_title=title, image_field='biography-infobox-primary-image')
            for website in websites[:2]:
                if photo:
                    break
                photo = attempt('wikidata:P856:official-profile', lambda: _official_page(reader, website, entity, excluded, source_results, evidence, originals))
            bangumi_ids = {str(v) for v in _values(entity, 'P6298') if re.fullmatch('\\d+', str(v))}
            if not photo and len(bangumi_ids) == 1:
                bangumi_id = bangumi_ids.pop()
                attempt('bangumi:P6298:' + bangumi_id, lambda: _bangumi(reader, bangumi_id, source_results))
            for prop, resolver in (('P4084', _mal), ('P11227', _anilist)):
                if photo:
                    break
                values = {str(v) for v in _values(entity, prop) if re.fullmatch('\\d+', str(v))}
                if len(values) == 1:
                    ident = values.pop()
                    photo = attempt('wikidata:' + prop + ':' + ident, lambda: resolver(reader, ident))
                    if photo:
                        evidence['linked_property'] = prop
                        evidence['linked_id'] = ident
        for kind in ('Tmdb', 'Imdb'):
            if photo:
                break
            if kind in ids:
                photo = attempt(kind + ':person-page', lambda: _provider_page(reader, kind, ids[kind]))
        match = None
        if photo:
            url, name, source = photo
            match = {'Name': name or next(iter(names), ''), 'ProviderIds': matched_ids, 'ImageUrl': url, '_photo_evidence': {**evidence, 'source_url': source, 'source_results': source_results}, '_verified_photo_source': True}
            match.update(originals)
        reason = errors[-1] if errors and (not match) else None
        write(path, {'version': VERSION, 'provider_ids': ids, 'match': match, 'source_results': source_results, 'identity_evidence': evidence, 'error': reason, 'expires_at': time.time() + (7 * 86400 if match else 900 if reason else 6 * 3600)})
        if reason:
            raise PhotoSourceUnavailable(reason, source_results)
        return match
