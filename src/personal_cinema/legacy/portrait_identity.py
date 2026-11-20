"""Resolve a cast member through that work's verified Chinese source.

No person-name search or billing-position guesses: a full-name cast link must
lead from an already verified work profile to a human Wikidata entity.
"""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from personal_cinema.runtime_paths import RuntimePath as Path
from chinese_credits import Document, clean
from library_tasks import read, write
_LOCK = threading.RLock()
_PROFILES = {}
_URL_LOCKS = {}
_MAX_BYTES = 5 * 1024 * 1024
_WIKI = re.compile('^(?:zh|en|ja)\\.wikipedia\\.org$')

class IdentityLookupError(OSError):
    """Safe diagnostic fields; never carry request credentials or HTML."""

    def __init__(self, code, host='', http_status=None):
        self.code = code
        self.host = host
        self.http_status = http_status
        super().__init__('identity-source:' + code + (':' + str(http_status) if http_status else ''))

def _url(url):
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parsed._replace(path=urllib.parse.quote(parsed.path, safe="/%:@!$&'()*+,;=-._~"), query=urllib.parse.urlencode(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)), fragment=''))

def _name(value):
    value = str(value or '').translate(str.maketrans({'塚': '冢', '恵': '惠'}))
    value = unicodedata.normalize('NFKD', clean(unicodedata.normalize('NFKC', value)))
    return ''.join((c for c in value.casefold() if c.isalnum()))

def _profiles(root):
    result = []
    with _LOCK:
        for filename in ('chinese-credits-profiles.json', 'chinese-season-profiles.json'):
            path = root / 'data' / filename
            try:
                stamp = path.stat().st_mtime_ns
            except OSError:
                continue
            old = _PROFILES.get(str(path))
            if old is None or old[0] != stamp:
                old = (stamp, read(path))
                _PROFILES[str(path)] = old
            result.extend(((key, value) for key, value in old[1].items() if isinstance(value, dict)))
    return result

def _same_work(root, work, key, profile):
    if work.get('Type') != profile.get('type'):
        return False
    if work.get('Type') == 'Season':
        try:
            relative = Path(work['Path']).absolute().relative_to((root / 'catalog').absolute()).as_posix()
        except (KeyError, ValueError):
            return False
        return relative.casefold() == str(profile.get('catalog_relative') or key).replace('\\', '/').casefold()
    left = work.get('ProviderIds') or {}
    right = profile.get('provider_ids') or {}
    return bool(left.get('Tmdb') and str(left['Tmdb']) == str(right.get('Tmdb')) and (not any((left.get(k) and right.get(k) and (str(left[k]) != str(right[k])) for k in ('Imdb', 'Tvdb')))))

class _Reader:

    def __init__(self, root, client):
        self.folder = root / 'data/person-identity-cache'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.http = urllib.request.build_opener()
        self.remaining = 6

    def get(self, url):
        url = _url(url)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or not (_WIKI.fullmatch(parsed.hostname or '') or parsed.hostname == 'www.wikidata.org'):
            raise ValueError('Untrusted identity source')
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.folder / (key + '.json')
        with _LOCK:
            guard = _URL_LOCKS.setdefault(key, threading.Lock())
        with guard:
            saved = read(path)
            if saved.get('url') == url and saved.get('expires_at', 0) > time.time():
                if not saved.get('failed'):
                    return saved['content']
                if saved.get('error_code'):
                    raise IdentityLookupError(saved['error_code'], parsed.hostname, saved.get('http_status'))
            if self.remaining <= 0:
                raise IdentityLookupError('request-budget', parsed.hostname)
            self.remaining -= 1
            try:
                request = urllib.request.Request(url, headers={'User-Agent': 'PersonalMediaLibrary/1.0 (personal cast metadata; cached requests)', 'Accept': 'application/json,text/html;q=0.9'})
                with self.http.open(request, timeout=9) as response:
                    final = urllib.parse.urlsplit(response.geturl())
                    if final.scheme != 'https' or not (_WIKI.fullmatch(final.hostname or '') or final.hostname == 'www.wikidata.org'):
                        raise ValueError('Identity redirect host changed')
                    data = response.read(_MAX_BYTES + 1)
                    if len(data) > _MAX_BYTES:
                        raise ValueError('Identity page exceeds limit')
                    content = data.decode('utf-8')
                write(path, {'url': url, 'content': content, 'expires_at': time.time() + 7 * 86400})
                return content
            except (OSError, ValueError) as error:
                status = getattr(error, 'code', None) if isinstance(error, urllib.error.HTTPError) else None
                code = 'not-found' if status in (404, 410) else 'rate-limited' if status == 429 else 'access-denied' if status in (401, 403) else 'upstream-http' if status else 'invalid-response' if isinstance(error, ValueError) else 'transport'
                delay = 86400 if code == 'not-found' else 900
                write(path, {'url': url, 'failed': True, 'error_code': code, 'http_status': status, 'expires_at': time.time() + delay})
                raise IdentityLookupError(code, parsed.hostname, status) from None

