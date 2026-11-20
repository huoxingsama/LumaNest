"""Chinese display labels for credits, retaining Jellyfin person-type enums."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from personal_cinema.runtime_paths import option
import re
JOBS = {'actor': '演员', 'actress': '演员', 'director': '导演', 'writer': '编剧', 'creator': '主创', 'producer': '制片人', 'executive producer': '执行制片人', 'co-producer': '联合制片人', 'associate producer': '协同制片人', 'co-executive producer': '联合执行制片人', 'supervising producer': '监制', 'line producer': '现场制片人', 'production manager': '制片主任', 'screenplay': '编剧', 'teleplay': '电视剧编剧', 'story': '故事', 'original story': '原作', 'original concept': '原创构思', 'original characters': '原作角色', 'characters': '角色创作', 'novel': '原著小说', 'comic book': '原著漫画', 'manga': '原著漫画', 'adaptation': '改编', 'scenario writer': '编剧', 'series composition': '系列构成', 'composer': '作曲', 'original music composer': '原创音乐', 'music': '音乐', 'cinematographer': '摄影指导', 'director of photography': '摄影指导', 'editor': '剪辑', 'production design': '美术指导', 'production designer': '美术指导', 'art direction': '美术指导', 'art director': '美术指导', 'costume design': '服装设计', 'costume designer': '服装设计', 'casting': '选角', 'casting director': '选角导演', 'animation director': '动画导演', 'character design': '角色设计', 'character designer': '角色设计', 'sound designer': '声音设计', 'sound design': '声音设计', 'sound': '声音制作', 'visual effects': '视觉特效', 'stunt coordinator': '特技指导', 'stunts': '特技演员', 'guest star': '客串', 'gueststar': '客串', 'self': '本人', 'himself': '本人', 'herself': '本人', 'themselves': '本人', 'narrator': '旁白', 'additional voices': '其他配音', 'voice': '配音', 'voice actor': '配音演员', 'voice actress': '配音演员', 'crew': '幕后制作'}
GENERIC_ROLES = {'bodyguard': '保镖', 'security guard': '安保人员', 'customer': '顾客', 'landlord': '房东', 'police officer': '警察', 'doctor': '医生', 'nurse': '护士', 'reporter': '记者', 'soldier': '士兵', 'guard': '守卫', 'waiter': '服务员', 'waitress': '服务员', 'bartender': '酒保', 'driver': '司机', 'the jackal': '豺狼'}
QUALIFIERS = {'voice': '配音', 'voices': '配音', 'uncredited': '未署名', 'archive footage': '资料影像', 'archive voice': '历史配音', 'additional voices': '其他配音', 'young': '幼年', 'singing voice': '歌唱配音', 'english voice': '英语配音', 'japanese voice': '日语配音', 'cameo': '客串'}
HAN = re.compile('[\\u3400-\\u9fff]')

def credit_types(person):
    """Retain all source job types even when one person occupies one card."""
    kinds = list(dict.fromkeys([person.get('Type')] + list(person.get('CreditTypes') or [])))
    labels = {'导演': 'Director', '编剧': 'Writer', '总编剧': 'Writer', '原作': 'Writer', '脚本': 'Writer', '制片人': 'Producer', '音乐': 'Composer', '作曲': 'Composer', '配乐': 'Composer', '主创': 'Creator'}
    parts = str(person.get('Role') or '').split(' · ')
    if len(parts) > 1:
        kinds.extend((labels[p] for p in parts if p in labels))
    return list(dict.fromkeys((kind for kind in kinds if kind)))

def merge_person_roles(people):
    """One card per proven identity; names alone never merge different people."""
    result = []

    def providers(person):
        return {str(k).casefold(): str(v) for k, v in (person.get('ProviderIds') or {}).items() if v}

    def same_person(left, right):
        a, b = (providers(left), providers(right))
        if any((key in b and b[key] != value for key, value in a.items())):
            return False
        return bool(left.get('Id') and left.get('Id') == right.get('Id') or any((b.get(key) == value for key, value in a.items())))
    for original in people:
        if not isinstance(original, dict):
            result.append(original)
            continue
        person = dict(original)
        existing = next((p for p in result if isinstance(p, dict) and same_person(p, person)), None)
        if existing is None:
            result.append(person)
            continue
        roles = []
        for row in (existing, person):
            role = localized_role(row)
            if isinstance(role, str):
                roles.extend((part.strip() for part in role.split(' · ') if part.strip()))
        existing['Role'] = ' · '.join(dict.fromkeys(roles))
        existing['CreditTypes'] = list(dict.fromkeys(credit_types(existing) + credit_types(person)))
        aliases = list(existing.get('Aliases') or []) + list(person.get('Aliases') or [])
        if person.get('Name') != existing.get('Name'):
            aliases.append(person.get('Name'))
        if aliases:
            existing['Aliases'] = list(dict.fromkeys((a for a in aliases if a)))
        for field in ('Id', 'PrimaryImageTag', 'ImageBlurHashes'):
            if not existing.get(field) and person.get(field):
                existing[field] = person[field]
        if person.get('ProviderIds'):
            existing['ProviderIds'] = {**person['ProviderIds'], **(existing.get('ProviderIds') or {})}
    return result

def localized_role(person, characters=None):
    role = person.get('Role')
    if role is not None and (not isinstance(role, str)):
        return role
    role = re.sub('\\s+', ' ', role or '').strip()
    kind = str(person.get('Type') or '').casefold()
    if not role:
        return JOBS.get(kind, '')
    if role.casefold() in JOBS:
        return JOBS[role.casefold()]
    match = re.fullmatch('(.*?)\\s*\\(([^()]*)\\)', role)
    character = match[1].strip() if match else role
    translated = (characters or {}).get(character) or GENERIC_ROLES.get(character.casefold())
    if character.casefold() in ('self', 'himself', 'herself', 'themselves', 'narrator'):
        translated = JOBS[character.casefold()]
    if translated:
        role = translated + (' (' + match[2] + ')' if match else '')

    def qualifier(m):
        words = [w.strip() for w in m[1].split(',')]
        if not any((w.casefold() in QUALIFIERS for w in words)):
            return m[0]
        return '（' + '，'.join((QUALIFIERS.get(w.casefold(), w) for w in words)) + '）'
    role = re.sub('\\(([^()]*)\\)', qualifier, role)
    role = re.sub('\\s+（', '（', role)
    if kind in ('actor', 'gueststar', 'guest star'):
        return role
    if not HAN.search(role) and kind in JOBS:
        return JOBS[kind] + ' · ' + role
    return role

def localize_people(value, rules=None, inherited=None, *, _portrait_policy=None):
    """Copy response data; change only display fields inside a People list."""
    if _portrait_policy is None:
        _portrait_policy = option('artwork.missing_portrait', 'placeholder')
    if isinstance(value, list):
        return [localize_people(v, rules, inherited, _portrait_policy=_portrait_policy) for v in value]
    if not isinstance(value, dict):
        return value
    rules = rules or {}
    ids = value.get('ProviderIds') or {}
    title = value.get('SeriesName') or value.get('Name') or ''
    characters = rules.get('characters', {}).get('tmdb:' + str(ids.get('Tmdb', '')))
    if characters is None:
        characters = rules.get('characters', {}).get('title:' + title, inherited or {})
    result = {k: localize_people(v, rules, characters, _portrait_policy=_portrait_policy) if k != 'People' else v for k, v in value.items()}
    if isinstance(value.get('People'), list):
        people = []
        for original in value['People']:
            if not isinstance(original, dict):
                people.append(original)
                continue
            person = dict(original)
            if 'Role' in person or person.get('Type'):
                role = localized_role(person, characters)
                if role or 'Role' in person:
                    person['Role'] = role
            for field in ['Job', 'Department']:
                if isinstance(person.get(field), str):
                    person[field] = JOBS.get(person[field].casefold(), person[field])
            people.append(person)
        merged = merge_person_roles(people)
        result['People'] = []
        for person in merged:
            if not isinstance(person, dict):
                continue
            has_photo = bool(person.get('PrimaryImageTag') or (person.get('ImageTags') or {}).get('Primary'))
            if not has_photo and _portrait_policy == 'hide':
                continue
            person['PortraitStatus'] = 'available' if has_photo else 'unavailable'
            result['People'].append(person)
    return result
