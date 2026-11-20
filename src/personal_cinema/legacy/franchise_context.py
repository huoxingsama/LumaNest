"""Use explicit anthology folders as search context, not as proof of identity.

No provider ID is inferred here. The normal metadata identity gate still has
to verify the resulting work. Bonus clips never become complete-film guesses.
"""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import difflib
import unicodedata
from pathlib import PurePosixPath
ALIASES = {'精灵宝可梦': '宝可梦', '神奇宝贝': '宝可梦', '神奇寶貝': '宝可梦', '寶可夢': '宝可梦', '宝可梦': '宝可梦', 'pokemon': '宝可梦', 'pokémon': '宝可梦', '航海王': '海贼王', '海賊王': '海贼王', '海贼王': '海贼王', '名偵探柯南': '名侦探柯南', '名侦探柯南': '名侦探柯南', '火影忍者': '火影忍者'}
PACK = re.compile('(?:剧场版?|劇場版?|电影版?|電影版?|电影合集|電影合集|系列|合集|合輯|movies?|films?)', re.I)
UNKNOWN_PACK = re.compile('(?:剧场版?|劇場版?|系列|合集|合輯|movies|films)', re.I)
GENERIC = re.compile('^(?:夸克|百度|百度网盘|电影|電影|movies?|外挂字幕版|外掛字幕版|内封字幕版|內封字幕版|无字幕版|無字幕版|字幕|剧场OVA|劇場OVA|OVA|字幕版|[\\d\\s_\\-]+)$', re.I)
BONUS = re.compile('被?删减片段|被?刪減片段|删除片段|刪除片段|删剪片段|deleted[ ._-]*scenes?|幕后|幕後|花絮|behind[ ._-]*the[ ._-]*scenes|预告片|預告片|trailers?', re.I)
SPECIAL = re.compile('(?:^|[/\\s._\\-\\[【])(?:OVA|OAD)(?:$|[/\\s._\\-\\]】])|剧场OVA|劇場OVA|剧场版[ ._-]*(?:序章|后续)|劇場版[ ._-]*(?:序章|後續)|通往觉醒的序章', re.I)
NOISE = re.compile('^(?:DBD[- ]?Raws|\\d+|剧场|劇場|\\d+[pi]|BDRip|BluRay|HEVC.*|FLAC.*|AAC.*|AVC.*|x26[45].*|h26[45].*|简中.*|簡中.*|内置字幕|內置字幕|外挂字幕|外掛字幕)$', re.I)

def _family(parts):
    for index, folder in enumerate(parts[:-1]):
        value = unicodedata.normalize('NFKC', folder).strip()
        for alias, family in ALIASES.items():
            if alias.casefold() in value.casefold() and PACK.search(value):
                return family
            if value.casefold() == alias.casefold() and any((p in ('电影', '電影', '电视特别篇', '電視特別篇', '特别篇', '特別篇', 'OVA') for p in parts[index + 1:-1])):
                return family
        if UNKNOWN_PACK.search(value):
            prefix = UNKNOWN_PACK.split(value, 1)[0].strip(' ._-')
            if prefix and len(prefix) < 32 and (not re.fullmatch('[\\d\\s._-]+', prefix)):
                return prefix
    return None

def _title(value, family):
    text = unicodedata.normalize('NFKC', value)
    text = re.sub('\\.(?:mkv|mp4|avi|rmvb|mov|ts|m2ts)$', '', text, flags=re.I)
    text = re.sub('\\.(?:chs|cht|chi|zh|eng|ja|jpn)$', '', text, flags=re.I)
    brackets = re.findall('[\\[【]([^\\]】]+)[\\]】]', text)
    if brackets and all((not c.strip() for c in re.split('[\\[【][^\\]】]+[\\]】]', text))):
        values = [b for b in brackets if not NOISE.fullmatch(b.strip()) and b.strip().casefold() not in ALIASES]
        text = ' '.join(values)
    else:
        text = re.sub('[\\[【]([^\\]】]*)[\\]】]', lambda m: '' if NOISE.fullmatch(m[1].strip()) or re.search('(?:19|20)\\d{2}', m[1]) else ' ' + m[1] + ' ', text)
    text = re.sub('[\\[【](?:19|20)\\d{2}.*$', '', text)
    text = re.sub('\\s*\\((?:19|20)\\d{2}\\)\\s*$', '', text)
    text = re.split('(?i)\\b(?:1080[pi]|2160p|720p|BDRip|BluRay|HEVC|FLAC)\\b', text)[0]
    text = re.sub('^\\s*0*\\d{1,2}[ ._-]+', '', text)
    text = text.replace('.', ' ').replace('_', ' ')
    for alias, canonical in sorted(ALIASES.items(), key=lambda pair: -len(pair[0])):
        if text.casefold().startswith(alias.casefold()):
            text = text[len(alias):].lstrip(' :：-_ ')
            break
    text = re.sub('^\\s*[\\[【]?0*\\d{1,2}[\\]】]\\s*', '', text)
    text = re.sub('\\s+', ' ', text).strip(' -:：[]【】')
    return family + '：' + text if text and (not GENERIC.fullmatch(text)) else None

