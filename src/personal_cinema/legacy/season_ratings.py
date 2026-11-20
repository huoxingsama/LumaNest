"""Season-specific Bangumi scores; no inherited or averaged whole-show scores."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import json, re, time, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from personal_cinema.runtime_paths import RuntimePath as Path
from chinese_credits import norm
from metadata_matcher import read_json, write_json
USER_AGENT = 'PersonalMediaLibrary/1.0 (local season metadata; public Bangumi API)'
CN = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '十一': 11, '十二': 12}
SEASON_LABEL = re.compile('\\bSeason\\s*\\d+\\b|第\\s*[一二三四五六七八九十\\d]+\\s*季|\\b\\d+(?:st|nd|rd|th)\\s+Season\\b', re.I)

def base_title(value):
    return norm(SEASON_LABEL.sub('', value or ''))

def season_number(subject):
    value = subject.get('name', '') + ' ' + subject.get('name_cn', '')
    m = re.search('\\bSeason\\s*(\\d+)\\b|第\\s*([一二三四五六七八九十\\d]+)\\s*季|\\b(\\d+)(?:st|nd|rd|th)\\s+Season', value, re.I)
    if m:
        return int(next((g for g in m.groups() if g))) if next((g for g in m.groups() if g)).isdigit() else CN.get(next((g for g in m.groups() if g)))
    return None

def matches(subject, parent, season, year=None):
    source_name = subject.get('name_cn') or subject.get('name', '')
    for format_name in ('迷你动画', '剧场版', '特别篇', '总集篇', '特典'):
        if format_name in source_name and format_name not in parent.get('Name', ''):
            return False
    n = season_number(subject)
    if n is not None and n != season:
        return False
    if season > 1 and n is None:
        return False
    titles = {base_title(subject.get('name_cn')), base_title(subject.get('name'))} - {''}
    aliases = [parent.get('Name', ''), parent.get('OriginalTitle', '')] + parent.get('MetadataAliases', [])
    if str((parent.get('ProviderIds') or {}).get('Tmdb')) == '34307':
        aliases += ['Shameless(US)']
    if not {base_title(a) for a in aliases} - {''} & titles:
        return False
    if '无耻之徒' in parent.get('Name', '') and (not re.search('\\bUS\\b|美版', subject.get('name', '') + ' ' + subject.get('name_cn', ''), re.I)):
        return False
    date = subject.get('date')
    if year and date and (int(date[:4]) != year):
        return False
    return True

def search(keyword):
    req = urllib.request.Request('https://api.bgm.tv/v0/search/subjects', data=json.dumps({'keyword': keyword, 'filter': {'type': [2, 6]}}, ensure_ascii=False).encode(), headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get('data', [])

def install_rating(root, client, season, source):
    rating = source.get('score')
    item = client.json('/Items/' + season['Id'])
    item['CommunityRating'] = rating
    overview = re.sub('^(?:Bangumi 本季评分：[^\\n]*|本季暂无独立评分[^\\n]*)\\n\\n', '', item.get('Overview') or '')
    item['Overview'] = (f"Bangumi 本季评分：{rating:g}/10（{source.get('votes', 0):,} 人评价）\n\n" if rating is not None else '本季暂无独立评分，未套用全剧分数。\n\n') + overview
    item['CriticRating'] = None
    client.json('/Items/' + item['Id'], item, method='POST')
    path = Path(item['Path']) / 'season.nfo'
    path.absolute().relative_to((root / 'catalog').absolute())
    node = ET.parse(path).getroot() if path.exists() else ET.Element('season')
    for child in list(node):
        if child.tag in ('rating', 'ratings', 'votes'):
            node.remove(child)
    plot = node.find('plot')
    if plot is None:
        plot = ET.SubElement(node, 'plot')
    plot.text = item['Overview']
    if rating is not None:
        ET.SubElement(node, 'rating').text = str(rating)
        ratings = ET.SubElement(node, 'ratings')
        value = ET.SubElement(ratings, 'rating', {'name': 'bangumi', 'max': '10', 'default': 'true'})
        ET.SubElement(value, 'value').text = str(rating)
        ET.SubElement(value, 'votes').text = str(source.get('votes', 0))
    ET.indent(node)
    temporary = path.with_suffix('.rating.tmp')
    ET.ElementTree(node).write(temporary, encoding='utf-8', xml_declaration=True)
    temporary.replace(path)

class RatingWorker:

    def __init__(self, root, client):
        self.root = Path(root)
        self.client = client

    def cycle(self, limit=2, target_ids=None):
        from personal_cinema.runtime_paths import option
        if not option('ratings.enabled', True) or 'bangumi' not in option('ratings.providers', ['bangumi']):
            return read_json(self.root / 'data/season-ratings-state.json', {})
        state = read_json(self.root / 'data/season-ratings-state.json', {})
        overrides = read_json(self.root / 'data/season-ratings-sources.json', {})
        target_ids = getattr(self, 'target_ids', None) if target_ids is None else target_ids
        if limit <= 0:
            return state
        done = 0
        if target_ids is None:
            parents = self.client.json('/Items?Recursive=true&IncludeItemTypes=Series&Fields=ProviderIds,OriginalTitle')['Items']
        else:
            parents = (self.client.json('/Items/' + str(ident)) for ident in sorted(set(target_ids)))
        for parent in parents:
            if parent.get('Type') != 'Series':
                continue
            for s in self.client.json('/Shows/' + parent['Id'] + '/Seasons?Fields=Path')['Items']:
                number = s.get('IndexNumber', 0)
                if not number:
                    continue
                key = parent.get('ProviderIds', {}).get('Tmdb', parent['Id']) + ':' + str(number)
                prior = state.get(key, {})
                if prior.get('next_attempt_at', 0) > time.time():
                    if prior.get('item_id') == s['Id']:
                        continue
                    if prior.get('status') in ('applied', 'no-independent-rating'):
                        install_rating(self.root, self.client, s, prior)
                        state[key] = {**prior, 'item_id': s['Id'], 'series_id': parent['Id']}
                        write_json(self.root / 'data/season-ratings-state.json', state)
                        continue
                try:
                    subject_id = overrides.get(key)
                    if parent.get('ProviderIds', {}).get('Tmdb') in ('37854', '46260', '31910') and (not subject_id):
                        result = {'status': 'no-independent-rating', 'score': None, 'reason': '连续动画的分篇没有独立评分，未套用全剧分数'}
                    else:
                        full = self.client.json('/Items/' + s['Id'])
                        year = full.get('ProductionYear')
                        query = parent['Name'] + (' 第' + str(number) + '季' if number > 1 else '')
                        values = search(query) if not subject_id else []
                        if not subject_id and (not any((matches(v, parent, number, year) for v in values))):
                            english = parent.get('OriginalTitle') or parent['Name']
                            if parent.get('ProviderIds', {}).get('Tmdb') == '34307':
                                english = 'Shameless(US)'
                            values = search(english + ' Season ' + str(number))
                        if subject_id:
                            request = urllib.request.Request('https://api.bgm.tv/v0/subjects/' + str(subject_id), headers={'User-Agent': USER_AGENT})
                            with urllib.request.urlopen(request, timeout=15) as response:
                                subject = json.load(response)
                        else:
                            candidates = [v for v in values if matches(v, parent, number, year)]
                            exact = [v for v in candidates if norm(v.get('name_cn') or v.get('name', '')) in (norm(parent.get('Name', '')), norm(parent.get('OriginalTitle', '')))]
                            if len(exact) == 1:
                                candidates = exact
                            if len(candidates) != 1:
                                subject = None
                            else:
                                subject = candidates[0]
                        score = (subject or {}).get('rating', {}).get('score', 0)
                        votes = (subject or {}).get('rating', {}).get('total', 0)
                        result = {'status': 'applied' if score and votes else 'no-independent-rating', 'score': score if score and votes else None, 'source': 'Bangumi', 'source_url': 'https://bgm.tv/subject/' + str(subject['id']) if subject else None, 'votes': votes, 'source_title': (subject or {}).get('name_cn') or (subject or {}).get('name'), 'source_subject_id': str(subject['id']) if subject else None}
                    install_rating(self.root, self.client, s, result)
                    state[key] = {**result, 'item_id': s['Id'], 'series_id': parent['Id'], 'series': parent['Name'], 'season': number, 'checked_at': time.time(), 'next_attempt_at': time.time() + 604800}
                except Exception as error:
                    state[key] = {**prior, 'status': 'retry', 'error_type': type(error).__name__, 'next_attempt_at': time.time() + 3600}
                write_json(self.root / 'data/season-ratings-state.json', state)
                done += 1
                if done >= limit:
                    return state
        return state
if __name__ == '__main__':
    from metadata_matcher import Jellyfin
    root = Path(_cinema_path(''))
    worker = RatingWorker(root, Jellyfin(root))
    state = worker.cycle(200)
    print('Season ratings:', sum((v['status'] == 'applied' for v in state.values())), 'verified;', sum((v['status'] == 'no-independent-rating' for v in state.values())), 'without own score')
