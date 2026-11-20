"""Prefer verified Chinese encyclopedia billing for this personal media library."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
import xml.etree.ElementTree as ET
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from chinese_credits import Document, identity_matches, parse_credits, parse_crew, ordered_people
from metadata_matcher import Jellyfin, read_json, write_json
from people_localization import credit_types, merge_person_roles
ROOT = Path(os.environ.get('MEDIA_LIBRARY_ROOT', _cinema_path('')))

def fingerprint(people):
    actors = [p for p in people if p.get('Type') in ('Actor', 'GuestStar')]
    crew = sorted((p for p in people if p.get('Type') not in ('Actor', 'GuestStar')), key=lambda p: (p.get('Type', ''), p.get('Name', ''), p.get('Role') or ''))
    value = [{k: p.get(k) or '' for k in ('Name', 'Type', 'Role')} for p in actors + crew]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def sidecar(root, item, people):
    relative = Path(item['Path']).absolute().relative_to((root / 'catalog').absolute())
    directory_nfo = {'Series': 'tvshow.nfo', 'Season': 'season.nfo'}
    path = cache_path(root / 'catalog', (relative / directory_nfo[item['Type']]).as_posix() if item['Type'] in directory_nfo else relative.with_suffix('.nfo').as_posix())
    node = ET.parse(path).getroot() if path.exists() else ET.Element({'Series': 'tvshow', 'Season': 'season'}.get(item['Type'], 'movie'))
    if not path.exists():
        ET.SubElement(node, 'title').text = item['Name']
        if item['Type'] == 'Season':
            ET.SubElement(node, 'seasonnumber').text = str(item['IndexNumber'])
        for provider, ident in item.get('ProviderIds', {}).items():
            ET.SubElement(node, 'uniqueid', {'type': provider.lower()}).text = ident
    for child in list(node):
        if child.tag in ('actor', 'director', 'credits', 'composer'):
            node.remove(child)
    for index, person in enumerate(people):
        kinds = credit_types(person)
        if 'Actor' in kinds or 'GuestStar' in kinds:
            actor = ET.SubElement(node, 'actor')
            ET.SubElement(actor, 'name').text = person['Name']
            ET.SubElement(actor, 'role').text = person.get('Role', '')
            ET.SubElement(actor, 'order').text = str(index)
        for kind in kinds:
            if kind in ('Director', 'Writer', 'Composer'):
                ET.SubElement(node, {'Director': 'director', 'Writer': 'credits', 'Composer': 'composer'}[kind]).text = person['Name']
    locked = node.find('lockedfields')
    if locked is None:
        locked = ET.SubElement(node, 'lockedfields')
    fields = (locked.text or '').split('|')
    locked.text = '|'.join(dict.fromkeys([f for f in fields if f] + ['Cast']))
    ET.indent(node)
    return (path, ET.tostring(node, encoding='utf-8', xml_declaration=True))

def apply_profile(root, client, item, profile, state):
    assert profile['type'] == item['Type'] and profile['year'] == item.get('ProductionYear')
    if profile.get('catalog_relative'):
        same_path = Path(item['Path']).absolute() == (root / 'catalog' / profile['catalog_relative']).absolute()
        expected = str(profile.get('provider_ids', {}).get('Tmdb') or '')
        actual = str(item.get('ProviderIds', {}).get('Tmdb') or '')
        same_work = item['Type'] in ('Movie', 'Series') and bool(expected) and (expected == actual)
        assert same_path or same_work, 'Profile belongs to a different work/path'
    else:
        assert str(profile['provider_ids']['Tmdb']) == str(item.get('ProviderIds', {}).get('Tmdb'))
    assert len(profile['actors']) >= 3
    if item.get('IsLocked') and (not (profile.get('managed') and profile.get('catalog_relative'))):
        return {'status': 'manual-lock', 'reason': '保留人工锁定资料'}
    desired = ordered_people(item, profile)
    wanted = fingerprint(desired)
    actual = fingerprint(item.get('People', []))
    if actual == wanted and 'Cast' in item.get('LockedFields', []):
        return {'status': 'applied', 'digest': wanted, 'source_url': profile['source_url'], 'cast_count': len(profile['actors']), 'updated_at': time.time()}
    owned = profile.get('source') in ('baidu_baike', 'zh_wikipedia') and bool(profile.get('source_url'))
    if 'Cast' in item.get('LockedFields', []) and (not state.get('digest')) and (not profile.get('managed')) and (not owned):
        return {'status': 'manual-lock', 'reason': '保留已有演员表锁定'}
    path, content = sidecar(root, item, desired)
    backup = root / 'data/chinese-credits-backups' / item['Id']
    backup.mkdir(parents=True, exist_ok=True)
    if not (backup / 'item-before.json').exists():
        write_json(backup / 'item-before.json', item)
        if path.exists():
            (backup / 'original.nfo').write_bytes(path.read_bytes())
    latest = client.json('/Items/' + item['Id'])
    if fingerprint(latest.get('People', [])) != actual or latest.get('ProviderIds') != item.get('ProviderIds'):
        return {'status': 'retry', 'reason': '资料已变化，下轮重新核对'}
    payload = dict(latest)
    payload['People'] = desired
    payload['LockedFields'] = list(dict.fromkeys(latest.get('LockedFields', []) + ['Cast']))
    client.json('/Items/' + item['Id'], payload, method='POST')
    updated = client.json('/Items/' + item['Id'])
    assert fingerprint(updated['People']) == wanted, 'Credits update did not preserve requested order'
    resolved = merge_person_roles(updated.get('People', []))
    if len(resolved) < len(updated.get('People', [])):
        merged_payload = {**updated, 'People': resolved, 'LockedFields': list(dict.fromkeys(updated.get('LockedFields', []) + ['Cast']))}
        client.json('/Items/' + item['Id'], merged_payload, method='POST')
        desired = resolved
        wanted = fingerprint(desired)
        path, content = sidecar(root, updated, desired)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        with temporary.open('xb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {'status': 'applied', 'digest': wanted, 'source_url': profile['source_url'], 'cast_count': len(profile['actors']), 'updated_at': time.time()}

def source_candidates(item, config):
    """Search source spellings as well as a provider's regional translation."""
    key = item['Type'] + ':' + item['ProviderIds']['Tmdb']
    selected = config.get('source_urls', {}).get(key)
    root = Path(config.get('library_root', ROOT))
    manual = read_json(root / 'data/manual-metadata-overrides.json', {}).get('works', {}).get(key, {})
    reference = manual.get('reference_url', '')
    parsed = urllib.parse.urlsplit(reference)
    if manual.get('kind') == item['Type'] and str(manual.get('provider_ids', {}).get('Tmdb', '')) == str(item['ProviderIds']['Tmdb']) and (parsed.scheme == 'https') and (parsed.hostname in ('baike.baidu.com', 'zh.wikipedia.org')):
        selected = reference
        item = {**item, 'ExplicitChineseSource': True}
    declared = config.get('source_titles', {}).get(key, [])
    if declared:
        item = {**item, 'RecognitionAliases': list(dict.fromkeys(item.get('RecognitionAliases', []) + declared))}
    alias = next((a for a in config.get('recognition_aliases', []) if a.get('kind') == item['Type'] and a.get('year') == item.get('ProductionYear') and all((item.get('ProviderIds', {}).get(k) == v for k, v in a.get('provider_ids', {}).items()))), None)
    if alias:
        item = {**item, 'RecognitionAliases': list(dict.fromkeys(item.get('RecognitionAliases', []) + alias['aliases']))}
    chinese_aliases = [a for a in item.get('RecognitionAliases', []) if re.search('[㐀-鿿]', a) and (not re.search('[A-Za-z]{3}', a)) and (a != item['Name'])]
    names = [item['Name']] + list(dict.fromkeys(chinese_aliases))[:1]
    sources = [('baidu_baike', 'https://baike.baidu.com/item/' + urllib.parse.quote(name)) for name in names]
    sources.extend((('baidu_baike', 'https://baike.baidu.com/item/' + urllib.parse.quote(name) + '?force=1') for name in names))
    titles = []
    for name in names:
        titles.extend([name + '_(电影)', name, name.replace('：', '_').replace(':', '_')] if item['Type'] == 'Movie' else [name + '_(电视剧)', name])
    sources.extend((('zh_wikipedia', 'https://zh.wikipedia.org/wiki/' + urllib.parse.quote(t)) for t in dict.fromkeys(titles)))
    if alias:
        wiki = next((u for u in alias.get('source_urls', []) if u.startswith('https://zh.wikipedia.org/wiki/')), None)
        if wiki:
            sources = [sources[0], ('zh_wikipedia', wiki)] + [s for s in sources[1:] if s[1] != wiki]
    if selected:
        source = 'zh_wikipedia' if 'wikipedia.org/' in selected else 'baidu_baike'
        sources = [(source, selected)] + ([] if item.get('ExplicitChineseSource') else [s for s in sources if s[1] != selected])
    return (item, sources)

