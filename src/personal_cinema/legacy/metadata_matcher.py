from personal_cinema.runtime_paths import proxy_handler, option
from personal_cinema.network_security import credential_opener
"""Independent, bounded Jellyfin metadata matching worker for the cloud library."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import argparse
import difflib
import hashlib
import html
import json
import logging
import os
import shutil
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from library_routing import catalog_relative, queue_animation
from smart_metadata import choose_match, derive_hints, metadata_action, normalize_title, persist_sidecars, retry_due
from library_tasks import observe
from catalog_reconcile import queue_movie
ROOT = Path(os.environ.get('MEDIA_LIBRARY_ROOT', _cinema_path('')))
DEFAULTS = {'enabled': True, 'poll_seconds': 60, 'max_items_per_cycle': 5, 'max_titles': 4, 'preferred_provider': 'TheMovieDb', 'search_languages': ['zh-CN', 'en'], 'minimum_score': 0.94, 'minimum_margin': 0.08, 'search_cache_seconds': 86400, 'retry_seconds': [120, 600, 3600, 43200]}
API_JSON_LIMIT = 16 * 1024 * 1024
IMAGE_LIMIT = 12 * 1024 * 1024


def read_api_json(response):
    body = response.read(API_JSON_LIMIT + 1)
    if len(body) > API_JSON_LIMIT:
        raise ValueError('服务 JSON 响应超过 16 MiB，已停止读取')
    return json.loads(body) if body else None

def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return fallback

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)

class Jellyfin:

    def __init__(self, root):
        self.root = root
        self.token = _cinema_vault().get(option('foundation.jellyfin_credential', 'jellyfin.token'))
        self.http = credential_opener(proxy_handler())
        self.authorization = 'MediaBrowser Client="SmartMetadata", Device="Windows", DeviceId="smart-metadata-worker-' + str(os.getpid()) + '-' + secrets.token_hex(6) + '", Version="1.0"'

    def request(self, path, data=None, method=None, image=False):
        for attempt in range(2):
            try:
                return self.request_once(path, data, method, image)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code != 401 or attempt:
                    raise
                self.token = None

    def request_once(self, path, data=None, method=None, image=False):
        if not self.token:
            account = read_json(self.root / 'secrets/accounts.json', {})['jellyfin_admin']
            request = urllib.request.Request(_cinema_endpoint('jellyfin') + '/Users/AuthenticateByName', data=json.dumps({'Username': account['username'], 'Pw': account['password']}).encode(), headers={'Authorization': self.authorization, 'Content-Type': 'application/json'})
            with self.http.open(request, timeout=30) as response:
                self.token = read_api_json(response)['AccessToken']
        headers = {'Authorization': self.authorization + ', Token="' + self.token + '"', 'Content-Type': 'application/json'}
        request = urllib.request.Request(_cinema_endpoint('jellyfin') + path, data=None if data is None else json.dumps(data).encode(), headers=headers, method=method)
        try:
            with self.http.open(request, timeout=90 if '/Apply/' in path else 45) as response:
                if not image:
                    return read_api_json(response)
                body = response.read(IMAGE_LIMIT + 1)
                if len(body) > IMAGE_LIMIT:
                    raise ValueError('Metadata image exceeds limit')
                return body
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 401:
                self.token = None
            raise

    def json(self, path, data=None, method=None):
        return self.request(path, data, method)

    def image(self, path):
        body = self.request(path, image=True)
        if not body.startswith((b'\xff\xd8', b'\x89PNG', b'RIFF')):
            raise ValueError('Metadata image is not an image')
        return body

class Matcher:

    def __init__(self, root=ROOT, client=None, read_only=False):
        self.root = Path(root)
        self.client = client or Jellyfin(self.root)
        self.config = {**DEFAULTS, **read_json(self.root / 'data/metadata-config.json', {})}
        self.state = read_json(self.root / 'data/metadata-state.json', {})
        self.search_cache = read_json(self.root / 'data/metadata-search-cache.json', {})
        self.known = read_json(self.root / 'data/metadata-known-identities.json', {})
        self.known_titles = read_json(self.root / 'data/metadata-known-titles.json', {})
        self.provider_aliases = read_json(self.root / 'data/metadata-provider-aliases.json', {})
        self.read_only = read_only

    def sources(self, item, entries):
        catalog = self.root / 'catalog'
        try:
            relative = Path(item.get('Path') or '').absolute().relative_to(catalog.absolute()).as_posix()
            cache_path(catalog, relative)
        except ValueError:
            return ([], None)
        if item['Type'] == 'Movie':
            candidates = [entry for entry in entries.values() if not entry.get('attachment_role') and (not entry.get('catalog_hidden')) and (str(Path(catalog_relative(entry)).with_suffix('.strm')).replace('\\', '/') == relative)]
            media_file = catalog / relative
        elif item['Type'] == 'Series':
            candidates = [entry for entry in entries.values() if entry.get('kind') == 'Shows' and catalog_relative(entry).startswith(relative.rstrip('/') + '/')]
            media_file = catalog / relative / 'tvshow.strm'
        else:
            return ([], None)
        return ([entry['remote'] for entry in candidates[:30]], media_file)

    def nfo_ids(self, media_file):
        relative = media_file.with_suffix('.nfo').absolute().relative_to((self.root / 'catalog').absolute()).as_posix()
        path = cache_path(self.root / 'catalog', relative)
        if not path.exists() or path.stat().st_size > 2 * 1024 * 1024:
            return {}
        try:
            node = ET.fromstring(path.read_bytes())
            ids = {}
            for unique in node.findall('uniqueid'):
                kind, value = ((unique.get('type') or '').lower(), (unique.text or '').strip())
                if kind in ('tmdb', 'tvdb') and value.isdecimal():
                    ids[kind.title() if kind == 'tmdb' else 'Tvdb'] = value
                if kind == 'imdb' and value.startswith('tt') and value[2:].isdecimal():
                    ids['Imdb'] = value
            return ids
        except (OSError, ET.ParseError):
            return {}

    def remember(self, item):
        if not item.get('ProductionYear') or not item.get('ProviderIds'):
            return
        for title in (item.get('Name'), item.get('OriginalTitle')):
            if not title:
                continue
            key = f"{item['Type']}|{normalize_title(title)}|{item['ProductionYear']}"
            identities = self.known.setdefault(key, [])
            self.known_titles[key] = title
            if item['ProviderIds'] not in identities:
                identities.append(item['ProviderIds'])

    def expand_titles(self, hints):
        """Reuse verified translations as search aliases, never as guessed years."""
        expanded = {**hints, 'titles': list(hints['titles'])}
        from catalog_intelligence import simplified
        for title in list(expanded['titles']):
            translated = simplified(title)
            if translated != title and translated not in expanded['titles']:
                expanded['titles'].insert(0, translated)
        franchise = read_json(self.root / 'data/franchise-movies.json', {})
        ordered = []
        for number in hints.get('numbered_movies', []):
            work = franchise.get(number['family'], {}).get(str(number['number']))
            if work and max((difflib.SequenceMatcher(None, normalize_title(number['label']), normalize_title(t)).ratio() for t in work.get('aliases', [work['title']])), default=0) >= 0.72:
                ordered.append(work)
        if len({json.dumps(w['provider_ids'], sort_keys=True) for w in ordered}) == 1:
            work = ordered[0]
            expanded.update(provider_ids=work['provider_ids'], year=work['year'], conflicts=[], verified_source_identity=True, required_numbers=[])
            expanded['titles'].insert(0, work['title'])
        aliases = read_json(self.root / 'data/recognition-aliases.json', [])
        confirmed = [a for a in aliases if a.get('kind') == hints['kind'] and a.get('year') == hints.get('year') and any((normalize_title(t) in {normalize_title(n) for n in a.get('aliases', [])} for t in hints['titles']))]
        identities = {json.dumps(a['provider_ids'], sort_keys=True) for a in confirmed}
        if len(identities) == 1:
            ids = json.loads(next(iter(identities)))
            if not hints.get('provider_ids') or hints['provider_ids'] == ids:
                expanded['provider_ids'] = ids
                for a in confirmed:
                    if a['title'] not in expanded['titles']:
                        expanded['titles'].insert(0, a['title'])
        identities = []
        for title in hints['titles']:
            prefix = f"{hints['kind']}|{normalize_title(title)}|"
            for key, values in self.known.items():
                if key.startswith(prefix) and (not hints.get('year') or key.endswith('|' + str(hints['year']))):
                    identities.extend(values)
        for key, values in self.known.items():
            if not key.startswith(hints['kind'] + '|') or (hints.get('year') and (not key.endswith('|' + str(hints['year'])))):
                continue
            shared = any((any((identity.get(provider) == value for provider, value in known.items())) for identity in identities for known in values))
            title = self.known_titles.get(key)
            if shared and title and (title not in expanded['titles']):
                expanded['titles'].append(title)
        return expanded

    def known_ids(self, hints):
        if not hints.get('year'):
            return {}
        matches = []
        for title in hints['titles']:
            key = f"{hints['kind']}|{normalize_title(title)}|{hints['year']}"
            for ids in self.known.get(key, []):
                if ids not in matches:
                    matches.append(ids)
        return matches[0] if len(matches) == 1 else {}

    def source_hints(self, sources, kind):
        from franchise_context import context, verified_source_identity, verified_special_identity
        hints = derive_hints(sources, kind=kind)
        titles = []
        identities = []
        if kind == 'Movie':
            for source in sources:
                evidence = context(source)
                for title in evidence.get('titles', []):
                    for cleaned in derive_hints([title], kind=kind)['titles']:
                        if cleaned not in titles:
                            titles.append(cleaned)
                work = verified_source_identity(source) or verified_special_identity(source)
                if work:
                    identities.append(work)
        hints['titles'] = titles + [t for t in hints['titles'] if t not in titles]
        if identities and len({json.dumps(w['provider_ids'], sort_keys=True) for w in identities}) == 1:
            work = identities[0]
            hints.update(provider_ids=work['provider_ids'], conflicts=[], verified_source_identity=True, required_numbers=[])
            if work.get('provider_year') or work.get('year'):
                hints['year'] = work.get('provider_year') or work['year']
            hints['titles'].insert(0, work['title'])
        return self.expand_titles(hints)

    def repair_generated_title(self, item, sources=None):
        """A known identity must not keep a generated folder/ID as its title."""
        if self.read_only or item.get('IsLocked') or 'Name' in item.get('LockedFields', []):
            return item
        source_year = derive_hints(sources or [], kind=item['Type']).get('year')
        matches = [a for a in read_json(self.root / 'data/recognition-aliases.json', []) if a.get('kind') == item['Type'] and (a.get('year') == item.get('ProductionYear') or a.get('year') == source_year) and all((item.get('ProviderIds', {}).get(k) == v for k, v in a.get('provider_ids', {}).items()))]
        if item['Type'] == 'Movie':
            from franchise_context import verified_special_identity
            releases = [w for source in sources or [] if (w := verified_special_identity(source)) and str(w['provider_ids'].get('Tmdb')) == str((item.get('ProviderIds') or {}).get('Tmdb'))]
            if releases and len({w['provider_ids']['Tmdb'] for w in releases}) == 1:
                matches = [releases[0]]
        if len(matches) == 1:
            desired = dict(matches[0])
        elif item['Type'] == 'Movie' and (item.get('ProviderIds') or {}).get('Tmdb'):
            desired = {'title': item.get('Name', ''), 'year': item.get('ProductionYear')}
        else:
            return item
        if item['Type'] == 'Movie':
            from movie_groups import clean_movie_title
            desired['title'] = clean_movie_title(desired['title'])
        year = desired['year'] if not any((f in item.get('LockedFields', []) for f in ('ProductionYear', 'PremiereDate'))) else item.get('ProductionYear')
        premiere = desired.get('premiere_date')
        if item.get('Name') == desired['title'] and item.get('ProductionYear') == year and (not premiere or str(item.get('PremiereDate', '')).startswith(premiere)):
            return item
        backup = self.root / 'data/metadata-title-backups' / (item['Id'] + '.json')
        if not backup.exists():
            write_json(backup, item)
        updated = {**item, 'Name': desired['title'], 'ProductionYear': year}
        if premiere and 'PremiereDate' not in item.get('LockedFields', []):
            updated['PremiereDate'] = premiere + 'T00:00:00.0000000Z'
        nfo = Path(item.get('Path') or '').with_suffix('.nfo')
        if item.get('Path') and nfo.is_file() and nfo.absolute().is_relative_to((self.root / 'catalog').absolute()):
            node = ET.parse(nfo).getroot()
            if not backup.with_suffix('.nfo').exists():
                shutil.copy2(nfo, backup.with_suffix('.nfo'))
            for tag, val in [('title', desired['title']), ('year', year), ('premiered', premiere)]:
                if val is None:
                    continue
                child = node.find(tag)
                if child is None:
                    child = ET.SubElement(node, tag)
                child.text = str(val)
            temp = nfo.with_name(nfo.name + '.repair.tmp')
            ET.ElementTree(node).write(temp, encoding='utf-8', xml_declaration=True)
            ET.parse(temp)
            temp.replace(nfo)
        self.client.json('/Items/' + item['Id'], updated, method='POST')
        return updated

    def search(self, hints, item_id=None, fallback=False):
        results = []
        languages = option('metadata.languages', self.config['search_languages'])
        titles = hints['titles'][:self.config['max_titles']] or ['']
        if hints.get('provider_ids'):
            titles = titles[:1]
        for title in titles:
            for language in languages:
                info = {'Name': title, 'MetadataLanguage': language}
                if hints.get('year'):
                    info['Year'] = hints['year']
                if hints.get('provider_ids'):
                    info['ProviderIds'] = hints['provider_ids']
                payload = {'SearchInfo': info, 'IncludeDisabledProviders': False}
                provider = None if fallback else self.config['preferred_provider']
                if provider:
                    payload['SearchProviderName'] = provider
                if item_id:
                    payload['ItemId'] = item_id
                key = hashlib.sha256(json.dumps([hints['kind'], info, provider], sort_keys=True).encode()).hexdigest()
                cached = self.search_cache.get(key, {})
                if cached.get('expires', 0) > time.time():
                    found = cached['results']
                else:
                    found = self.client.json('/Items/RemoteSearch/' + hints['kind'], payload)
                    self.search_cache[key] = {'results': found, 'expires': time.time() + (self.config['search_cache_seconds'] if found else 300)}
                results.extend([{**result, '_language': language} for result in found])
        return results

    def official_aliases(self, kind, ident):
        """Exact provider page aliases enrich names, never bypass ID/year checks."""
        if kind not in ('Movie', 'Series') or not str(ident).isdecimal():
            return []
        key = kind + '|' + str(ident)
        prior = self.provider_aliases.get(key, {})
        if prior.get('expires', 0) > time.time():
            return prior.get('titles', [])
        from chinese_credits import Document
        import re
        category = 'movie' if kind == 'Movie' else 'tv'
        url = 'https://www.themoviedb.org/' + category + '/' + str(ident) + '/titles'
        titles = []
        verified = False
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('Alias page too large')
            doc = Document(raw.decode('utf-8'))
            canonical = next((n.attrs.get('href', '') for n in doc.root.find('link') if n.attrs.get('rel') == 'canonical'), '')
            if not re.fullmatch('https://www\\.themoviedb\\.org/' + category + '/' + re.escape(str(ident)) + '(?:-[^/?#]*)?(?:/titles)?/?', canonical):
                raise ValueError('Alias provider identity mismatch')
            for table in doc.root.find('table'):
                if 'titles' not in table.attrs.get('class', '').split():
                    continue
                for row in table.find('tr'):
                    cells = row.find('td')
                    if cells:
                        title = ' '.join(cells[0].text().split())
                        if title and len(title) <= 200 and (title not in titles):
                            titles.append(title)
            verified = True
        except (OSError, ValueError, UnicodeError):
            pass
        self.provider_aliases[key] = {'source_url': url, 'titles': titles if verified else [], 'verified': verified, 'expires': time.time() + (7 * 86400 if titles and verified else 3600)}
        if not self.read_only:
            write_json(self.root / 'data/metadata-provider-aliases.json', self.provider_aliases)
        return titles if verified else []

    def resolve(self, hints, item_id=None):
        results = self.search(hints, item_id)
        decision = choose_match(hints, results, self.config['minimum_score'], self.config['minimum_margin'])
        if decision['status'] != 'matched' and decision.get('reason') == 'No sufficiently close title/year match':
            results += self.search(hints, item_id, fallback=True)
            decision = choose_match(hints, results, self.config['minimum_score'], self.config['minimum_margin'])
        if decision.get('reason') == 'No sufficiently close title/year match':
            wanted = {normalize_title(t) for t in hints['titles']}
            confirmed = {}
            for candidate in decision.get('candidates', [])[:3]:
                ident = (candidate.get('provider_ids') or {}).get('Tmdb')
                aliases = self.official_aliases(hints['kind'], ident)
                if wanted.intersection((normalize_title(t) for t in aliases)):
                    confirmed[str(ident)] = aliases
            if confirmed:
                results = [{**result, '_official_aliases': confirmed.get(str((result.get('ProviderIds') or {}).get('Tmdb')), [])} for result in results]
                decision = choose_match(hints, results, self.config['minimum_score'], self.config['minimum_margin'])
        if decision.get('reason') == 'Multiple similarly strong candidates':
            from chinese_identity import enrich_source_identity
            wanted = {normalize_title(t) for t in hints['titles']}
            verified = []
            for candidate in decision.get('candidates', [])[:3]:
                if candidate.get('score', 0) < self.config['minimum_score']:
                    continue
                item = {'Type': hints['kind'], 'Name': candidate['name'], 'ProductionYear': candidate.get('year'), 'ProviderIds': candidate['provider_ids']}
                enriched, wiki = enrich_source_identity(item, {'library_root': str(self.root)})
                aliases = {normalize_title(t) for t in enriched.get('RecognitionAliases', [])}
                if wiki and wanted.intersection(aliases):
                    verified.append(enriched['ProviderIds'])
            if len(verified) == 1:
                decision = choose_match({**hints, 'provider_ids': verified[0]}, results, self.config['minimum_score'], self.config['minimum_margin'])
        return decision

    def persist(self, item, media_file):
        if not self.read_only:
            observe(self.root, 'metadata', 'poster', item.get('Name', ''), item_id=item['Id'])
        images = {}
        try:
            from verified_artwork import artwork_target
            if (item.get('ImageTags') or {}).get('Primary') and (not artwork_target(self.root, item, 'Primary').is_file()):
                images['Primary'] = self.client.image('/Items/' + item['Id'] + '/Images/Primary')
            if item.get('BackdropImageTags') and (not artwork_target(self.root, item, 'Backdrop').is_file()):
                images['Backdrop'] = self.client.image('/Items/' + item['Id'] + '/Images/Backdrop/0')
        except (OSError, ValueError):
            logging.warning('A metadata image was unavailable; keeping existing local art')
        paths = persist_sidecars(self.root / 'catalog', media_file, item, images)
        expected = ['nfo']
        if (item.get('ImageTags') or {}).get('Primary'):
            expected.append('poster')
        if item.get('BackdropImageTags'):
            expected.append('backdrop')
        return all((paths[key].is_file() for key in expected))

    def report(self):
        if self.read_only:
            return
        counts = {}
        rows = []
        labels = {'matched': '已自动匹配', 'already_matched': '已识别，保留资料', 'locked': '人工锁定，保留资料', 'needs_review': '待确认', 'retry': '等待重试', 'updating': '资料正在更新', 'pending': '等待检查'}
        for record in self.state.values():
            status = record['status']
            counts[status] = counts.get(status, 0) + 1
            candidates = record.get('candidates') or []
            candidate_text = '；'.join((f"{c.get('name')} ({c.get('year')}) · {c.get('score')}" for c in candidates))
            ids = json.dumps(record.get('provider_ids') or {}, ensure_ascii=False)
            rows.append('<tr><td>' + html.escape(record.get('name') or '') + '</td><td>' + labels.get(status, status) + '</td><td>' + html.escape(ids) + '</td><td>' + html.escape(record.get('reason') or '') + ('<details><summary>候选影片</summary>' + html.escape(candidate_text) + '</details>' if candidate_text else '') + '</td></tr>')
        page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="refresh" content="60"><title>影视资料匹配状态</title><style>body{font:16px/1.65 system-ui;margin:36px;color:#172536;background:#f4f6f8}table{width:100%;border-collapse:collapse;background:white}td,th{text-align:left;padding:14px;border-bottom:1px solid #ddd}h1{font-size:28px}details{max-width:540px}</style><h1>影视资料匹配状态</h1><p>新增影片自动检查；已识别和人工锁定的资料会保留。待确认条目不会直接套用不确定的海报。</p><table><tr><th>影片 / 剧集</th><th>状态</th><th>资料库 ID</th><th>说明</th></tr>' + ''.join(rows) + '</table></html>'
        (self.root / 'data/metadata-report.html').write_text(page, encoding='utf-8')
        write_json(self.root / 'data/metadata-state.json', self.state)
        write_json(self.root / 'data/metadata-search-cache.json', self.search_cache)
        write_json(self.root / 'data/metadata-known-identities.json', self.known)
        write_json(self.root / 'data/metadata-known-titles.json', self.known_titles)
        write_json(self.root / 'data/metadata-status.json', {'updated_at': time.time(), 'counts': counts, 'total': len(self.state), 'minimum_score': self.config['minimum_score'], 'minimum_margin': self.config['minimum_margin']})

    def cycle(self):
        if not self.config['enabled']:
            return
        if not self.read_only:
            observe(self.root, 'metadata', 'index', '检查待识别影片')
        entries = read_json(self.root / 'data/cache-state.json', {})
        listed = self.client.json('/Items?Recursive=true&IncludeItemTypes=Movie,Series&Fields=Path')
        attempted = 0
        routing_candidates = []
        for brief in listed.get('Items', []):
            if getattr(self, 'target_ids', None) is not None and brief['Id'] not in self.target_ids:
                continue
            sources, media_file = self.sources(brief, entries)
            if not sources:
                continue
            item = self.client.json('/Items/' + brief['Id'])
            from manual_match import override_for_item, apply_identity
            try:
                manual = override_for_item(self.root, item, entries)
                if manual:
                    if not self.read_only:
                        item = apply_identity(self.root, self.client, item, manual, manual.get('request_id', 'automatic'))
                    self.state[item['Id']] = {'status': 'locked', 'name': item['Name'], 'provider_ids': manual['provider_ids'], 'checked_at': time.time(), 'persisted': True, 'source_verified': True, 'manual_confirmed': True, 'reference_url': manual.get('reference_url', ''), 'reason': '用户已确认作品；自动匹配保留此身份'}
                    routing_candidates.append(item)
                    if not self.read_only:
                        queue_movie(self.root, item, entries)
                    continue
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                self.state[item['Id']] = {'status': 'retry', 'name': item['Name'], 'provider_ids': item.get('ProviderIds') or {}, 'checked_at': time.time(), 'next_attempt_at': time.time() + 120, 'manual_confirmed': True, 'reason': '手动匹配规则待恢复；已暂停自动覆盖：' + type(error).__name__}
                continue
            item = self.repair_generated_title(item, sources)
            routing_candidates.append(item)
            action = metadata_action(item)
            record = self.state.setdefault(item['Id'], {'status': 'pending', 'name': item['Name'], 'provider_ids': item.get('ProviderIds') or {}})
            hints = self.source_hints(sources, item['Type'])
            source_revision = max((e.get('source_renamed_at', 0) for e in entries.values() if e.get('remote') in sources), default=0)
            renamed_source = bool(action != 'locked' and source_revision > record.get('source_revision', 0))
            renamed_titles = {e['source_name_override'] for e in entries.values() if e.get('remote') in sources and e.get('source_name_override')}
            if renamed_source and len(renamed_titles) == 1:
                hints = derive_hints([next(iter(renamed_titles))], kind=item['Type'])
            rename_years = {e.get('source_rename_year') or e.get('source_year_confirmed') for e in entries.values() if e.get('remote') in sources and (e.get('source_rename_year') or e.get('source_year_confirmed'))}
            if not hints.get('year') and len(rename_years) == 1:
                hints['year'] = next(iter(rename_years))
            if not hints['titles'] and (not hints.get('provider_ids')):
                record.update(status='needs_review', name=item['Name'], checked_at=time.time(), reason='文件名只有年份或编码参数，请补充作品名称后重新入库', candidates=[])
                continue
            unverified_native = bool(action == 'preserve' and record.get('status') in ('pending', 'retry', 'needs_review') and (not self.nfo_ids(media_file)) and any((e.get('created_at') for e in entries.values() if e.get('remote') in sources)))
            year_conflict = bool(action != 'locked' and hints.get('year') and item.get('ProductionYear') and (hints['year'] != item['ProductionYear']) and (not hints.get('provider_ids')) and (not self.nfo_ids(media_file)))
            source_override = bool(hints.get('verified_source_identity') and (not item.get('IsLocked')) and set(item.get('LockedFields', [])).issubset({'Cast'}))
            identity_conflict = bool((action != 'locked' or source_override) and hints.get('provider_ids') and any((item.get('ProviderIds', {}).get(k) != v for k, v in hints['provider_ids'].items())))
            if year_conflict or identity_conflict or unverified_native or renamed_source:
                action = 'search'
            signature = hashlib.sha256(json.dumps([hints, item.get('ProviderIds'), item.get('Name'), source_revision], sort_keys=True).encode()).hexdigest()
            base = {'name': item['Name'], 'provider_ids': item.get('ProviderIds') or {}, 'checked_at': time.time(), 'signature': signature}
            if action in ('preserve', 'locked'):
                persisted = bool(record.get('persisted'))
                reason = ''
                if action == 'preserve':
                    self.remember(item)
                    if not persisted and retry_due(record, signature) and (attempted < self.config['max_items_per_cycle']):
                        attempted += 1
                        try:
                            persisted = self.persist(item, media_file)
                        except (OSError, ValueError):
                            persisted = False
                        if not persisted:
                            record['next_attempt_at'] = time.time() + self.config['retry_seconds'][0]
                    if not persisted:
                        reason = '本地资料保存排队或等待图片重试；已识别信息保持不变'
                record.update({**base, 'status': 'already_matched' if action == 'preserve' else 'locked', 'reason': reason, 'persisted': persisted, 'candidates': []})
                if not self.read_only and (action == 'preserve' or (action == 'locked' and set(item.get('LockedFields', [])).issubset({'Cast'}))):
                    queue_movie(self.root, item, entries)
                continue
            if attempted >= self.config['max_items_per_cycle'] or not retry_due(record, signature):
                continue
            attempted += 1
            attempts = record.get('attempts', 0) + 1
            delays = self.config['retry_seconds']
            delay = delays[min(attempts - 1, len(delays) - 1)]
            record.update({**base, 'attempts': attempts, 'next_attempt_at': time.time() + delay})
            try:
                if not self.read_only:
                    observe(self.root, 'metadata', 'identity', item.get('Name', ''), item_id=item['Id'])
                hints = self.expand_titles(hints)
                trusted = hints.get('provider_ids') or ({} if renamed_source else (None if year_conflict or unverified_native else item.get('ProviderIds')) or self.nfo_ids(media_file) or ({} if unverified_native else self.known_ids(hints)))
                if trusted:
                    hints['provider_ids'] = trusted
                    hints['conflicts'] = []
                if hints['conflicts']:
                    decision = {'status': 'needs_review', 'reason': '; '.join(hints['conflicts']), 'candidates': []}
                else:
                    decision = self.resolve(hints, item['Id'])
                record.update({key: value for key, value in decision.items() if key != 'candidate'})
                if decision['status'] != 'matched':
                    if not self.read_only:
                        observe(self.root, 'metadata', 'identity', item.get('Name', ''), 'needs_review', item_id=item['Id'], retry_at=record.get('next_attempt_at'))
                        self.report()
                    continue
                if renamed_source:
                    identity_conflict = True
                latest = self.client.json('/Items/' + item['Id'])
                candidate_ids = decision['candidate'].get('ProviderIds') or {}
                same_decision = bool(candidate_ids) and all((str((latest.get('ProviderIds') or {}).get(k, '')) == str(v) for k, v in candidate_ids.items()))
                changed = latest.get('ProviderIds') != item.get('ProviderIds') or latest.get('Name') != item.get('Name')
                if metadata_action(latest) == 'locked' and (not source_override) or (changed and (not same_decision)):
                    record.update({'status': 'retry', 'reason': 'Metadata changed during search; retaining current identification'})
                    continue
                if year_conflict or identity_conflict:
                    write_json(self.root / 'data/metadata-correction-backups' / (item['Id'] + '.json'), item)
                if identity_conflict or year_conflict:
                    backup = self.root / 'data/metadata-correction-sidecars' / item['Id']
                    backup.mkdir(parents=True, exist_ok=True)
                    nfo = media_file.with_suffix('.nfo')
                    if nfo.exists():
                        target = backup / ('old-' + str(time.time_ns()) + '.nfo')
                        shutil.move(str(nfo), str(target))
                    for suffix in ('-poster.jpg', '-fanart.jpg', '-thumb.jpg', '-landscape.jpg'):
                        art = media_file.with_name(media_file.stem + suffix)
                        if art.exists():
                            shutil.move(str(art), str(backup / (str(time.time_ns()) + '-' + art.name)))
                    if len(item.get('MediaSources') or []) <= 1:
                        for art in (media_file.parent / 'poster.jpg', media_file.parent / 'fanart.jpg'):
                            if art.exists():
                                shutil.move(str(art), str(backup / (str(time.time_ns()) + '-' + art.name)))
                    if 'Cast' in latest.get('LockedFields', []):
                        latest = {**latest, 'LockedFields': [f for f in latest['LockedFields'] if f != 'Cast']}
                        self.client.json('/Items/' + item['Id'], latest, method='POST')
                    latest = {**latest, 'ProviderIds': dict(hints.get('provider_ids') or {})}
                    self.client.json('/Items/' + item['Id'], latest, method='POST')
                self.client.json('/Items/RemoteSearch/Apply/' + item['Id'] + '?replaceAllImages=' + ('true' if year_conflict or identity_conflict else 'false'), decision['candidate'])
                updated = self.repair_generated_title(self.client.json('/Items/' + item['Id']), sources)
                routing_candidates.append(updated)
                persisted = self.persist(updated, media_file)
                self.remember(updated)
                if not self.read_only:
                    queue_movie(self.root, updated, entries)
                new_signature = hashlib.sha256(json.dumps([derive_hints(sources, kind=updated['Type']), updated.get('ProviderIds'), updated.get('Name')], sort_keys=True).encode()).hexdigest()
                record.update({'name': updated['Name'], 'status': 'matched', 'source_verified': True, 'source_revision': source_revision, 'provider_ids': updated.get('ProviderIds') or {}, 'persisted': persisted, 'reason': '' if persisted else '图片尚未保存，稍后重试', 'signature': new_signature, 'next_attempt_at': time.time() + (86400 if persisted else delay)})
            except (OSError, ValueError, KeyError) as error:
                record.update({'status': 'retry', 'reason': type(error).__name__ + ': metadata service temporarily unavailable'})
                logging.warning('Metadata attempt failed for item %s: %s', item['Id'], type(error).__name__)
            if not self.read_only:
                self.report()
        if not self.read_only:
            for candidate in routing_candidates:
                queue_animation(self.root, candidate, entries)
        live_ids = {item['Id'] for item in listed.get('Items', [])}
        self.state = {ident: record for ident, record in self.state.items() if ident in live_ids}
        self.report()
        if not self.read_only:
            observe(self.root, 'metadata', 'identity', '本轮识别检查完成', 'idle', count=attempted)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['run', 'once', 'preview'])
    parser.add_argument('--id')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', filename=ROOT / 'logs/metadata-matcher.log', encoding='utf-8')
    matcher = Matcher(read_only=args.command == 'preview')
    if args.command == 'preview':
        entry = read_json(ROOT / 'data/cache-state.json', {})[args.id]
        hints = derive_hints([entry['remote']], kind='Movie' if entry['kind'] == 'Movies' else 'Series')
        hints = matcher.expand_titles(hints)
        decision = matcher.resolve(hints)
        print(json.dumps({'hints': hints, 'decision': {key: value for key, value in decision.items() if key != 'candidate'}, 'selected_provider_ids': (decision.get('candidate') or {}).get('ProviderIds')}, ensure_ascii=True))
        return
    import msvcrt
    lockfile = (ROOT / 'data/metadata-matcher.lock').open('a+b')
    lockfile.seek(0)
    lockfile.write(b'0')
    lockfile.flush()
    lockfile.seek(0)
    msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
    while True:
        try:
            matcher.cycle()
        except (OSError, ValueError, KeyError) as error:
            observe(ROOT, 'metadata', 'identity', '识别服务暂不可用，将自动重试', 'error', retry_at=time.time() + matcher.config['poll_seconds'])
            logging.warning('Metadata cycle deferred: %s', type(error).__name__)
        if args.command == 'once':
            break
        time.sleep(matcher.config['poll_seconds'])
if __name__ == '__main__':
    main()
