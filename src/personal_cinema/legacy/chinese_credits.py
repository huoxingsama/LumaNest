"""Chinese encyclopedia credits: exact-work validation, ordering and provenance."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import os
import difflib
from html.parser import HTMLParser

class Node:

    def __init__(self, tag='', attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def text(self):
        if self.tag in ('style', 'script'):
            return ''
        if self.tag == 'br':
            return '、'
        return ''.join((c if isinstance(c, str) else c.text() for c in self.children))

    def find(self, tag=None, cls=None):
        result = []
        for c in self.children:
            if isinstance(c, Node):
                if (tag is None or c.tag == tag) and (cls is None or cls in c.attrs.get('class', '')):
                    result.append(c)
                result.extend(c.find(tag, cls))
        return result

class Document(HTMLParser):

    def __init__(self, content):
        super().__init__()
        self.root = Node()
        self.stack = [self.root]
        self.person_aliases = {}
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def title(self):
        values = self.root.find('title')
        return values[0].text() if values else ''

    def description(self):
        result = next((n.attrs.get('content', '') for n in self.root.find('meta') if n.attrs.get('name') == 'description'), '')
        if not result or '维基百科' in self.title() or '維基百科' in self.title():
            result += ' ' + ''.join((p.text() for p in self.root.find('p')[:8]))[:2000]
        return result

    def tables(self):
        tables = []
        for table in self.root.find('table'):
            rows = []
            spans = {}
            for row in table.find('tr'):
                values = {}
                column = 0
                for index, (value, remaining) in list(spans.items()):
                    values[index] = value
                    if remaining <= 1:
                        del spans[index]
                    else:
                        spans[index] = (value, remaining - 1)
                for cell in [c for c in row.children if isinstance(c, Node) and c.tag in ('td', 'th')]:
                    while column in values:
                        column += 1

                    def span(name, maximum):
                        try:
                            return max(1, min(maximum, int(cell.attrs.get(name) or '1')))
                        except ValueError:
                            return 1
                    colspan = span('colspan', 20)
                    rowspan = span('rowspan', 500)
                    linked, aliases = linked_person_names(cell)
                    value = '、'.join(linked) if linked else credit_cell_text(cell)
                    self.person_aliases.update(aliases)
                    for offset in range(colspan):
                        values[column + offset] = value
                        if rowspan > 1:
                            spans[column + offset] = (value, rowspan - 1)
                    column += colspan
                if values:
                    rows.append([values.get(i, '') for i in range(max(values) + 1)])
            tables.append(rows)
        return tables

def simplify(value):
    if os.name != 'nt':
        return value
    import ctypes
    convert = ctypes.windll.kernel32.LCMapStringEx
    convert.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t]
    size = convert('zh-CN', 33554432, value, -1, None, 0, None, None, 0)
    if not size:
        return value
    output = ctypes.create_unicode_buffer(size)
    return output.value if convert('zh-CN', 33554432, value, -1, output, size, None, None, 0) else value

def clean(value):
    return simplify(re.sub('\\s+', ' ', re.sub('\\[\\s*(?:注|註)?\\s*\\d+(?:[-–,，、]\\d+)*\\s*\\]', '', value)).strip())

def norm(value):
    return re.sub('[\\W_]', '', re.sub('第(\\d+)(?:个|位|名)(?=[\\u3400-\\u9fff])', '第\\1', clean(value))).casefold()

def credit_cell_text(node):
    """Keep actual paragraph/list boundaries inside a credit cell."""
    if node.tag in ('script', 'style', 'sup'):
        return ''
    if node.tag in ('br', 'hr'):
        return '、'
    parts = []
    for index, child in enumerate(node.children):
        if isinstance(child, str):
            parts.append(child)
            continue
        if child.tag == 'br':
            following = next((next_child for next_child in node.children[index + 1:] if isinstance(next_child, Node) or str(next_child).strip()), None)
            if isinstance(following, Node) and following.tag == 'small' and re.match('[（(]', clean(following.text())):
                continue
        text = credit_cell_text(child)
        if child.tag in ('p', 'li', 'div') and text:
            parts.extend(['、', text, '、'])
        else:
            parts.append(text)
    return clean(re.sub('、+', '、', ''.join(parts))).strip('、 ')

def linked_person_names(node):
    """Use distinct source links, retaining same-link spellings as aliases.

    Only accept a cell composed of names and punctuation. Explanatory prose
    cannot become a person just because it happens to contain a wiki link.
    """
    groups = {}
    remainder = credit_cell_text(node)
    for link in node.find('a'):
        href = link.attrs.get('href', '').split('#', 1)[0]
        if not re.search('(?:^|https?://[^/]+)/(?:wiki|item)/', href):
            continue
        label = clean(link.text())
        if not label:
            continue
        groups.setdefault(href, [])
        if label not in groups[href]:
            groups[href].append(label)
        remainder = remainder.replace(label, '', 1)
    if not groups:
        return ([], {})
    residue = re.sub('[\\s（）()\\[\\]【】、,，;；&＆/／]', '', remainder)
    if residue:
        if len(groups) == 1:
            labels = next(iter(groups.values()))
            first = labels[0]
            text = credit_cell_text(node)
            match = re.fullmatch(re.escape(first) + "\\s*[、,，]\\s*([A-Za-zÀ-ž][A-Za-zÀ-ž .\\'’\\-]{1,90})", text)
            if re.search('[㐀-鿿]', first) and match:
                return ([first], {norm(first): [match[1].strip()]})
        return ([], {})
    result = []
    aliases = {}
    for labels in groups.values():
        preferred = next((label for label in labels if re.search('[㐀-鿿]', label)), labels[0])
        result.append(preferred)
        alternatives = [label for label in labels if label != preferred]
        if alternatives:
            aliases[norm(preferred)] = alternatives
    return (result, aliases)

def basic_fields(doc):
    fields = {}
    for node in doc.root.find('dt'):
        siblings = [n for n in node.parent.children if isinstance(n, Node)]
        index = siblings.index(node)
        if index + 1 < len(siblings) and siblings[index + 1].tag == 'dd':
            fields[norm(node.text())] = clean(siblings[index + 1].text())
    for table in doc.root.find('table', cls='infobox'):
        for row in table.find('tr'):
            cells = [c for c in row.children if isinstance(c, Node) and c.tag in ('td', 'th')]
            if len(cells) == 2:
                fields[norm(cells[0].text())] = clean(cells[1].text())
    return fields

def identity_matches(item, doc):
    """Reject same-title books, films of a different year, and adaptations."""
    title = doc.title()
    description = doc.description()
    if not title or title == '百度百科':
        return False
    heading = re.split('\\s+[-–—]\\s*(?:维基百科|維基百科|Wikipedia)|_百度百科', title)[0].strip()
    heading = re.sub('\\s*[（(](?:[^）)]*(?:电影|電影|电视剧|電視劇|推理剧|推理劇|悬疑剧|懸疑劇|犯罪剧|犯罪劇|迷你剧|迷你劇|电视动画|電視動畫|小说|小說|游戏|遊戲|漫画|漫畫))[^）)]*[）)]$', '', heading).strip()
    fields = basic_fields(doc)
    primary_name = fields.get(norm('中文名')) or fields.get(norm('中文名称'))
    if primary_name:
        heading = primary_name
    aliases = {norm(item.get('Name', '')), norm(item.get('OriginalTitle', ''))}
    aliases.update((norm(t) for t in item.get('RecognitionAliases', [])))
    if item.get('Type') == 'Movie':

        def core(t):
            return re.sub('^(?:火影忍者(?:疾风传)?|海贼王|航海王)(?:剧场版)?', '', norm(t))
        if any((core(heading) == core(t) and core(t) for t in [item.get('Name', ''), *item.get('RecognitionAliases', [])])):
            aliases.add(norm(heading))

        def subtitle_identity(value):
            parts = re.split('[:：]', value, maxsplit=1)
            return (re.sub('\\d+$', '', norm(parts[0])), norm(parts[1])) if len(parts) == 2 else None
        source = subtitle_identity(item.get('Name', ''))
        target = subtitle_identity(heading)
        if source and target and (len(source[1]) >= 2) and (source == target):
            aliases.add(norm(heading))
    if item.get('ProviderIds', {}).get('Tmdb') == '37854':
        aliases.update(map(norm, ['航海王', '海贼王']))
    original = norm(item.get('OriginalTitle', ''))
    source_original = fields.get(norm('外文名')) or fields.get(norm('外文名称'))
    exact = norm(heading) in aliases or bool(original and source_original and (norm(source_original) == original))
    close = bool(item.get('RecognitionAliases')) and item.get('Type') == 'Movie' and (max((difflib.SequenceMatcher(None, norm(heading), a).ratio() for a in aliases if a), default=0) >= 0.88)
    linked = False
    for link in doc.root.find('a'):
        href = link.attrs.get('href', '')
        for provider, pattern in [('Imdb', '(?:www\\.)?imdb\\.com/title/(tt\\d+)(?:[/?#]|$)'), ('Tmdb', '(?:www\\.)?themoviedb\\.org/(?:movie|tv)/(\\d+)(?:[-/?#]|$)')]:
            found = re.search(pattern, href)
            if found and found[1] == str(item.get('ProviderIds', {}).get(provider, '')):
                linked = True
    if not exact and (not close) and (not linked):
        return False
    if re.search('[（(][^）)]*(?:漫画|漫畫|小说|小說|图书|圖書|出版|游戏|遊戲)', title):
        return False
    if any((key in fields for key in map(norm, ['ISBN', '出版时间', '出版時間', '出版机构', '出版機構', '出版社']))):
        return False
    text = title + ' ' + description
    kind = item.get('Type')
    if kind == 'Series' and (not re.search('电视剧|電視劇|推理剧|推理劇|悬疑剧|懸疑劇|犯罪剧|犯罪劇|迷你剧|迷你劇|电视动画|電視動畫|网络动画|網路動畫|动画作品|動畫作品|长篇.*动画|原创.*动画|網路影集|電視影集', text)):
        return False
    if kind == 'Movie' and (not re.search('电影|電影|剧场版|劇場版|影片|恐怖片|剧情片|劇情片|喜剧片|喜劇片|动作片|動作片|动画片|動畫片', text)):
        if item.get('SourceRole') != 'special' or not re.search('特别篇|特別篇|OVA|原创动画录像带|原創動畫錄影帶', text):
            return False
    year = item.get('ProductionYear')
    if not year:
        return False
    years = []
    for key, value in fields.items():
        if key in map(norm, ['首播时间', '首播日期', '首映日期', '上映时间', '上映日期', '公映日期', '发行时间', '发行日期']):
            years.extend(re.findall('(?:19|20)\\d{2}', value))
    if years:
        return str(year) in years
    return str(year) in re.findall('(?:19|20)\\d{2}', text)

def person_name_available(value):
    value = clean(value)
    return bool(value and (not re.fullmatch('(?:主题曲|主題曲|配音|插曲|演唱|作词|作詞|作曲)\\s*[:：]', value)) and (not re.match('^(?:原译|原譯|旧译|舊譯)', value)) and (value.casefold() not in {'-', '—', '–', '－', '不适用', '不適用', '不详', '不詳', '不明', '未知', '无', '無', '无对白', '無對白', '无对白客串', '無對白客串', '未公布', '未公开', '未公開', '未列名', '未署名', '待定', 'n/a', 'n.a.', 'none', 'null', '?', '无台词', '無台詞', '没有台词', '沒有台詞', '剧场版中没有台词', '劇場版中沒有台詞', '安室透的回忆', '安室透的回憶'}))
_PERSON_GROUPS = {norm(name): [{'Name': '詹姆斯·菲尔普斯', 'Role': '弗雷德·韦斯莱', 'Aliases': ['James Phelps']}, {'Name': '奥立佛·菲尔普斯', 'Role': '乔治·韦斯莱', 'Aliases': ['Oliver Phelps']}] for name in ('詹士和奥利弗·菲利乌斯普斯', '詹姆斯和奥利弗·菲利乌斯普斯', '詹姆斯和奥利弗·菲力普斯', 'James and Oliver Phelps')}

def expand_credit_person(person):
    if not person_name_available(person.get('Name', '')):
        return []
    annotation = re.search('\\s*[<〈（(](幼年|童年|少年(?:时代|時代|时期|時期|期)|青年(?:时代|時代|时期|時期|期)|成年|老年)[>〉）)]\\s*$', person['Name'])
    if annotation:
        person = {k: v for k, v in person.items() if k not in ('Id', 'ProviderIds', 'PrimaryImageTag', 'ImageTags', 'ImageBlurHashes')}
        person['Name'] = person['Name'][:annotation.start()].strip()
        if annotation[1] not in person.get('Role', ''):
            person['Role'] = person.get('Role', '') + '（' + annotation[1] + '）'
    members = _PERSON_GROUPS.get(norm(person['Name'])) if person.get('Type') in ('Actor', 'GuestStar') else None
    if not members:
        member = next((m for group in _PERSON_GROUPS.values() for m in group if norm(m['Name']) == norm(person['Name']) and norm(m['Role']) in norm(person.get('Role', ''))), None)
        if member and person.get('Type') in ('Actor', 'GuestStar'):
            return [{**person, 'Aliases': list(dict.fromkeys([*(person.get('Aliases') or []), *member['Aliases']]))}]
        return [person]
    result = []
    role = person.get('Role', '')
    paired = norm(role) in {norm('弗雷德和乔治·韦斯莱'), norm('弗雷德&乔治·韦斯莱')}
    for member in members:
        row = {k: v for k, v in person.items() if k not in ('Id', 'ProviderIds', 'PrimaryImageTag', 'ImageTags', 'ImageBlurHashes', 'Aliases')}
        row['Name'] = member['Name']
        row['Aliases'] = list(member.get('Aliases') or [])
        if paired:
            row['Role'] = member['Role']
        result.append(row)
    return result

def names(value):
    if not person_name_available(value):
        return []
    result = []
    for part in re.split('[、/／,，&＆]', value):
        part = clean(part)
        for opening, closing in [('（', '）'), ('(', ')')]:
            while part.endswith(closing) and part.count(closing) > part.count(opening):
                part = part[:-1].rstrip()
        if person_name_available(part):
            result.append(part)
    return result

def prefer_chinese_role(role):
    role = re.sub("\\s*/\\s*[A-Za-z][A-Za-z .\\'-]*(?=[、，]|$)", '', role)
    if re.search('[㐀-鿿]', role):
        role = re.sub("[（(]\\s*[A-Za-z][A-Za-z0-9 .\\'’：:,&/\\-]*\\s*[）)]", '', role)
    parts = re.split('\\s+[／/]\\s+', role)
    if len(parts) > 1 and re.search('[㐀-鿿]', parts[0]):
        chinese = [p for p in parts if re.search('[㐀-鿿]', p)]
        return ' / '.join(chinese)
    return role

def cast_section_nodes(doc):
    active = False
    base = False
    has_heading = False
    allowed = set()
    for node in doc.root.find():
        if node.tag == 'h2':
            base = active = bool(re.search('人物|角色|演职|演員|演员|配音|声优', clean(node.text())))
            has_heading |= active
        elif node.tag == 'h3':
            active = base or bool(re.search('演职|演員|演员|配音|声优', clean(node.text())))
            has_heading |= active
        if active:
            allowed.add(node)
    return allowed if has_heading else None

def definition_credits(doc):
    result = []
    allowed = cast_section_nodes(doc)
    positions = {n: i for i, n in enumerate(doc.root.find())}
    for dl in doc.root.find('dl'):
        if allowed is not None and dl not in allowed:
            continue
        role = None
        if dl.parent.tag == 'li':
            prefix = ''.join((c if isinstance(c, str) else c.text() if c.tag != 'dl' else '' for c in dl.parent.children))
            role = re.split('[（(]', clean(prefix))[0].strip()
            if len(role) > 40:
                role = None
        if not role:
            siblings = [n for n in dl.parent.children if isinstance(n, Node)]
            before = siblings[:siblings.index(dl)]
            previous = before[-1] if before else None
            if previous and previous.tag in ('ul', 'ol'):
                labels = previous.find('li')
                if labels:
                    label_text = clean(labels[-1].text())
                    role = re.split('[（(]', label_text)[0].strip()
                    if len(role) > 40:
                        role = None
        for node in dl.children:
            if not isinstance(node, Node):
                continue
            if node.tag == 'dt':
                role = re.split('[（(]', clean(node.text()))[0].strip()
            if node.tag != 'dd' or not role:
                continue
            match = re.search('(?:声优|聲優|配音|声|聲)\\s*[:：-]\\s*(.*)', clean(node.text()))
            if not match:
                continue
            for person, note, language in original_voice_names(match[1]):
                if person in ('不明', '未知', '未公开'):
                    continue
                name = re.sub('[〈（(].*?[〉）)]', '', person).strip()
                if not name:
                    continue
                result.append({'Name': name, 'Type': 'Actor', 'Role': role + '（' + note + language + '配音）', '_billing': positions[node]})
    return result

def original_voice_names(text):
    text = text.translate(str.maketrans({'﹝': '（', '﹞': '）', '〔': '（', '〕': '）'}))
    labeled = re.search('(?:日本|日语|日語|日文)(?:配音|声优|聲優)?\\s*[:：]\\s*', text)
    match = re.search('[（(]\\s*(?:日|日本|日语|日語|日文)\\s*[）)]', text)
    if labeled:
        text = text[labeled.end():]
        text = re.split('[,，;；]\\s*(?:台湾|台灣|香港|中国|中國|大陆|大陸|英语|英語)(?:配音|声优|聲優)?\\s*[:：]', text)[0]
        language = '日语'
    elif match:
        text = text[:match.start()]
        language = '日语'
    else:
        if re.search('[（(]\\s*(?:台|臺|港|中|英)', text):
            return []
        text = re.split('[;；]', text)[0]
        language = ''
    result = []
    for part in re.split('[・/／、&＆]', text):
        note = '幼年·' if re.search('幼年|童年|少年时', part) else ''
        name = re.sub('(?:幼年|童年|少年时)(?:时|期)?\\s*[:：]?', '', part).strip()
        qualifier = re.search('[（(]([^）)]*)[）)]', name)
        if qualifier and (not note):
            note = qualifier[1].strip() + '·'
        name = re.sub('[（(].*?[）)]', '', name).strip('（）() ')
        if name and len(name) < 30 and (not re.search('[：:；;]', name)):
            result.append((name, note, language))
    return result

def voice_list_credits(doc):
    allowed = cast_section_nodes(doc)
    result = []
    positions = {n: i for i, n in enumerate(doc.root.find())}
    for node in [n for n in doc.root.find() if n.tag in ('li', 'p')]:
        if allowed is not None and node not in allowed or node.find('li') or node.find('dd'):
            continue
        text = clean(node.text())
        if not re.search('[（(﹝](?:日|日本|日语)', text):
            continue
        labeled = re.search('[（(﹝]\\s*(?:日本|日语|日語|日文)(?:配音|声优|聲優)?\\s*[:：]', text)
        if labeled:
            role = without_parentheses(text[:labeled.start()])
            raw = text[labeled.start() + 1:]
            if not role or len(role) > 40:
                continue
        else:
            match = re.match('([^：:、]{1,35})[：:、](.+)', text) or re.match('(.{1,40}?)\\s*[-–—]\\s*(.+)', text)
            if not match:
                continue
            role = match[1]
            raw = match[2]
        if role.count('（') + role.count('(') != role.count('）') + role.count(')'):
            continue
        for name, note, language in original_voice_names(raw):
            result.append({'Name': name, 'Type': 'Actor', 'Role': role + '（' + note + language + '配音）', '_billing': positions[node]})
    return result

def inline_voice_credits(doc):
    result = []
    children = []
    allowed = cast_section_nodes(doc)
    for node in doc.root.find('dt'):
        if allowed is not None and node not in allowed:
            continue
        text = clean(node.text())
        match = re.search('(?:配音|声优|声)\\s*[：:]\\s*', text)
        if not match:
            continue
        role = re.split('[（(]', text)[0].strip()
        raw = text[match.end():]
        while raw.endswith(('）', ')')) and raw.count('）') + raw.count(')') > raw.count('（') + raw.count('('):
            raw = raw[:-1]
        tokens = names(raw)
        if any(('电视动画' in token for token in tokens)):
            tokens = [t for t in tokens if '电视动画' in t]
        for token in tokens:
            if '广告' in token or '广播剧' in token:
                continue
            name = re.split('[（(]', token)[0].strip()
            if not name or len(name) > 40:
                continue
            note = '幼年·' if re.search('幼年|童年|少年时期', token) else ''
            (children if note else result).append({'Name': name, 'Type': 'Actor', 'Role': role + '（' + note + '日语配音）'})
    return result + children

def without_parentheses(text):
    result = []
    depth = 0
    for character in text:
        if character in '（(':
            depth += 1
        elif character in '）)':
            depth = max(0, depth - 1)
        elif not depth:
            result.append(character)
    return ''.join(result).strip()

def prose_actor_credits(doc):
    result = []
    allowed = cast_section_nodes(doc)
    group = ''
    for node in doc.root.find():
        if node.tag == 'h3':
            group = clean(node.text())
        if node.tag != 'li':
            continue
        if allowed is not None and node not in allowed or node.find('li'):
            continue
        text = clean(node.text())
        split = re.split('\\s*饰演\\s*|\\s*饰\\s*', text, maxsplit=1)
        if len(split) != 2:
            continue
        actor, role = split
        actor = without_parentheses(actor)
        role = re.split('[:：]', without_parentheses(role))[0].strip()
        if actor and role and (len(actor) < 50):
            result.append({'Name': actor, 'Type': 'Actor', 'Role': role + ('（客串）' if '客串' in group else '')})
    return result

def parenthesized_credits(doc):
    """A cast section can list Chinese role (actor) without a separate label."""
    result = []
    allowed = cast_section_nodes(doc)
    positions = {n: i for i, n in enumerate(doc.root.find())}
    if allowed is None:
        return result
    for node in doc.root.find('li'):
        if node not in allowed or node.find('li') or node.find('dd'):
            continue
        text = clean(node.text())
        match = re.fullmatch('([^（(]{1,40})[（(](.+)[）)]', text)
        if not match:
            continue
        role = match[1].strip()
        name = without_parentheses(match[2]).strip()
        if re.search('[：:。；;]', role) or not re.fullmatch("[\\u3400-\\u9fff\\u3040-\\u30ffA-Za-z·・. \\'-]{2,35}", name):
            continue
        if not re.search('[㐀-鿿]', name) or len(name) > 25 or re.search('[\\d:：；;]|角色|主角|配音|声优|动画|漫画|台版|港版|日版|宝可梦|神奇宝贝|幼年|童年|少年|日本|台湾|香港', name):
            continue
        if re.search('进化|進化|演化|变成|變成|化为|化為|后来|後來', name):
            continue
        if not any((norm(link.text()) == norm(name) for link in node.find('a') if re.search('(?:^|https?://[^/]+)/(?:wiki|item)/', link.attrs.get('href', '')))):
            continue
        result.append({'Name': name, 'Type': 'Actor', 'Role': role, '_billing': positions[node]})
    return result

def parse_credits(content, maximum=60, all_cast_tables=False):
    """Read a cast table or Baike actor cards in source billing order."""
    doc = Document(content)
    current = []
    former = []
    seen = set()
    allowed = cast_section_nodes(doc)
    for source, table in zip(doc.root.find('table'), doc.tables()):
        if allowed is not None and source not in allowed:
            continue
        if not table:
            continue
        header = table[0]
        header_row = 0
        for idx, cells in enumerate(table[:4]):
            if any((c in ('演员', '演員', '演员名', '演員名') for c in cells)) and any((c in ('角色', '角色名', '人物', '飾演角色', '饰演角色') for c in cells)):
                header = cells
                header_row = idx
                break
        role_index = next((i for i, c in enumerate(header) if c in ('角色', '角色名', '角色名称', '角色名稱', '人物', '飾演角色', '饰演角色')), None)
        japanese = next((i for i, c in enumerate(header) if re.search('日语|日語|日本.*(?:配音|声优)', c) and (not re.search('曾|代役', c))), None)
        start = header_row + 1
        if len(table) > start and any((c == '日本' for c in table[start])):
            japanese = table[start].index('日本')
            start += 1
        actor_index = japanese if japanese is not None else next((i for i, c in enumerate(header) if c in ('演员', '演員', '演员名', '演員名')), None)
        if actor_index is None:
            actor_index = next((i for i, c in enumerate(header) if c in ('配音', '配音员', '配音員', '声优', '聲優')), None)
        english = next((i for i, c in enumerate(header) if c in ('美国', '美國', '英国', '英國', '英语', '英語', '英语配音', '英語配音')), None)
        if actor_index is None:
            actor_index = english
        if role_index is None or actor_index is None:
            continue
        former_index = next((i for i, c in enumerate(header) if re.search('日语|日語', c) and re.search('曾|代役', c)), None)
        for row in table[start:]:
            if max(role_index, actor_index) >= len(row):
                continue
            role = row[role_index]
            if not role or re.search('幼年|童年|少年时期|少年時期', role):
                continue
            if role == row[actor_index]:
                continue
            suffix = '日语配音' if japanese is not None else '英语配音' if english is not None and actor_index == english else '配音' if '配音' in header[actor_index] else ''
            for name in names(row[actor_index]):
                if len(name) > 45 or 'OVA' in name or name in ('演员名', '演員名', '配音员', '日本', '无对白客串', '無對白客串', '无对白', '無對白', '未公布'):
                    continue
                effective_suffix = '英语台词替声' if re.search('英文台词替声|英文台詞替聲|英语台词替声|英語台詞替聲', name) else suffix
                note = re.search('[（(](.*?)[）)]', name)
                if note:
                    name = re.sub('[（(].*?[）)]', '', name).strip()
                    if not name or '预告片' in note[1]:
                        continue
                    if note[1].startswith(('日语', '日語', '英语', '英語')):
                        current.append({'Name': name, 'Type': 'Actor', 'Role': role + ('（' + effective_suffix + '）' if effective_suffix else '')})
                    elif re.search('代演|代役', note[1]):
                        current.append({'Name': name, 'Type': 'Actor', 'Role': role + '（' + note[1] + ('·' + effective_suffix if effective_suffix else '') + '）'})
                    else:
                        former.append({'Name': name, 'Type': 'Actor', 'Role': role + '（' + note[1] + ('·' + effective_suffix if effective_suffix else '') + '）'})
                else:
                    current.append({'Name': name, 'Type': 'Actor', 'Role': role + ('（' + effective_suffix + '）' if effective_suffix else '')})
            if former_index is not None and former_index < len(row):
                for name in names(row[former_index]):
                    if 'OVA' in name:
                        continue
                    name = re.sub('[（(].*?[）)]', '', name).strip()
                    if name:
                        former.append({'Name': name, 'Type': 'Actor', 'Role': role + '（日语配音·曾任／代役）'})
        if current and (not all_cast_tables):
            break
    if not current:
        for card in doc.root.find(cls='actorItem_'):
            dt = card.find('dt')
            dd = card.find('dd')
            if not dt:
                continue
            name = clean(dt[0].text())
            role = clean(dd[0].text()) if dd else ''
            if re.search('\\s饰\\s', name):
                name, role = re.split('\\s饰\\s', name, maxsplit=1)
            role = re.sub('^饰\\s*', '', role)
            if name:
                current.append({'Name': name, 'Type': 'Actor', 'Role': role if role not in ('-', '—') else '演员'})
    if all_cast_tables:
        current.extend(sorted(definition_credits(doc) + voice_list_credits(doc) + parenthesized_credits(doc), key=lambda p: p['_billing']))
    if not current:
        current = inline_voice_credits(doc) or prose_actor_credits(doc)
    if not current:
        for node in doc.root.find():
            if node.tag not in ('p', 'div') or (node.tag == 'div' and node.attrs.get('data-tag') != 'paragraph'):
                continue
            if allowed is not None and node not in allowed:
                continue
            match = re.fullmatch('主演\\s*[:：]\\s*(.{1,700})', clean(node.text()))
            if not match:
                continue
            for name in names(match[1]):
                if person_name_available(name) and len(name) < 50:
                    current.append({'Name': name, 'Type': 'Actor', 'Role': '演员', 'RoleStatus': 'not_published'})
    merged = []
    for person in [member for row in current + former for member in expand_credit_person(row)]:
        person.pop('_billing', None)
        key = norm(person['Name'])
        if key in seen:
            existing = next((p for p in merged if norm(p['Name']) == key))
            if person['Role'] not in existing['Role']:
                existing['Role'] += ' / ' + person['Role']
            continue
        person['Role'] = prefer_chinese_role(person['Role'])
        if norm(person['Name']) in doc.person_aliases:
            person['Aliases'] = list(dict.fromkeys([*(person.get('Aliases') or []), *doc.person_aliases[norm(person['Name'])]]))
        seen.add(key)
        merged.append(person)
        if len(merged) >= maximum:
            break
    return merged

def ordered_people(item, profile):
    """Use verified source billing and retain separately sourced crew credits."""
    actors = [{**p, 'Name': names(p['Name'])[0] if names(p['Name']) else p['Name'], 'Role': prefer_chinese_role(p.get('Role', ''))} for row in profile['actors'] for p in expand_credit_person(row)]
    crew = profile.get('crew')
    if crew is None:
        crew = [p for p in item.get('People', []) if p.get('Type') not in ('Actor', 'GuestStar')]
    result = actors + [dict(p) for p in crew if person_name_available(p.get('Name', ''))]
    existing = {}
    for person in item.get('People', []):
        if person.get('Id'):
            existing.setdefault(norm(person.get('Name', '')), []).append(person)
    for person in result:
        candidates = existing.get(norm(person['Name']), [])
        if len({p['Id'] for p in candidates}) == 1:
            old = candidates[0]
            for field in ('Id', 'PrimaryImageTag', 'ImageBlurHashes', 'ProviderIds'):
                if field in old and field not in person:
                    person[field] = old[field]
            if person.get('RoleStatus') == 'not_published' and re.search('[㐀-鿿]', old.get('Role', '')) and (not old.get('Role', '').startswith('演员')):
                person['Role'] = old['Role']
    if profile.get('cast_scope') == 'starring':
        ids = {p.get('Id') for p in result if p.get('Id')}
        known_names = {norm(p.get('Name', '')) for p in result}
        for old in item.get('People', []):
            if old.get('Type') not in ('Actor', 'GuestStar') or old.get('Id') in ids or norm(old.get('Name', '')) in known_names:
                continue
            person = dict(old)
            if not re.search('[㐀-鿿]', person.get('Role', '')):
                person['OriginalRole'] = person.get('Role', '')
                person['Role'] = '演员（角色名暂无中文资料）'
            result.append(person)
    from people_localization import merge_person_roles
    return merge_person_roles(result)

def parse_crew(content):
    doc = Document(content)
    fields = basic_fields(doc)
    result = []
    seen = set()
    people_fields = {}
    allowed = cast_section_nodes(doc)
    for node in doc.root.find():
        if node.tag not in ('p', 'div') or (node.tag == 'div' and node.attrs.get('data-tag') != 'paragraph'):
            continue
        if allowed is not None and node not in allowed:
            continue
        match = re.fullmatch('(导演|编剧|制片人|制片|监制)\\s*[:：]\\s*(.{1,150})', clean(node.text()))
        if match:
            fields.setdefault(norm('制片人' if match[1] == '制片' else match[1]), match[2])
    for table in doc.root.find('table', cls='infobox'):
        for row in table.find('tr'):
            cells = [c for c in row.children if isinstance(c, Node) and c.tag in ('td', 'th')]
            if len(cells) != 2:
                continue
            pieces = [li for li in cells[1].find('li') if not li.find('li')] or [cells[1]]
            values = []
            for piece in pieces:
                linked, aliases = linked_person_names(piece)
                values.extend(linked or names(credit_cell_text(piece)))
                doc.person_aliases.update(aliases)
            people_fields[norm(cells[0].text())] = values
    for field, kind, role in [('导演', 'Director', '导演'), ('编剧', 'Writer', '编剧'), ('总编剧', 'Writer', '总编剧'), ('原作者', 'Writer', '原作'), ('音乐', 'Composer', '音乐'), ('配乐', 'Composer', '配乐'), ('作曲', 'Composer', '作曲'), ('脚本', 'Writer', '脚本'), ('开创', 'Creator', '主创'), ('开发', 'Creator', '改编开发'), ('剧本统筹', 'Writer', '剧本统筹'), ('系列构成', 'Writer', '系列构成'), ('制片人', 'Producer', '制片人'), ('监制', 'Producer', '监制')]:
        for name in people_fields.get(norm(field), names(fields.get(norm(field), ''))):
            assistant = bool(re.search('[（(](?:助理|协助|協助)[）)]', name))
            name = without_parentheses(name)
            if re.search('公司|工作室|出版社|CD Projekt Red|\\bStudio\\b|\\bProductions?\\b|\\bInc\\b|\\bLtd\\b', name, re.I):
                continue
            if not name or len(name) > 24 or (kind, name) in seen:
                continue
            seen.add((kind, name))
            person = {'Name': name, 'Type': kind, 'Role': ('助理导演' if kind == 'Director' else role + '（协助）') if assistant else role}
            if norm(name) in doc.person_aliases:
                person['Aliases'] = doc.person_aliases[norm(name)]
            result.append(person)
    return result