def discovered_sources(browser, item, sources):
    """A bounded Chinese Wikipedia search recovers regional title variants."""
    index = 0
    while index < len(sources):
        source = sources[index]
        index += 1
        yield source
    if item.get('ExplicitChineseSource'):
        return
    page = browser.new_page()
    try:
        query = re.sub('第(\\d+)(?:个|名|位)', '第\\1 ', item['Name']).replace('：', ' ')
        url = 'https://zh.wikipedia.org/w/api.php?' + urllib.parse.urlencode({'action': 'query', 'list': 'search', 'srsearch': query, 'srlimit': 3, 'format': 'json'})
        response = page.goto(url, wait_until='domcontentloaded', timeout=12000)
        if response is None or response.status != 200:
            return
        payload = json.loads(page.locator('body').inner_text())
        for found in payload.get('query', {}).get('search', []):
            candidate = ('zh_wikipedia', 'https://zh.wikipedia.org/wiki/' + urllib.parse.quote(found['title']))
            if candidate not in sources:
                yield candidate
    except Exception:
        return
    finally:
        try:
            page.close()
        except Exception:
            pass

def fetch_profile(item, config):
    """Read public pages without credentials; refuse unavailable/wrong versions."""
    from playwright.sync_api import sync_playwright
    from chinese_identity import enrich_source_identity
    item, exact_wiki = enrich_source_identity(item, config)
    item, sources = source_candidates(item, config)
    if exact_wiki:
        sources = [('zh_wikipedia', exact_wiki)] + [s for s in sources if s[1] != exact_wiki]
    attempts = []
    with sync_playwright() as p:
        from personal_cinema.runtime_paths import launch_metadata_browser
        browser = launch_metadata_browser(p)
        try:
            if item.get('SourceRole') == 'special' and (not exact_wiki):
                from pokepedia_credits import fetch_profile as fetch_pokepedia
                special = fetch_pokepedia(browser, item, config)
                if special:
                    return (special, attempts)
                attempts.append({'source': '52poke', 'status': 'unavailable-or-unconfirmed'})
            for source, url in discovered_sources(browser, item, sources):
                page = browser.new_page()
                page.route('**/*', lambda route: route.abort() if route.request.resource_type in ('image', 'font', 'media') else route.continue_())
                try:
                    response = page.goto(url, wait_until='domcontentloaded', timeout=15000)
                    if response is None or response.status != 200:
                        attempts.append({'source': source, 'status': 'unavailable', 'http_status': response.status if response else None})
                        continue
                    try:
                        page.wait_for_function('document.body && document.body.innerText.length > 500', timeout=3000)
                    except Exception:
                        pass
                    if source == 'baidu_baike':
                        try:
                            page.wait_for_function('document.title !== "百度百科" && document.querySelector("meta[name=description]")?.content?.length > 30', timeout=5000)
                        except Exception:
                            pass
                    content = page.content()
                    doc = Document(content)
                    if not identity_matches(item, doc):
                        if source == 'zh_wikipedia' and (not item.get('ExplicitChineseSource')):
                            from chinese_credits import norm
                            wanted = norm(item['Name'])
                            for link in doc.root.find('a'):
                                href = link.attrs.get('href', '')
                                label = urllib.parse.unquote(href)
                                if not href.startswith('/wiki/') or not re.search('电影|電影', label):
                                    continue
                                base = re.sub('[_\\s]*[（(][^）)]*[）)]$', '', label.split('/wiki/', 1)[-1])
                                if norm(base) != wanted:
                                    continue
                                discovered = ('zh_wikipedia', 'https://zh.wikipedia.org' + href)
                                if discovered not in sources and len(sources) < 8:
                                    sources.append(discovered)
                        elif source == 'baidu_baike' and (not item.get('ExplicitChineseSource')):
                            from chinese_credits import norm
                            wanted = {norm(item['Name']), *(norm(n) for n in item.get('RecognitionAliases', []))}
                            for link in doc.root.find('a'):
                                href = link.attrs.get('href', '')
                                candidate = urllib.parse.urlsplit(urllib.parse.urljoin('https://baike.baidu.com', href))
                                if candidate.hostname != 'baike.baidu.com' or not candidate.path.startswith('/item/'):
                                    continue
                                label = urllib.parse.unquote(candidate.path.split('/item/', 1)[1].split('/')[0])
                                if norm(label) not in wanted:
                                    continue
                                discovered = ('baidu_baike', urllib.parse.urlunsplit(candidate._replace(query='', fragment='')))
                                if discovered not in sources and len(sources) < 12:
                                    sources.append(discovered)
                        attempts.append({'source': source, 'status': 'version-not-confirmed', 'title': doc.title()[:160]})
                        continue
                    actors = parse_credits(content, config.get('max_cast', 60), all_cast_tables=item['Type'] == 'Movie')
                    if len(actors) < 3 and source == 'baidu_baike':
                        try:
                            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                        except Exception:
                            pass
                        try:
                            page.wait_for_function('document.querySelectorAll("[class*=actorItem_]").length >= 3', timeout=3500)
                        except Exception:
                            pass
                        content = page.content()
                        doc = Document(content)
                        if not identity_matches(item, doc):
                            continue
                        actors = parse_credits(content, config.get('max_cast', 60), all_cast_tables=item['Type'] == 'Movie')
                    if len(actors) < 3:
                        attempts.append({'source': source, 'status': 'cast-not-readable'})
                        continue
                    missing_roles = 0
                    for actor in actors:
                        if not re.search('[㐀-鿿]', actor.get('Role', '')):
                            actor['OriginalRole'] = actor.get('Role', '')
                            actor['Role'] = '演员（角色名暂无中文资料）'
                            actor['RoleStatus'] = 'translation_unavailable'
                            missing_roles += 1
                    profile = {'type': item['Type'], 'title': item['Name'], 'year': item['ProductionYear'], 'provider_ids': item['ProviderIds'], 'source': source, 'source_url': urllib.parse.urlunsplit(urllib.parse.urlsplit(page.url)._replace(query='', fragment='')), 'source_title': doc.title(), 'actors': actors, 'managed': True, 'checked_at': time.time()}
                    if missing_roles:
                        profile['source_coverage'] = {'untranslated_role_count': missing_roles}
                    if any((a.get('RoleStatus') == 'not_published' for a in actors)):
                        profile['cast_scope'] = 'starring'
                    crew = parse_crew(content)
                    if crew:
                        profile['crew'] = crew
                    cache = config.get('page_cache_directory') or Path(config.get('library_root', ROOT)) / 'data/chinese-source-pages'
                    cached = Path(cache) / (item['Type'] + '-' + item['ProviderIds']['Tmdb'] + '.html')
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_text(content, 'utf-8')
                    profile['source_cache_file'] = str(cached)
                    return (profile, attempts)
                except Exception as error:
                    attempts.append({'source': source, 'status': 'unavailable', 'error_type': type(error).__name__})
                finally:
                    page.close()
            if item.get('SourceRole') == 'special' and exact_wiki:
                from pokepedia_credits import fetch_profile as fetch_pokepedia
                special = fetch_pokepedia(browser, item, config)
                if special:
                    return (special, attempts)
        finally:
            browser.close()
    return (None, attempts)