def _wiki_link(base, href):
    url = urllib.parse.urljoin(base, href)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not _WIKI.fullmatch(parsed.hostname or ''):
        return None
    if not parsed.path.startswith('/wiki/'):
        return None
    title = urllib.parse.unquote(parsed.path[6:])
    if not title or ':' in title:
        return None
    return urllib.parse.urlunsplit(parsed._replace(query='', fragment=''))

def _cast_links(content, source, name):
    wanted = _name(name)
    doc = Document(content)
    links = []
    if not wanted:
        return links
    for anchor in doc.root.find('a'):
        if _name(anchor.text()) != wanted:
            continue
        direct = _wiki_link(source, anchor.attrs.get('href', ''))
        if direct:
            links.append(direct)
        parent = getattr(anchor, 'parent', None)
        for _ in range(3):
            if parent is None or parent.tag not in ('span', 'small'):
                break
            if any((marker in parent.attrs.get('class', '') for marker in ('ilh-all', 'interlanguage'))):
                for linked in parent.find('a'):
                    target = _wiki_link(source, linked.attrs.get('href', ''))
                    if target:
                        links.append(target)
                break
            parent = getattr(parent, 'parent', None)
    for original in _cast_aliases(content, name):
        for cell in doc.root.find('td'):
            if original not in cell.text() or _name(name) not in _name(cell.text()):
                continue
            for anchor in cell.find('a'):
                if _name(anchor.text()) != _name(original):
                    continue
                target = _wiki_link(source, anchor.attrs.get('href', ''))
                if target:
                    links.append(target)
    return list(dict.fromkeys(links))

def _cast_aliases(content, name):
    """Exact work-source spellings such as 班·霍尔（Ben Hall）.

    This records a documented name equivalence, not a provider/person match.
    The caller must still resolve homonyms against its same-work cast.
    """
    wanted = _name(name)
    aliases = set()
    doc = Document(content)
    for node in doc.root.find():
        if node.tag not in ('li', 'td', 'span'):
            continue
        text = clean(node.text()).strip()
        if len(text) > 600:
            continue
        match = re.match("^([^（(]{1,70})[（(]\\s*(?:英语[:：]\\s*)?([A-Za-zÀ-ž][A-Za-zÀ-ž .\\'’\\-]{1,90})[）)]", text)
        if match and _name(match[1]) == wanted:
            aliases.add(match[2].strip())
        if node.tag == 'td':
            bilingual = re.fullmatch("([^、]{1,70})、\\s*([A-Za-zÀ-ž][A-Za-zÀ-ž .\\'’\\-]{1,90})(?:[（(](?:英语|英語)[:：]\\s*\\2[）)])?", text)
            if bilingual and _name(bilingual[1]) == wanted:
                aliases.add(bilingual[2].strip())
    return sorted(aliases)

def _values(entity, property_id):
    values = []
    for claim in (entity.get('claims') or {}).get(property_id, []):
        if claim.get('rank') == 'deprecated':
            continue
        value = claim.get('mainsnak', {}).get('datavalue', {}).get('value')
        if value is not None:
            values.append(value)
    return values

