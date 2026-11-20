"""Chinese Pokémon short credits from that short's own character membership."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from personal_cinema.runtime_paths import RuntimePath as Path
from chinese_credits import Document, clean, norm, names, without_parentheses
from library_tasks import read

def title_key(value):
    value = clean(value)
    value = re.sub('^(?:精灵宝可梦|宝可梦|神奇宝贝|宠物小精灵)(?:剧场版)?[：:\\s]*', '', value)
    return norm(value).replace('pokémon', '').replace('pokemon', '')

def article_identity(item, content):
    doc = Document(content)
    description = doc.description()
    title = clean(doc.title())
    if item.get('SourceRole') != 'special' or not re.search('特别篇|特別篇|序章|序幕|短篇|短片', title + ' ' + description):
        return False
    if str(item.get('ProductionYear')) not in re.findall('(?:19|20)\\d{2}', description):
        return False
    available = [re.split('\\s+-\\s*神奇', title)[0], re.split('[（(]', description.strip(' 《》「」'))[0]]
    for table in doc.tables():
        if not table or not any(('名字' in cell for cell in table[0])):
            continue
        for row in table[1:]:
            if any((c in ('中文', '英文', '日文') for c in row[:2])):
                available.extend(row[2:])
    expected = {title_key(t) for t in [item['Name'], *item.get('RecognitionAliases', [])] if len(title_key(t)) >= 4}
    return bool(expected.intersection((title_key(t) for t in available if t)))

def characters(content):
    doc = Document(content)
    active = False
    result = []
    seen = set()
    for node in doc.root.find():
        if node.tag == 'h2':
            active = bool(re.search('登场角色|出场角色|角色', clean(node.text())))
        if not active or node.tag != 'li' or node.find('li'):
            continue
        text = clean(node.text())
        if re.search('幻想|回忆|照片|仅提及|传说中', text):
            continue
        links = [link for link in node.find('a') if link.attrs.get('href', '').startswith('/wiki/')]
        specific = [link for link in links if re.search('电影系列|动画|动画首作|特别篇|电影的|动画的', urllib.parse.unquote(link.attrs.get('href', '')) + ' ' + clean(link.text()))]
        chosen = specific[-1] if specific else links[0] if links else None
        if not chosen:
            continue
        title = urllib.parse.unquote(chosen.attrs['href'].split('/wiki/', 1)[1]).replace('_', ' ')
        if title in seen:
            continue
        seen.add(title)
        role = re.split('[（(]', text)[0].strip()
        if role:
            result.append({'title': title, 'role': role, 'membership': text})
    return result

def japanese_voice(content, role):
    doc = Document(content)
    values = []
    for table in doc.tables():
        if not table:
            continue
        header = table[0]
        japanese = next((i for i, c in enumerate(header) if c in ('日文', '日语', '日本')), None)
        if japanese is not None and any(('配音' in cell for cell in header)):
            for row in table[1:]:
                if len(row) <= japanese or '{{{' in row[0]:
                    continue
                if title_key(row[0]) != title_key(role):
                    continue
                values.extend(names(row[japanese]))
                break
        if any(('语言' in c for c in header)) and any(('配音' in c for c in header)):
            for row in table[1:]:
                if row and row[0] in ('日文', '日语', '日本') and (len(row) > 1):
                    values.extend(names(row[-1]))
                    break
    values = [without_parentheses(value).strip(' *※') for value in values if not re.search('→|～|~|\\d{4}|{{{', value)]
    values = list(dict.fromkeys((v for v in values if v and len(v) < 35 and re.search('[\u3040-鿿]', v))))
    return values[0] if len(values) == 1 else None

def actor_key(value):
    return norm(value).translate(str.maketrans({'恵': '惠', '髙': '高', '﨑': '崎'}))

def credited_cast(content, ident):
    doc = Document(content)
    canonical = next((n.attrs.get('href', '') for n in doc.root.find('link') if n.attrs.get('rel') == 'canonical'), '')
    if not re.fullmatch('https://www\\.themoviedb\\.org/movie/' + re.escape(str(ident)) + '(?:-[^/?#]*)?/cast/?', canonical):
        raise ValueError('Short credits identity mismatch')
    groups = [n for n in doc.root.find('ol') if {'people', 'credits'}.issubset(n.attrs.get('class', '').split())]
    result = []
    for row in groups[0].find('li') if groups else []:
        paragraphs = row.find('p')
        if not paragraphs:
            continue
        name = clean(paragraphs[0].text())
        role = clean(paragraphs[1].text()) if len(paragraphs) > 1 else ''
        if name:
            person = next((re.search('/person/(\\d+)(?:[-/?#]|$)', a.attrs.get('href', '')) for a in row.find('a') if re.search('/person/(\\d+)(?:[-/?#]|$)', a.attrs.get('href', ''))), None)
            result.append({'Name': name, 'original_role': role, **({'person_id': person[1]} if person else {})})
    return result

def fetch_profile(browser, item, config):
    if item.get('SourceRole') != 'special':
        return None
    from franchise_context import POKEMON_SPECIALS
    ident = str(item.get('ProviderIds', {}).get('Tmdb', ''))
    known = next((p for p in POKEMON_SPECIALS if p['provider_ids']['Tmdb'] == ident), None)
    if known:
        item = {**item, 'RecognitionAliases': list(dict.fromkeys(item.get('RecognitionAliases', []) + known.get('aliases', []) + [known['title']]))}
    if not known and (not any((re.search('宝可梦|神奇宝贝|精灵宝可梦', t) for t in [item['Name'], *item.get('RecognitionAliases', [])]))):
        return None
    root = Path(config.get('library_root', _cinema_path('')))
    cache = Path(config.get('pokepedia_cache_directory', root / 'data/pokepedia-source-pages'))
    cache.mkdir(parents=True, exist_ok=True)
    page = browser.new_page()
    page.route('**/*', lambda route: route.abort() if route.request.resource_type in ('image', 'font', 'media') else route.continue_())

    def api(payload):
        url = 'https://wiki.52poke.com/api.php?' + urllib.parse.urlencode({**payload, 'format': 'json'})
        response = page.goto(url, wait_until='domcontentloaded', timeout=12000)
        if response is None or response.status != 200:
            return {}
        return json.loads(page.locator('body').inner_text())

    def article(title):
        path = cache / (hashlib.sha256(title.encode()).hexdigest() + '.html')
        if path.exists() and time.time() - path.stat().st_mtime < 604800:
            return path.read_text('utf-8')
        response = page.goto('https://wiki.52poke.com/zh-hans/' + urllib.parse.quote(title), wait_until='domcontentloaded', timeout=12000)
        if response is None or response.status != 200:
            return None
        result = page.content()
        path.write_text(result, 'utf-8')
        return result
    try:
        cast_url = 'https://www.themoviedb.org/movie/' + ident + '/cast?language=ja-JP'
        cast_page = cache / ('short-cast-' + ident + '.html')
        if not cast_page.exists():
            with urllib.request.urlopen(cast_url, timeout=15) as response:
                cast_page.write_bytes(response.read(7 * 1024 * 1024))
        original_cast = credited_cast(cast_page.read_text('utf-8'), ident)
        chinese_cast_page = cache / ('short-cast-zh-' + ident + '.html')
        if not chinese_cast_page.exists():
            with urllib.request.urlopen(cast_url.replace('ja-JP', 'zh-CN'), timeout=15) as response:
                chinese_cast_page.write_bytes(response.read(7 * 1024 * 1024))
        chinese_cast = credited_cast(chinese_cast_page.read_text('utf-8'), ident)
        by_person = {p['person_id']: p for p in chinese_cast if p.get('person_id')}
        actual_cast = [{**p, 'Name': by_person.get(p.get('person_id'), p)['Name']} for p in original_cast]
        actual = {actor_key(p['Name']): p for p in actual_cast}
        for original, preferred in zip(original_cast, actual_cast):
            actual[actor_key(original['Name'])] = preferred
        names_to_try = list(dict.fromkeys([re.sub('^(?:精灵宝可梦|宝可梦|神奇宝贝)(?:剧场版)?[：:\\s]*', '', t) for t in [item['Name'], *item.get('RecognitionAliases', [])] if len(t) < 55 and (not re.search('\\[|DBD|外挂字幕|剧场OVA', t))]))
        candidates = names_to_try[:2]
        queries = [next((t for t in names_to_try if not re.search('[㐀-鿿]', t)), names_to_try[0]), names_to_try[0]]
        for query in dict.fromkeys(queries):
            data = api({'action': 'query', 'list': 'search', 'srsearch': query, 'srlimit': 4})
            candidates.extend((v['title'] for v in data.get('query', {}).get('search', [])))
        for title in dict.fromkeys(candidates):
            raw = article(title)
            if not raw or not article_identity(item, raw):
                continue
            members = characters(raw)
            actors = []
            sources = []
            for member in members[:12]:
                specific = article(member['title'])
                voice = japanese_voice(specific, member['role']) if specific else None
                if not voice or actor_key(voice) not in actual:
                    continue
                displayed = actual[actor_key(voice)]['Name']
                actor = next((a for a in actors if a['Name'] == displayed), None)
                if actor:
                    actor['Role'] += ' / ' + member['role'] + '（日语配音）'
                else:
                    actors.append({'Name': displayed, 'Type': 'Actor', 'Role': member['role'] + '（日语配音）'})
                sources.append({'role': member['role'], 'actor': voice, 'source_url': 'https://wiki.52poke.com/zh-hans/' + urllib.parse.quote(member['title'])})
            references = read(Path(config.get('reference_profiles_path', root / 'data/chinese-credits-profiles.json')))
            from franchise_context import POKEMON
            film_ids = {row[0] for records in POKEMON.values() for row in records}
            for member in members:
                for key, reference in references.items():
                    reference = reference.get('profile') if isinstance(reference, dict) and 'profile' in reference else reference
                    if not reference or key.split(':')[-1] not in film_ids or reference.get('year') != item['ProductionYear']:
                        continue
                    for person in reference.get('actors', []):
                        if actor_key(person['Name']) not in actual:
                            continue
                        for role in person.get('Role', '').split(' / '):
                            chinese_role = without_parentheses(role).strip()
                            if len(chinese_role) < 2 or norm(chinese_role) not in norm(member['membership']):
                                continue
                            actor = next((a for a in actors if actor_key(a['Name']) == actor_key(person['Name'])), None)
                            if actor:
                                if role not in actor['Role']:
                                    actor['Role'] += ' / ' + role
                            else:
                                actors.append({'Name': actual[actor_key(person['Name'])]['Name'], 'Type': 'Actor', 'Role': role})
                            sources.append({'role': role, 'actor': person['Name'], 'source_url': reference['source_url']})
            if not actors:
                continue
            for person in actual_cast:
                if any((actor_key(a['Name']) == actor_key(person['Name']) for a in actors)):
                    continue
                actors.append({'Name': person['Name'], 'Type': 'Actor', 'Role': '旁白（日语配音）' if re.search('narrat', person['original_role'], re.I) else '日语配音（角色资料待补充）'})
            return {'type': item['Type'], 'title': item['Name'], 'year': item['ProductionYear'], 'provider_ids': item['ProviderIds'], 'source': '52poke', 'source_url': 'https://wiki.52poke.com/zh-hans/' + urllib.parse.quote(title), 'source_title': Document(raw).title(), 'actors': actors, 'managed': True, 'source_role': 'special', 'cast_scope': 'this-short-characters', 'character_sources': sources, 'actual_cast_source': cast_url, 'checked_at': time.time()}
    except Exception as error:
        logging.warning('Pokepedia credits lookup deferred (%s)', type(error).__name__)
        return None
    finally:
        page.close()
    return None