from library_tasks import observe

class Worker:

    def __init__(self, root=ROOT, client=None):
        self.root = root
        self.client = client or Jellyfin(root)
        self.config = read_json(root / 'data/chinese-credits-config.json', {})
        self.state = read_json(root / 'data/chinese-credits-state.json', {})
        self.profiles = read_json(root / 'data/chinese-credits-profiles.json', {})

    def cycle(self, fetch=True, max_items=None):
        observe(self.root, 'credits', 'credits', '检查中文演职人员资料')
        self.config = read_json(self.root / 'data/chinese-credits-config.json', self.config)
        if max_items is not None:
            self.config['max_items_per_cycle'] = max_items
        self.config['recognition_aliases'] = read_json(self.root / 'data/recognition-aliases.json', [])
        self.config['library_root'] = str(self.root)
        manual_sources = read_json(self.root / 'data/manual-metadata-overrides.json', {}).get('works', {})
        self.profiles = read_json(self.root / 'data/chinese-credits-profiles.json', self.profiles)
        entries = read_json(self.root / 'data/cache-state.json', {})
        source_entries = {e['id'][:12]: e for e in entries.values() if e.get('id') and (not e.get('source_missing'))}
        if not self.config.get('enabled'):
            return
        items = self.client.json('/Items?Recursive=true&IncludeItemTypes=Movie,Series&Fields=Path,ProviderIds')['Items']
        fetched = 0
        for brief in items:
            if getattr(self, 'target_ids', None) is not None and brief['Id'] not in self.target_ids:
                continue
            ident = (brief.get('ProviderIds') or {}).get('Tmdb')
            key = brief['Type'] + ':' + ident if ident else next((k for k, p in self.profiles.items() if p.get('catalog_relative') and Path(brief.get('Path', '')).absolute() == (self.root / 'catalog' / p['catalog_relative']).absolute()), None)
            if not key:
                continue
            prior = self.state.get(brief['Id'], {})
            alias_config = [a for a in self.config.get('recognition_aliases', []) if a.get('kind') == brief['Type'] and str(a.get('provider_ids', {}).get('Tmdb')) == str(ident)]
            source_signature = __import__('hashlib').sha256(json.dumps(['credits-parser-2026-11-09.2', self.config.get('source_urls', {}).get(key), manual_sources.get(key, {}).get('reference_url'), self.config.get('source_titles', {}).get(key), alias_config], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            explicit_source_changed = bool(manual_sources.get(key, {}).get('reference_url') and prior.get('source_signature') != source_signature)
            if prior.get('source_signature') != source_signature or getattr(self, 'force_retry', False):
                prior = {**prior, 'next_attempt_at': 0}
            prior = {**prior, 'source_signature': source_signature}
            try:
                item = self.client.json('/Items/' + brief['Id'])
                Path(item['Path']).absolute().relative_to((self.root / 'catalog').absolute())
                short = re.search('\\[([a-f0-9]{12})\\]', Path(item['Path']).stem)
                source = source_entries.get(short[1]) if short else None
                if source and item['Type'] == 'Movie':
                    from smart_metadata import derive_hints
                    from franchise_context import context
                    item = {**item, 'RecognitionAliases': derive_hints([source['remote']], kind='Movie')['titles'], 'SourceRole': source.get('media_role') or context(source['remote']).get('role')}
                profile = self.profiles.get(key)
                if profile and (profile['year'] != item.get('ProductionYear') or profile['type'] != item['Type']):
                    profile = None
                if profile and ident and fetch and (fetched < self.config.get('max_items_per_cycle', 1)) and (explicit_source_changed or time.time() - profile.get('checked_at', 0) > self.config.get('refresh_seconds', 604800)) and (prior.get('next_attempt_at', 0) <= time.time()):
                    fetched += 1
                    observe(self.root, 'credits', 'credits', item['Name'], item_id=item['Id'])
                    refreshed, attempts = fetch_profile(item, self.config)
                    if refreshed:
                        profile = refreshed
                        self.profiles[key] = profile
                        write_json(self.root / 'data/chinese-credits-profiles.json', self.profiles)
                    else:
                        prior = {**prior, 'refresh_attempts': attempts, 'next_attempt_at': time.time() + self.config.get('retry_seconds', 86400)}
                if not profile:
                    if not fetch or fetched >= self.config.get('max_items_per_cycle', 1) or prior.get('next_attempt_at', 0) > time.time():
                        continue
                    fetched += 1
                    observe(self.root, 'credits', 'credits', item['Name'], item_id=item['Id'])
                    profile, attempts = fetch_profile(item, self.config)
                    if not profile:
                        self.state[item['Id']] = {'status': 'needs-review', 'name': item['Name'], 'attempts': attempts, 'reason': '中文资料暂不可读取或作品版本不符；保留现有名单', 'next_attempt_at': time.time() + self.config.get('retry_seconds', 86400), 'source_signature': source_signature, 'updated_at': time.time()}
                        write_json(self.root / 'data/chinese-credits-state.json', self.state)
                        continue
                    self.profiles[key] = profile
                    write_json(self.root / 'data/chinese-credits-profiles.json', self.profiles)
                record = apply_profile(self.root, self.client, item, profile, prior)
                self.state[item['Id']] = {**prior, **record, 'name': item['Name'], 'updated_at': time.time()}
            except (OSError, ValueError, KeyError, AssertionError, ET.ParseError) as error:
                self.state[brief['Id']] = {**prior, 'status': 'retry', 'name': brief.get('Name'), 'error_type': type(error).__name__, 'updated_at': time.time()}
            write_json(self.root / 'data/chinese-credits-state.json', self.state)
        write_json(self.root / 'data/chinese-credits-state.json', self.state)
        observe(self.root, 'credits', 'credits', '本轮中文资料检查完成', 'idle', count=fetched)
        self.apply_seasons(items)
        write_json(self.root / 'data/chinese-credits-status.json', {'updated_at': time.time(), 'source_priority': self.config.get('source_priority', []), 'items': self.state})

    def apply_seasons(self, items):
        profiles = read_json(self.root / 'data/chinese-season-profiles.json', {})
        for brief in items:
            if getattr(self, 'target_ids', None) is not None and brief['Id'] not in self.target_ids:
                continue
            if brief.get('Type') != 'Series':
                continue
            seasons = self.client.json('/Shows/' + brief['Id'] + '/Seasons?Fields=Path')['Items']
            regular = [s for s in seasons if s.get('IndexNumber', 0) > 0 and s.get('Path') and Path(s['Path']).is_dir()]
            tmdb = brief.get('ProviderIds', {}).get('Tmdb')
            parent = self.profiles.get('Series:' + str(tmdb))
            for season in regular:
                relative = Path(season['Path']).absolute().relative_to((self.root / 'catalog').absolute()).as_posix()
                profile = profiles.get(relative)
                if not profile and parent and (len(regular) == 1) and (season.get('IndexNumber') == 1) and (tmdb not in ('37854', '46260', '31910', '34307')):
                    item = self.client.json('/Items/' + season['Id'])
                    if item.get('ProductionYear') not in (None, parent['year']):
                        continue
                    profile = {**parent, 'type': 'Season', 'year': item.get('ProductionYear'), 'managed': True, 'catalog_relative': relative, 'parent_key': 'Series:' + str(tmdb)}
                    profiles[relative] = profile
                if not profile:
                    continue
                try:
                    item = self.client.json('/Items/' + season['Id'])
                    record = apply_profile(self.root, self.client, item, profile, self.state.get(item['Id'], {}))
                    self.state[item['Id']] = {**record, 'name': brief['Name'] + ' ' + item['Name'], 'updated_at': time.time()}
                except (OSError, ValueError, KeyError, AssertionError, ET.ParseError) as error:
                    self.state[season['Id']] = {'status': 'retry', 'error_type': type(error).__name__, 'updated_at': time.time()}
        write_json(self.root / 'data/chinese-season-profiles.json', profiles)
        write_json(self.root / 'data/chinese-credits-state.json', self.state)

    def update_collections(self, items):
        films = [i for i in items if i.get('Type') == 'Movie' and i.get('Name', '').startswith('海贼王剧场版')]
        if not films:
            return
        sets = self.client.json('/Items?Recursive=true&IncludeItemTypes=BoxSet')['Items']
        collection = next((s for s in sets if s['Name'] == '海贼王剧场版'), None)
        ids = ','.join((i['Id'] for i in films))
        if collection:
            existing = {i['Id'] for i in self.client.json('/Items?ParentId=' + collection['Id'])['Items']}
            missing = ','.join((i['Id'] for i in films if i['Id'] not in existing))
            if missing:
                self.client.json('/Collections/' + collection['Id'] + '/Items?ids=' + missing, {}, method='POST')
        else:
            self.client.json('/Collections?' + urllib.parse.urlencode({'name': '海贼王剧场版', 'ids': ids, 'isLocked': 'true'}), {}, method='POST')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['run', 'profiles-once'])
    args = parser.parse_args()
    import msvcrt
    lock = (ROOT / 'data/chinese-credits.lock').open('a+b')
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    worker = Worker()
    from season_artwork import ArtworkWorker
    from episode_artwork import refresh_resume_artwork, refresh_new_episode_artwork
    from season_ratings import RatingWorker
    artwork = ArtworkWorker(ROOT, worker.client)
    ratings = RatingWorker(ROOT, worker.client)
    while True:
        try:
            if args.command == 'run':
                observe(ROOT, 'artwork', 'poster', '检查续播与新剧集封面')
                refresh_resume_artwork(ROOT, worker.client)
                refresh_new_episode_artwork(ROOT, worker.client)
                artwork.cycle()
                observe(ROOT, 'artwork', 'poster', '本轮封面检查完成', 'idle')
                ratings.cycle()
            worker.cycle(fetch=args.command == 'run')
            if args.command == 'run':
                worker.update_collections(worker.client.json('/Items?Recursive=true&IncludeItemTypes=Movie')['Items'])
        except Exception as error:
            observe(ROOT, 'credits', 'credits', '中文资料服务暂不可用，将自动重试', 'error', retry_at=time.time() + worker.config.get('poll_seconds', 120))
            logging.warning('Chinese credits cycle deferred: %s', type(error).__name__)
        if args.command == 'profiles-once':
            break
        time.sleep(worker.config.get('poll_seconds', 120))
if __name__ == '__main__':
    main()