def context(remote):
    parts = list(PurePosixPath(remote.replace('\\', '/')).parts)
    family = _family(parts)
    if not family:
        return {}
    filename = parts[-1]
    role = 'bonus' if BONUS.search(filename) else 'special' if SPECIAL.search(remote) or any((p in ('电视特别篇', '電視特別篇', '特别篇', '特別篇') for p in parts[:-1])) else 'movie'
    parent = next((p for p in reversed(parts[:-1]) if p not in ('/', '\\') and (not GENERIC.fullmatch(p)) and (not PACK.search(p))), '')
    multiple = bool(re.search('[+＋]|(?:黑.*白|白.*黑).*(?:英雄)', parent))
    raw = [filename] if multiple else [parent, filename]
    aliases = []
    for value in raw:
        if not value:
            continue
        title = _title(value, family)
        if title and title not in aliases:
            aliases.append(title)
    return {'family': family, 'role': role, 'titles': [] if role == 'bonus' else aliases, 'filename_first': multiple, 'evidence': '明确的系列合集目录；附件用途由文件名标记', 'bonus_title': re.sub('[\\[【].*?[\\]】]', '', PurePosixPath(filename).stem) if role == 'bonus' else None}
POKEMON = {1: [('10228', '宝可梦：超梦的逆袭')], 2: [('12599', '宝可梦：梦幻的宝可梦 洛奇亚爆诞')], 3: [('10991', '宝可梦：结晶塔的帝王')], 4: [('12600', '宝可梦：时拉比 穿越时空的相遇')], 5: [('33875', '宝可梦：水都的守护神 拉帝亚斯与拉帝欧斯')], 6: [('36218', '宝可梦：七夜的许愿星 基拉祈')], 7: [('34065', '宝可梦：裂空的访问者 代欧奇希斯')], 8: [('34067', '宝可梦：梦幻与波导的勇者 路卡利欧')], 9: [('16808', '宝可梦：宝可梦巡护员与苍海的王子 玛纳霏')], 10: [('25961', '宝可梦：决战时空之塔 帝牙卢卡VS帕路奇犽VS达克莱伊')], 11: [('47292', '宝可梦：骑拉帝纳与冰空的花束 谢米')], 12: [('39057', '宝可梦：去往超克的时空')], 13: [('50087', '宝可梦：幻影的霸者索罗亚克')], 14: [('115223', '宝可梦：比克提尼与白英雄 莱希拉姆'), ('88557', '宝可梦：比克提尼与黑之英雄捷克罗姆')], 15: [('150213', '宝可梦：酋雷姆VS圣剑士凯尔迪奥')], 16: [('227679', '宝可梦：神速的灭世虫 盖诺赛克特 超梦觉醒')], 17: [('303903', '宝可梦：破坏之茧与蒂安希')], 18: [('350499', '宝可梦：光轮的超魔神')], 19: [('382190', '宝可梦：波尔凯尼恩与机巧的玛机雅娜')], 20: [('436931', '宝可梦剧场版：就决定是你了！')], 21: [('494407', '宝可梦剧场版：我们的故事')], 22: [('571891', '宝可梦：超梦的逆袭 进化')], 23: [('662708', '宝可梦剧场版：皮卡丘和可可的冒险')]}