def _linked_work_wiki(root, reader, profile):
    """Reach a Chinese work page from an exact stored provider identity.

    A Baike cast profile remains authoritative. This is a second identity
    source, linked from the actual movie/series, not a person-name search.
    """
    kind = profile.get('type')
    providers = profile.get('provider_ids') or {}
    if kind == 'Season':
        parent = profile.get('parent_key', '').split(':', 1)
        if len(parent) != 2 or parent[0] != 'Series':
            return None
        kind = 'Series'
        ident = parent[1]
    else:
        ident = str(providers.get('Tmdb') or '')
    if kind not in ('Movie', 'Series') or not ident.isdecimal():
        return None
    page = root / 'data/artwork-source-pages' / (('movie-' if kind == 'Movie' else 'tv-') + ident + '.html')
    if not page.is_file():
        return None
    from chinese_identity import provider_links
    links = provider_links(page.read_text('utf-8'), kind, ident)
    if providers.get('Imdb') and links.get('imdb') and (providers['Imdb'] != links['imdb']):
        return None
    qid = links.get('wikidata', '')
    if not re.fullmatch('Q\\d+', qid):
        return None
    entity = json.loads(reader.get('https://www.wikidata.org/wiki/Special:EntityData/' + qid + '.json')).get('entities', {}).get(qid, {})
    if entity.get('id') != qid:
        return None
    prop = 'P4947' if kind == 'Movie' else 'P4983'
    actual = {str(v) for v in _values(entity, prop)}
    if actual and ident not in actual:
        return None
    imdb = {str(v) for v in _values(entity, 'P345')}
    if links.get('imdb') and imdb and (links['imdb'] not in imdb):
        return None
    title = (entity.get('sitelinks') or {}).get('zhwiki', {}).get('title')
    if not title:
        return None
    return 'https://zh.wikipedia.org/wiki/' + urllib.parse.quote(title, safe='')

def _person(reader, url):
    parsed = urllib.parse.urlsplit(url)
    title = urllib.parse.unquote(parsed.path[6:])
    api = 'https://' + parsed.hostname + '/w/api.php?' + urllib.parse.urlencode({'action': 'query', 'format': 'json', 'formatversion': 2, 'redirects': 1, 'prop': 'pageprops', 'ppprop': 'wikibase_item|disambiguation', 'titles': title})
    try:
        payload = json.loads(reader.get(api))
        pages = payload.get('query', {}).get('pages', [])
        if len(pages) != 1 or pages[0].get('missing'):
            return None
        props = pages[0].get('pageprops') or {}
        if 'disambiguation' in props:
            return None
        qid = props.get('wikibase_item', '')
    except IdentityLookupError as error:
        if error.code in ('not-found', 'rate-limited', 'request-budget'):
            raise
        html = reader.get(url)
        qids = set(re.findall('"wgWikibaseItemId"\\s*:\\s*"(Q\\d+)"', html))
        qid = next(iter(qids)) if len(qids) == 1 else ''
    if not re.fullmatch('Q\\d+', qid):
        return None
    entity_url = 'https://www.wikidata.org/wiki/Special:EntityData/' + qid + '.json'
    entity = json.loads(reader.get(entity_url)).get('entities', {}).get(qid, {})
    if entity.get('id') != qid or not any((isinstance(v, dict) and v.get('id') == 'Q5' for v in _values(entity, 'P31'))):
        return None
    providers = {}
    for prop, key, pattern in [('P4985', 'Tmdb', '\\d+'), ('P345', 'Imdb', 'nm\\d+')]:
        values = {str(v) for v in _values(entity, prop) if re.fullmatch(pattern, str(v))}
        if len(values) > 1:
            return None
        if values:
            providers[key] = values.pop()
    aliases = []
    for language in ('en', 'ja', 'zh', 'zh-cn', 'zh-hans', 'zh-tw', 'zh-hant'):
        label = (entity.get('labels') or {}).get(language, {}).get('value')
        if label:
            aliases.append(label)
        aliases.extend((a['value'] for a in (entity.get('aliases') or {}).get(language, []) if a.get('value')))
    for site in ('enwiki', 'jawiki', 'zhwiki'):
        label = (entity.get('sitelinks') or {}).get(site, {}).get('title')
        if label and '(' not in label and ('（' not in label):
            aliases.append(label)
    aliases = list(dict.fromkeys(aliases))[:20]
    pictures = [v for v in _values(entity, 'P18') if isinstance(v, str)]
    image_url = 'https://commons.wikimedia.org/wiki/Special:FilePath/' + urllib.parse.quote(pictures[0], safe='') + '?width=800' if pictures else None
    return {'name': aliases[0] if aliases else title, 'aliases': aliases, 'provider_ids': providers, 'image_url': image_url, 'wikidata_id': qid, 'person_source_url': url, 'identity_source_url': entity_url}

