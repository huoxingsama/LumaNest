"""Actual Baike actor-card portraits and exact linked human-lemma main images.

No search thumbnails, guessed picture URLs, provider photo-pool dependency or
access-control workarounds. Each picture retains its exact work/person proof.
"""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import re
import threading
import time
import urllib.parse
import urllib.request
from personal_cinema.runtime_paths import RuntimePath as Path
from chinese_credits import Document, basic_fields, clean, norm, identity_matches
from library_tasks import read, write
from portrait_identity import _name, _profiles, _same_work
PHOTO_HOSTS = ('bkimg.cdn.bcebos.com',)
_LOCK = threading.RLock()
_URL_LOCKS = {}
_MAX_BYTES = 5 * 1024 * 1024

def _cast_name(value):
    value = clean(value or '')
    value = re.sub('\\s*[<〈（(](?:幼年|童年|少年(?:时代|時代|时期|時期|期)|青年(?:时代|時代|时期|時期|期)|成年|老年|幼少期|幼少時代|儿童时期|兒童時期|(?:日语|日語|日文|英语|英語|国语|國語|普通话|普通話)配音)[>〉）)]\\s*$', '', value)
    return _name(value)

def _page_url(base, value):
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(base, value or ''))
    if parsed.scheme != 'https' or parsed.hostname != 'baike.baidu.com' or (not parsed.path.startswith('/item/')):
        return None
    return urllib.parse.urlunsplit(parsed._replace(path=urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/%:@!$&'()*+,;=-._~"), query='', fragment=''))

def _photo_url(value):
    if str(value).startswith('//'):
        value = 'https:' + value
    parsed = urllib.parse.urlsplit(str(value or ''))
    if parsed.scheme != 'https' or parsed.hostname not in PHOTO_HOSTS or (not parsed.path.startswith('/pic/')):
        return None
    if re.search('default|placeholder|blank|transparent', parsed.path, re.I):
        return None
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if k.lower() != 'x-bce-process']
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query), fragment=''))

def _image(node):
    for field in ('data-src', 'data-original', 'src'):
        url = _photo_url(node.attrs.get(field))
        if url:
            return (url, field)
    return (None, None)

def _same_page(left, right):
    a, b = (urllib.parse.urlsplit(left), urllib.parse.urlsplit(right))
    if a.hostname != 'baike.baidu.com' or b.hostname != 'baike.baidu.com':
        return False
    x, y = (urllib.parse.unquote(a.path).rstrip('/'), urllib.parse.unquote(b.path).rstrip('/'))
    first, second = (re.search('/(\\d+)$', x), re.search('/(\\d+)$', y))
    if first and second:
        return first[1] == second[1]
    return norm(x.split('/item/', 1)[-1].split('/')[0]) == norm(y.split('/item/', 1)[-1].split('/')[0])

def _work_page_matches(content, source, profile):
    doc = Document(content)
    for link in doc.root.find('link'):
        if link.attrs.get('rel') == 'canonical':
            canonical = _page_url(source, link.attrs.get('href'))
            if canonical:
                return _same_page(source, canonical)
    expected = profile.get('source_title')
    if expected and norm(expected) == norm(doc.title()) and (norm(doc.title()) != norm('百度百科')):
        return True
    return identity_matches({'Type': profile.get('type'), 'Name': profile.get('title', ''), 'ProductionYear': profile.get('year'), 'ProviderIds': profile.get('provider_ids') or {}}, doc)

