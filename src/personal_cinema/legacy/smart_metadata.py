"""Conservative title extraction, candidate scoring and local metadata storage."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import copy
import difflib
import html
import os
import re
import secrets
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from catalog_intelligence import simplified
GENERIC = {'夸克', '百度', '百度网盘', 'baidu', 'movie', 'movies', '电影', '影视', '电视剧', '剧集', 'tv', 'shows', 'videos', '视频', '网盘', '下载', '分享', '来自分享', '4k', '1080p', '2160p', '资源'}
QUALITY = re.compile('(?i)(?<![a-z])(?:2160p|1080p|1080i|720p|480p|4k|8k|uhd|web[ ._-]?(?:dl|rip)|blu[ ._-]?ray|bd[ ._-]?(?:rip|remux)|remux|hdtv|hdr(?:10\\+?)?|sdr|hevc|avc|x[ ._-]?26[45]|h[ ._-]?26[45]|itunes|netflix|amzn|nf(?=\\s)|ddp?\\d|dts|aac\\d?|atmos|dolby)')
YEAR = re.compile('(?<!\\d)((?:19|20)\\d{2})(?!\\d)')
IDS = {'Tmdb': re.compile('(?i)(?<![a-z])tmdb(?:id)?[\\s:=_-]*(\\d+)'), 'Imdb': re.compile('(?i)\\b(tt\\d{7,10})\\b')}

def _quantity_number(match):
    digits = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    value = match[0]
    units = {'十': 10, '百': 100, '千': 1000}
    total = 0
    number = 0
    if not any((c in units for c in value)):
        return ''.join((str(digits[c]) for c in value))
    for char in value:
        if char in units:
            total += (number or 1) * units[char]
            number = 0
        else:
            number = digits[char]
    return str(total + number)

def normalize_title(value):
    value = unicodedata.normalize('NFKC', simplified(value or '')).casefold()
    value = re.sub('[零〇一二两三四五六七八九十百千]+(?=厘米|毫米|千米|公里|分钟|小时|岁|號|号)', _quantity_number, value)
    value = re.sub('第(\\d+)(?:个|位|名)(?=[\\u3400-\\u9fff])', '第\\1', value)
    ordinals = {'first': '1', 'second': '2', 'third': '3', 'fourth': '4', 'fifth': '5', 'sixth': '6', 'seventh': '7', 'eighth': '8', 'ninth': '9', 'tenth': '10', 'eleventh': '11', 'twelfth': '12'}
    value = re.sub('(?<![a-z])(?:' + '|'.join(ordinals) + ')(?![a-z])', lambda m: ordinals[m[0]], value)
    romans = {'ii': '2', 'iii': '3', 'iv': '4', 'v': '5', 'vi': '6', 'vii': '7', 'viii': '8', 'ix': '9', 'x': '10'}
    value = re.sub('(?<![a-z])(?:viii|vii|iii|vi|iv|ii|ix|v|x)(?![a-z])', lambda match: romans[match[0]], value)
    return ''.join((char for char in value if char.isalnum()))

def _label(value, kind=None):
    text = unicodedata.normalize('NFKC', html.unescape(value))
    text = re.sub('(?<=[\\u3400-\\u9fff])[丨│┃|]+(?=[\\u3400-\\u9fff])', '', text)
    text = re.sub('^(名侦探柯南|名偵探柯南)[ ._-]+(?:(?:剧场版|劇場版|Movie)\\s*)?\\d{1,2}[ ._-]+(?=\\S)', '\\1：', text, flags=re.I)
    text = re.sub('(?i)(Detective[ ._-]+Conan)[ ._-]+Movie[ ._-]*\\d{1,2}[ ._-]+', '\\1：', text)
    text = re.sub('(剧场|劇場)(?:版)?\\s*\\d{1,2}\\s*[:：._\\-]', '\\1版：', text)
    text = re.sub('(?i)\\.(?:mkv|mp4|avi|mov|m4v|ts|m2ts|webm|strm)$', '', text)
    for pattern in IDS.values():
        text = pattern.sub('', text)
    text = re.sub('(?i)^\\s*\\[(?:LIBVIO|FRDS|Baha|VCB-Studio|动漫国|喵萌奶茶屋)\\]\\s*', '', text)
    text = re.sub('[\\[【](?:[^\\]】]*(?:中字|中英|双语|简繁|国语|全集|完结|公众号|资源分享)[^\\]】]*)[\\]】]', ' ', text)
    text = re.sub('[\\[【](?:[^\\]】]*(?:内封|內封|内嵌|內嵌|外挂|外掛|字幕)[^\\]】]*)[\\]】]', ' ', text)
    text = re.split('\\s+(?:蓝光原盘|藍光原盤|蓝光原碟|藍光原碟|蓝光无损|藍光無損|原盘提取|原盤提取)', text, maxsplit=1)[0]
    text = text.replace('.', ' ').replace('_', ' ')
    quality = QUALITY.search(text)
    quality_found = quality is not None
    if quality:
        text = text[:quality.start()]
    episode = re.search('(?i)S\\d{1,2}E\\d{1,3}', text)
    if kind == 'Series':
        episode = re.search('(?i)(?<![A-Za-z0-9])(?:S\\d{1,2}\\s*E\\d{1,4}|E(?:P(?:ISODE)?)?\\s*\\d{1,4})(?=$|[\\s\\[\\]()_-])|第[零〇一二三四五六七八九十百千\\d]+[集话話]', text) or episode
    if episode:
        text = text[:episode.start()]
    bracket_years = list(re.finditer('[\\[(]((?:19|20)\\d{2})[\\])]', text))
    years = list(YEAR.finditer(text))
    selected = bracket_years[-1] if bracket_years else years[-1] if years else None
    year = None
    if selected:
        prefix = text[:selected.start()].strip(' -[]()')
        trailing = text[selected.end():].strip(' -[]()')
        if prefix or (not trailing and quality_found and (int(selected[1]) >= 2000)):
            year = int(selected[1])
            text = prefix
    text = re.sub('\\[\\s*\\]|\\(\\s*\\)', ' ', text)
    text = re.sub('(?i)[ ._-]+S\\d{1,2}\\s*$', '', text)
    if kind == 'Series':
        text = re.sub('(?i)[ ._-]*(?:第\\s*[零〇一二两三四五六七八九十百\\d]+\\s*季|Season\\s*\\d{1,2})\\s*$', '', text)
    text = re.sub('\\s+', ' ', text).strip(' -[]()')
    normalized = normalize_title(text)
    if not normalized or normalized in {normalize_title(value) for value in GENERIC} or re.fullmatch('(?i)(?:season\\s*\\d+|s\\d+|第[一二三四五六七八九十\\d]+季|\\d{1,4}\\s*[-~至]\\s*\\d{1,4})', text):
        text = ''
    return (text, year)

def derive_hints(paths, kind='Movie'):
    titles, file_titles, named_years, bare_years, folder_years, conflicts = ([], [], [], [], [], [])
    providers = {}
    numbered_movies = []
    for source in paths:
        parts = list(PurePosixPath(source.replace('\\', '/')).parts)
        labels = list(reversed(parts[:-1])) + parts[-1:]
        for raw in labels:
            if kind == 'Movie':
                marked = re.search('^(海贼王|航海王|ONE[ ._-]*PIECE)\\s*(?:剧场版|劇場版|电影版|電影版|movies?|films?)\\s*(?:第)?\\s*0*(\\d{1,2})(?:部)?\\s*[:：._\\- ]', unicodedata.normalize('NFKC', raw), re.I)
                if marked:
                    value = {'family': '海贼王', 'number': int(marked[2]), 'label': _label(raw)[0]}
                    if value not in numbered_movies:
                        numbered_movies.append(value)
            for provider, pattern in IDS.items():
                for match in pattern.finditer(raw):
                    ident = match[1].lower()
                    if provider in providers and providers[provider] != ident:
                        conflicts.append('Conflicting ' + provider + ' identifiers')
                    providers[provider] = ident
            title, year = _label(raw, kind)
            if kind == 'Series' and raw == parts[-1] and title.isdecimal():
                continue
            if title:
                if title not in titles:
                    titles.append(title)
                if raw == parts[-1]:
                    file_titles.append(title)
                if year:
                    named_years.append(year)
                    if raw != parts[-1]:
                        folder_years.append(year)
            elif year:
                bare_years.append(year)
    years = set((folder_years if kind == 'Series' and folder_years else named_years) or bare_years)
    if len(years) > 1:
        conflicts.append('Folder and file release years disagree')
    if any((not title.isdecimal() for title in titles)):
        titles = [title for title in titles if not (title.isdecimal() and len(title) == 4 and (int(title) >= 2000))]
    aliases = []
    for title in titles:
        if re.search('[\\u3400-\\u9fff]', title) and re.search('[a-zA-Z]{3}', title):
            segments = re.findall('[\\u3400-\\u9fff\\u3040-\\u30ff\\d][\\u3400-\\u9fff\\u3040-\\u30ff\\d\\s：:·-]*|[A-Za-z][A-Za-z\\d\\s:：\\x27’!?-]*', title)
            for segment in segments:
                segment = segment.strip(' -:：')
                if segment != title and normalize_title(segment) and (not segment.isdecimal()):
                    aliases.append(segment)
    required_numbers = sorted({number for title in file_titles if title in titles for number in re.findall('\\d+', normalize_title(title))})
    return {'titles': (titles + [value for value in aliases if value not in titles])[:6], 'year': next(iter(years)) if len(years) == 1 else None, 'provider_ids': providers, 'conflicts': sorted(set(conflicts)), 'kind': kind, 'required_numbers': required_numbers, 'numbered_movies': numbered_movies}

def _candidate_groups(results):
    groups = []
    for raw in results:
        ids = {key: str(value) for key, value in (raw.get('ProviderIds') or {}).items() if value}
        if not ids:
            continue
        names = {raw.get('Name'), raw.get('OriginalTitle'), raw.get('OriginalName')}
        names.update(raw.get('_official_aliases') or [])
        names.discard(None)
        group = {'ids': ids, 'names': names, 'year': raw.get('ProductionYear'), 'records': [raw]}
        groups.append(group)
    changed = True
    while changed:
        changed = False
        for index, left in enumerate(groups):
            for other in range(index + 1, len(groups)):
                right = groups[other]
                shared = any((key in right['ids'] and value == right['ids'][key] for key, value in left['ids'].items()))
                conflicting = any((key in right['ids'] and value != right['ids'][key] for key, value in left['ids'].items()))
                if not conflicting and shared:
                    left['ids'].update(right['ids'])
                    left['names'].update(right['names'])
                    left['records'].extend(right['records'])
                    groups.pop(other)
                    changed = True
                    break
            if changed:
                break
    return groups

def choose_match(hints, results, minimum_score=0.94, minimum_margin=0.08):
    if hints.get('conflicts'):
        return {'status': 'needs_review', 'reason': '; '.join(hints['conflicts']), 'candidates': []}
    ranked = []
    for group in _candidate_groups(results):
        ids = hints.get('provider_ids') or {}
        id_conflict = any((key in group['ids'] and group['ids'][key] != value for key, value in ids.items()))
        explicit = bool(ids) and (not id_conflict) and any((group['ids'].get(key) == value for key, value in ids.items()))
        if ids and (not explicit):
            continue
        if hints.get('year') and group['year'] and (hints['year'] != group['year']) and (not explicit):
            continue
        best = 0.0
        for title in hints['titles']:
            source = normalize_title(title)
            for name in group['names']:
                candidate = normalize_title(name)
                if not source or not candidate:
                    continue
                if hints.get('required_numbers') and set(re.findall('\\d+', candidate)) != set(hints['required_numbers']) and (not explicit):
                    continue
                if set(re.findall('\\d+', source)) != set(re.findall('\\d+', candidate)):
                    continue
                best = max(best, difflib.SequenceMatcher(None, source, candidate).ratio())
        poster = any((record.get('ImageUrl') for record in group['records']))
        overview = any((record.get('Overview') for record in group['records']))
        year_bonus = 0.06 if hints.get('year') and group['year'] == hints['year'] else 0.04
        score = 1.0 if explicit else min(1.0, best * 0.9 + year_bonus + 0.02 * poster + 0.02 * overview)
        body = copy.deepcopy(next((record for record in group['records'] if record.get('_language') == 'zh-CN' and 'Tmdb' in (record.get('ProviderIds') or {})), group['records'][0]))
        body = {key: value for key, value in body.items() if not key.startswith('_')}
        body['ProviderIds'] = group['ids']
        ranked.append({'score': round(score, 4), 'title_similarity': round(best, 4), 'candidate': body})
    ranked.sort(key=lambda value: value['score'], reverse=True)
    summary = [{'name': value['candidate'].get('Name'), 'year': value['candidate'].get('ProductionYear'), 'provider_ids': value['candidate'].get('ProviderIds'), 'score': value['score']} for value in ranked[:5]]
    if not ranked or ranked[0]['score'] < minimum_score:
        return {'status': 'needs_review', 'reason': 'No sufficiently close title/year match', 'candidates': summary}
    if len(ranked) > 1 and ranked[0]['score'] - ranked[1]['score'] < minimum_margin:
        return {'status': 'needs_review', 'reason': 'Multiple similarly strong candidates', 'candidates': summary}
    return {'status': 'matched', **ranked[0], 'candidates': summary}

def metadata_action(item):
    if item.get('IsLocked') or item.get('LockedFields'):
        return 'locked'
    providers = item.get('ProviderIds') or {}
    if providers:
        complete = bool((item.get('ImageTags') or {}).get('Primary') and item.get('Overview') and any((person.get('Type') == 'Actor' for person in item.get('People') or [])))
        return 'preserve' if complete else 'repair_by_id'
    return 'search'

def retry_due(record, signature, now=None):
    return record.get('signature') != signature or (time.time() if now is None else now) >= record.get('next_attempt_at', 0)

def persist_sidecars(root, media_file, item, images):
    root = Path(root).absolute()
    try:
        relative = Path(media_file).absolute().relative_to(root).as_posix()
    except ValueError:
        raise ValueError('Sidecars must stay inside the managed catalogue') from None
    media = cache_path(root, relative)
    nfo = cache_path(root, media.relative_to(root).with_suffix('.nfo').as_posix())
    if not nfo.exists():
        nfo.parent.mkdir(parents=True, exist_ok=True)
        tag = 'tvshow' if item.get('Type') == 'Series' else 'movie'
        node = ET.Element(tag)
        for name, key in [('title', 'Name'), ('originaltitle', 'OriginalTitle'), ('plot', 'Overview'), ('year', 'ProductionYear'), ('mpaa', 'OfficialRating')]:
            if item.get(key) is not None:
                ET.SubElement(node, name).text = str(item[key])
        if item.get('PremiereDate'):
            ET.SubElement(node, 'premiered').text = item['PremiereDate'][:10]
        for provider, ident in (item.get('ProviderIds') or {}).items():
            ET.SubElement(node, 'uniqueid', type=provider.lower(), default='true' if provider.lower() == 'tmdb' else 'false').text = ident
        for genre in item.get('Genres') or []:
            ET.SubElement(node, 'genre').text = genre
        for person in item.get('People') or []:
            if person.get('Type') == 'Actor':
                actor = ET.SubElement(node, 'actor')
                ET.SubElement(actor, 'name').text = person['Name']
                ET.SubElement(actor, 'role').text = person.get('Role') or ''
            elif person.get('Type') in ('Director', 'Writer'):
                ET.SubElement(node, 'director' if person['Type'] == 'Director' else 'credits').text = person['Name']
        ET.indent(node)
        temp = cache_path(root, nfo.relative_to(root).as_posix() + '.' + secrets.token_hex(10) + '.tmp')
        try:
            with temp.open('xb') as output:
                ET.ElementTree(node).write(output, encoding='utf-8', xml_declaration=True)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temp, nfo)
            except FileExistsError:
                pass
        finally:
            temp.unlink(missing_ok=True)
    paths = {'nfo': nfo}
    for kind, suffix in [('Primary', '-poster.jpg'), ('Backdrop', '-fanart.jpg')]:
        name = ('poster.jpg' if kind == 'Primary' else 'fanart.jpg') if item.get('Type') == 'Series' else media.stem + suffix
        destination = cache_path(root, (media.parent / name).relative_to(root).as_posix())
        paths['poster' if kind == 'Primary' else 'backdrop'] = destination
        if images.get(kind) and (not destination.exists()):
            try:
                with destination.open('xb') as output:
                    output.write(images[kind])
            except FileExistsError:
                pass
    return paths