def resolve(root, client, person, works):
    """Return a verified identity and optional photo URL, or None.

    ``works`` must be current Movie/Series/Season contexts collected by the
    caller while walking this exact PersonId in each work's People list.
    They may omit People; verified profiles still must contain the full name.
    The caller retains Chinese billing and role information.
    Transient HTTP failures raise OSError so callers schedule a short retry;
    a source without an unambiguous person link returns None.
    """
    root = Path(root)
    name = person.get('Name', '')
    wanted = _name(name)
    current_work_ids = {str(work.get('Id')) for work in works if work.get('Id')}
    confirmed_records = []
    for filename in ('portrait-identity-confirmed.json', 'portrait-cast-identity-findings.json'):
        confirmed = read(root / 'data' / filename)
        if isinstance(confirmed, dict) and isinstance(confirmed.get('people'), list):
            confirmed_records.extend(confirmed['people'])
    matches = []
    for record in confirmed_records:
        if not isinstance(record, dict) or record.get('person_id') != person.get('Id'):
            continue
        if _name(record.get('name')) != wanted or not record.get('verified_at'):
            continue
        if not current_work_ids.intersection((str(value) for value in record.get('work_ids', []))):
            continue
        evidence = dict(record.get('evidence') or {})
        if not evidence or not record.get('source_url'):
            continue
        for original_key in ('original_file', 'original_sha256'):
            if record.get(original_key):
                evidence[original_key] = record[original_key]
        aliases = [a for a in record.get('verified_aliases', []) if isinstance(a, str) and a.strip()]
        providers = record.get('provider_ids') or {}
        providers = {key: str(value) for key, value in providers.items() if key == 'Tmdb' and str(value).isdigit() or (key == 'Imdb' and re.fullmatch('nm\\d+', str(value)))}
        if aliases or providers or record.get('image_url') or record.get('original_file'):
            matches.append({'name': aliases[0] if aliases else name, 'aliases': aliases, 'provider_ids': providers, 'image_url': record.get('image_url'), 'verified': True, 'evidence': evidence, 'original_file': record.get('original_file') or evidence.get('original_file'), 'original_sha256': record.get('original_sha256') or evidence.get('original_sha256'), 'source_url': record['source_url']})
    if matches:
        matches.sort(key=lambda row: (bool(row['original_file'] and row['original_sha256']), bool(row['image_url'])), reverse=True)
        result = matches[0]
        evidence = result['evidence']
        for exclusion in ('excluded_photo_urls', 'excluded_photo_filenames'):
            values = []
            for row in matches:
                entries = row['evidence'].get(exclusion) or []
                if isinstance(entries, str):
                    entries = [entries]
                values.extend((value for value in entries if isinstance(value, str) and value))
            if values:
                evidence[exclusion] = list(dict.fromkeys(values))
        evidence['verified_identity_sources'] = []
        for row in matches:
            if any((key in result['provider_ids'] and result['provider_ids'][key] != value for key, value in row['provider_ids'].items())):
                continue
            result['provider_ids'].update(row['provider_ids'])
            result['aliases'] = list(dict.fromkeys(result['aliases'] + row['aliases']))
            evidence['verified_identity_sources'].append({'source_url': row['source_url'], 'provider_ids': row['provider_ids'], 'aliases': row['aliases']})
        return result
    profiles = _profiles(root)
    sources = []
    reader = _Reader(root, client)
    failure = None
    profile_aliases = []
    profile_evidence = []
    for work in works:
        if 'People' in work and (not any((p.get('Id') == person.get('Id') and _name(p.get('Name')) == wanted for p in work.get('People') or []))):
            continue
        for key, profile in profiles:
            if not _same_work(root, work, key, profile):
                continue
            same_people = [p for p in list(profile.get('actors') or []) + list(profile.get('crew') or []) if _name(p.get('Name')) == wanted]
            if not same_people:
                continue
            for record in same_people:
                aliases = record.get('Aliases') or []
                if isinstance(aliases, str):
                    aliases = [aliases]
                aliases = [a.strip() for a in aliases if isinstance(a, str) and a.strip() and (len(a.strip()) <= 120) and (_name(a) != wanted)]
                if aliases:
                    profile_aliases.extend(aliases)
                    profile_evidence.append({'work_id': work['Id'], 'source_url': profile.get('source_url'), 'cast_name': record['Name'], 'aliases': aliases})
            source = profile.get('source_url', '')
            parsed = urllib.parse.urlsplit(source)
            if parsed.scheme == 'https' and parsed.hostname == 'zh.wikipedia.org' and parsed.path.startswith('/wiki/'):
                sources.append((source, work['Id']))
            elif parsed.scheme == 'https' and parsed.hostname == 'baike.baidu.com':
                try:
                    linked = _linked_work_wiki(root, reader, profile)
                    if linked:
                        sources.append((linked, work['Id']))
                except (OSError, ValueError, KeyError, TypeError) as error:
                    failure = error
        if len(sources) >= 2:
            break

    def profile_alias_result():
        aliases = list(dict.fromkeys(profile_aliases))[:12]
        if not aliases:
            return None
        return {'name': aliases[0], 'aliases': aliases, 'provider_ids': {}, 'image_url': None, 'verified': True, 'evidence': {'kind': 'verified-work-name-equivalence', 'cast_name': name, 'source_url': profile_evidence[0].get('source_url'), 'work_id': profile_evidence[0]['work_id'], 'profile_aliases': profile_evidence}}
    if not sources:
        equivalent = profile_alias_result()
        if equivalent:
            return equivalent
        if failure:
            if isinstance(failure, IdentityLookupError):
                raise failure from None
            raise IdentityLookupError('invalid-work-identity') from None
        return None
    found = {}
    documented = {}
    for source, work_id in list(dict.fromkeys(sources))[:2]:
        try:
            parsed_source = urllib.parse.urlsplit(source)
            query = dict(urllib.parse.parse_qsl(parsed_source.query, keep_blank_values=True))
            query['variant'] = 'zh-cn'
            converted = urllib.parse.urlunsplit(parsed_source._replace(query=urllib.parse.urlencode(query)))
            content = reader.get(converted)
            aliases = _cast_aliases(content, name)
            if len(aliases) == 1:
                documented[aliases[0]] = {'name': aliases[0], 'aliases': aliases, 'provider_ids': {}, 'image_url': None, 'verified': True, 'evidence': {'kind': 'verified-work-name-equivalence', 'work_id': work_id, 'source_url': source, 'cast_name': name, 'original_name': aliases[0]}}
            links = _cast_links(content, source, name)
            if len(links) > 2:
                continue
            for linked in links:
                try:
                    identity = _person(reader, linked)
                except IdentityLookupError as error:
                    if error.code == 'not-found':
                        continue
                    failure = error
                    continue
                if identity:
                    found[identity['wikidata_id']] = {**identity, 'verified': True, 'evidence': {'kind': 'verified-work-cast-link', 'work_id': work_id, 'source_url': source, 'cast_name': name, 'person_url': linked, 'wikidata_id': identity['wikidata_id']}}
        except (OSError, ValueError, KeyError, TypeError) as error:
            failure = error
    if len(found) == 1:
        return next(iter(found.values()))
    if len(found) > 1:
        return None
    equivalent = profile_alias_result()
    if equivalent:
        return equivalent
    if len(documented) == 1:
        return next(iter(documented.values()))
    if failure:
        if isinstance(failure, IdentityLookupError):
            raise failure from None
        raise IdentityLookupError('invalid-response') from None
    return None