def _cached_work_page(root, profile, source):
    data = (root / 'data').resolve()
    candidates = []
    if profile.get('source_cache_file'):
        path = Path(profile['source_cache_file'])
        if not path.is_absolute():
            path = root / path
        try:
            path.resolve().relative_to(data)
            candidates.append(path)
        except (OSError, ValueError):
            pass
    tmdb = str((profile.get('provider_ids') or {}).get('Tmdb') or '')
    kind = profile.get('type')
    if tmdb.isdecimal() and kind in ('Movie', 'Series', 'Season'):
        candidates.extend([data / 'chinese-source-pages' / (kind + '-' + tmdb + '.html'), data / 'chinese-page-cache' / (tmdb + '.html')])
        config = read(data / 'chinese-credits-config.json')
        if config.get('page_cache_directory'):
            path = Path(config['page_cache_directory']) / (kind + '-' + tmdb + '.html')
            try:
                path.resolve().relative_to(data)
                candidates.append(path)
            except (OSError, ValueError):
                pass
    for path in dict.fromkeys(candidates):
        try:
            if not path.is_file() or path.stat().st_size > _MAX_BYTES:
                continue
            content = path.read_text('utf-8-sig')
            if _work_page_matches(content, source, profile):
                return content
        except (OSError, ValueError):
            continue
    return None

class _Reader:

    def __init__(self, root):
        self.folder = root / 'data/portrait-baike-pages'
        self.http = urllib.request.build_opener()
        self.remaining = 4

    def get(self, value):
        url = _page_url('https://baike.baidu.com', value)
        if not url:
            return None
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.folder / (key + '.json')
        with _LOCK:
            guard = _URL_LOCKS.setdefault(key, threading.Lock())
        with guard:
            cached = read(path)
            if cached.get('url') == url and cached.get('expires_at', 0) > time.time():
                return cached.get('content')
            if self.remaining <= 0:
                return None
            self.remaining -= 1
            try:
                request = urllib.request.Request(url, headers={'User-Agent': 'PersonalMediaLibrary/1.0 (cached public cast portraits)', 'Accept': 'text/html'})
                with self.http.open(request, timeout=6) as response:
                    if not _page_url(url, response.geturl()):
                        return None
                    raw = response.read(_MAX_BYTES + 1)
                    if len(raw) > _MAX_BYTES:
                        return None
                    content = raw.decode('utf-8')
                write(path, {'url': url, 'content': content, 'expires_at': time.time() + 7 * 86400})
                return content
            except (OSError, ValueError):
                try:
                    write(path, {'url': url, 'expires_at': time.time() + 900})
                except OSError:
                    pass
                return None

def _cards(content, source, wanted):
    found = []
    for card in Document(content).root.find(cls='actorItem_'):
        headings = card.find('dt')
        heading = headings[0] if headings else next(iter(card.find(cls='actorName_')), None)
        if heading is None:
            continue
        label = clean(heading.text())
        split = re.split('\\s*饰演\\s*|\\s+饰\\s+', label, maxsplit=1)
        if not split or _cast_name(split[0]) not in wanted:
            continue
        links = []
        for anchor in ([heading] if heading.tag == 'a' else []) + heading.find('a'):
            if _cast_name(anchor.text()) in wanted:
                url = _page_url(source, anchor.attrs.get('href'))
                if url:
                    links.append(url)
        pictures = []
        for node in card.find('a') + card.find('div'):
            if not re.search('coverWrap_|actorImg_', node.attrs.get('class', '')):
                continue
            for image in node.find('img'):
                url, field = _image(image)
                if url:
                    pictures.append((url, field, image.attrs.get(field)))
        found.append({'name': split[0], 'role': split[1] if len(split) == 2 else '', 'person_urls': list(dict.fromkeys(links)), 'pictures': list(dict.fromkeys(pictures))})
    return found