def verified_source_identity(remote):
    """Ordinal plus Chinese subtitle in a known anthology can correct bad IDs.

    The ordinal alone is insufficient. OVA/bonus clips never inherit a feature
    film ID, and the black/white pair requires the filename's explicit version.
    """
    evidence = context(remote)
    if evidence.get('family') != '宝可梦' or evidence.get('role') != 'movie':
        return None
    parts = list(PurePosixPath(remote.replace('\\', '/')).parts)
    numbers = set()
    for raw in parts:
        text = unicodedata.normalize('NFKC', raw)
        match = re.match('^\\s*0*(\\d{1,2})[ ._-]+', text)
        if not match:
            match = re.search('(?:精灵宝可梦|宝可梦|神奇宝贝|Pok[eé]mon)\\s*\\[0*(\\d{1,2})\\]', text, re.I)
        if match:
            numbers.add(int(match[1]))
    if len(numbers) != 1:
        return None
    number = next(iter(numbers))
    records = POKEMON.get(number, [])
    if number == 14:
        filename = parts[-1]
        records = [record for record in records if '白英雄' in filename and '白英雄' in record[1] or ('黑英雄' in filename and '黑之英雄' in record[1])]
        if len(records) != 1:
            return None

    def norm(title):
        title = re.sub('^(?:宝可梦(?:剧场版)?)[：:]?', '', title)
        return ''.join((char for char in unicodedata.normalize('NFKC', title).casefold() if char.isalnum()))
    matches = []
    for ident, title in records:
        expected = norm(title)
        for alias in evidence.get('titles', []):
            candidate = norm(alias)
            if ('进化' in candidate or 'evolution' in candidate) != ('进化' in expected):
                continue
            similar = difflib.SequenceMatcher(None, candidate, expected).ratio()
            contains = min(len(candidate), len(expected)) >= 5 and (candidate in expected or expected in candidate)
            if similar >= 0.72 or contains:
                matches.append((ident, title))
                break
    if len(matches) != 1:
        return None
    ident, title = matches[0]
    result = {'kind': 'Movie', 'title': title, 'provider_ids': {'Tmdb': ident}, 'family': '宝可梦', 'number': number, 'verified_by': 'provider-collection-and-explicit-source-subtitle', 'source_url': 'https://www.themoviedb.org/movie/' + ident + '?language=zh-CN'}
    verified_years = {'10991': 2000, '25961': 2007, '115223': 2011, '88557': 2011, '571891': 2019}
    if ident in verified_years:
        result['year'] = verified_years[ident]
    return result
POKEMON_SPECIALS = ({'title': '宝可梦：超梦重现，风云再起！', 'year': 2000, 'provider_year': 2001, 'premiere_date': '2000-12-30', 'provider_ids': {'Tmdb': '36897', 'Imdb': 'tt0304564'}, 'aliases': ['超梦重现风云再起', '超夢重現風雲再起', 'Pokémon Mewtwo Returns', 'Pokemon Mewtwo Returns'], 'release_source': 'https://zh.wikipedia.org/zh-cn/超夢重現_風雲再起！'}, {'title': '宝可梦：超梦 觉醒之路的序章', 'year': 2013, 'provider_ids': {'Tmdb': '249424', 'Imdb': 'tt5750440'}, 'aliases': ['超梦通往觉醒的序章', '超梦觉醒之路的序章', '超夢通往覺醒的序章', 'Pokémon Mewtwo Prologue to Awakening', 'Pokemon Mewtwo Prologue to Awakening']}, {'title': '宝可梦：矿国的公主蒂安希', 'year': 2014, 'provider_ids': {'Tmdb': '407482', 'Imdb': 'tt7784582'}, 'aliases': ['矿国的公主蒂安希', '礦國的公主蒂安希', 'Diancie Princess of the Diamond Domain']}, {'title': '宝可梦：出来吧，小魔神胡帕！', 'year': 2015, 'provider_ids': {'Tmdb': '1677413', 'Imdb': 'tt7784584'}, 'aliases': ['出来吧小魔神胡帕', '出来吧小魔神胡巴', '出來吧小魔神胡帕', 'Hoopa The Mischief Pokémon', 'Pokemon Hoopa The Mischief of Pokemon']})

def verified_special_identity(remote):
    """Exact special aliases correct bad release tags without weakening films.

    The work must be in a confirmed Pokémon anthology context and explicitly
    marked as an OVA/prologue/special. Feature films and bonus clips are excluded.
    """
    evidence = context(remote)
    if evidence.get('family') != '宝可梦' or evidence.get('role') != 'special':
        return None

    def norm(title):
        title = re.sub('^(?:宝可梦(?:剧场版)?)[：:]?', '', title)
        return ''.join((c for c in unicodedata.normalize('NFKC', title).casefold() if c.isalnum()))
    titles = {norm(title) for title in evidence.get('titles', [])}
    matches = [record for record in POKEMON_SPECIALS if titles.intersection((norm(alias) for alias in [record['title'], *record['aliases']]))]
    if len(matches) != 1:
        return None
    record = matches[0]
    result = {key: dict(value) if isinstance(value, dict) else list(value) if isinstance(value, list) else value for key, value in record.items()}
    result.update(kind='Movie', role='special', family='宝可梦', cloud_category='电视特别篇', verified_by='exact-special-title-and-provider-identity', source_url='https://www.themoviedb.org/movie/' + record['provider_ids']['Tmdb'] + '?language=zh-CN')
    return result