def _person_main_photo(content, names, source, work_title, exclude_urls=()):
    doc = Document(content)
    headings = [clean(node.text()) for node in doc.root.find('h1')]
    heading = next((h for h in headings if _cast_name(h) in names), None)
    if not heading:
        return None
    description = clean(doc.description())
    fields = basic_fields(doc)
    human = fields.get(norm('职业'), '') + ' ' + description + ' ' + doc.title()
    if re.search('虚构人物|虛構人物|虚构角色|虛構角色|动漫角色|動漫角色|漫画角色|漫畫角色|游戏角色|遊戲角色', doc.title() + ' ' + description[:150]):
        return None
    if not re.search('演员|演員|声优|聲優|配音|导演|導演|编剧|編劇|作曲|制片|製片|歌手|艺术家|藝術家|\\bactor\\b|\\bactress\\b|\\bcomposer\\b', human, re.I):
        return None
    pictures = []
    for meta in doc.root.find('meta'):
        if meta.attrs.get('property') == 'og:image' or meta.attrs.get('itemprop') == 'image':
            url = _photo_url(meta.attrs.get('content'))
            if url:
                pictures.append((url, 'lemma-meta-image'))
    for image in doc.root.find('img'):
        node = image
        for _ in range(4):
            if re.search('summaryPic|summary-pic|mainPic|mainPicture|lemmaPicture|mainImage', node.attrs.get('class', ''), re.I):
                url, _ = _image(image)
                if url:
                    pictures.append((url, 'lemma-main-picture'))
                break
            node = getattr(node, 'parent', None)
            if node is None:
                break
    pictures = [picture for picture in pictures if picture[0] not in exclude_urls]
    if not pictures:
        return None
    url, field = pictures[0]
    return (url, {'kind': 'baike-linked-human-main-image', 'person_url': source, 'person_title': heading, 'human_evidence': clean(human)[:400], 'work_mentioned': bool(work_title and norm(work_title) in norm(description)), 'image_field': field})

def resolve(root, client, person, works, exclude_urls=()):
    """Return a same-person Baike photograph match, or None; never upload it."""
    root = Path(root)
    name = person.get('Name', '')
    wanted = _cast_name(name)
    if not wanted:
        return None
    profiles = _profiles(root)
    reader = _Reader(root)
    sources = {}
    excluded = {_photo_url(url) or url for url in exclude_urls}
    for work in works or []:
        if 'People' in work and (not any((p.get('Id') == person.get('Id') and _cast_name(p.get('Name')) == wanted for p in work.get('People') or []))):
            continue
        for key, profile in profiles:
            if not _same_work(root, work, key, profile):
                continue
            rows = [row for row in list(profile.get('actors') or []) + list(profile.get('crew') or []) if _cast_name(row.get('Name')) == wanted]
            source = _page_url('https://baike.baidu.com', profile.get('source_url'))
            if not rows or not source:
                continue
            names = {wanted}
            for row in rows:
                aliases = row.get('Aliases') or []
                if isinstance(aliases, str):
                    aliases = [aliases]
                names.update((_cast_name(a) for a in aliases if isinstance(a, str) and a))
            sources.setdefault(source, []).append((work, profile, names))
    for source, contexts in sources.items():
        content = None
        origin = None
        for work, profile, names in contexts:
            content = _cached_work_page(root, profile, source)
            if content:
                origin = (work, profile)
                break
        if not content:
            content = reader.get(source)
            if content:
                origin = next(((work, profile) for work, profile, _ in contexts if _work_page_matches(content, source, profile)), None)
        if not content or origin is None:
            continue
        work, profile = origin
        names = set().union(*(names for _, _, names in contexts))
        cards = _cards(content, source, names)
        identities = {url for card in cards for url in card['person_urls']}
        if len(identities) > 1:
            continue
        for card in cards:
            for url, field, original in card['pictures']:
                if url in excluded:
                    continue
                return {'Name': name, 'ImageUrl': url, '_verified_chinese_source': True, '_photo_evidence': {'kind': 'baike-exact-work-actor-card', 'work_id': work['Id'], 'source_url': source, 'cast_name': card['name'], 'role': card['role'], 'person_urls': card['person_urls'], 'image_field': field, 'source_image_url': original}}
        for linked in identities:
            person_content = reader.get(linked)
            if not person_content:
                continue
            picture = _person_main_photo(person_content, names, linked, profile.get('title', ''), excluded)
            if picture:
                url, evidence = picture
                return {'Name': name, 'ImageUrl': url, '_verified_chinese_source': True, '_photo_evidence': {**evidence, 'work_id': work['Id'], 'source_url': source, 'cast_name': name}}
    return None
