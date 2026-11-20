"""Explicit source identities and provider-verified absolute episode ordering."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import xml.etree.ElementTree as ET
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from catalog_intelligence import episode_label

def classify_absolute(path, rules):
    for rule in rules:
        if not path.startswith(rule['source_root'].rstrip('/') + '/'):
            continue
        match = re.fullmatch('(?:海贼王|航海王|ONE[ ._-]*PIECE)?\\s*(\\d{1,5})(?:[ ._-](?:4k|2160p|1080p|720p))?\\.*', Path(path).stem, re.I)
        labelled = episode_label(Path(path).name)
        extra = rule.get('extra_files', {}).get(Path(path).name)
        if extra:
            data = extra
            number = data['episode']
        else:
            directories = path[len(rule['source_root'].rstrip('/')) + 1:].split('/')[:-1]
            if any((re.search('剧场|劇場|特别篇|特別篇|特辑|特輯|短篇|联动|聯動|(?:^|[\\W_])(?:movies?|specials?|sp)(?:$|[\\W_])', folder, re.I) for folder in directories)):
                continue
            if not match and (not labelled or labelled.get('season') is not None):
                continue
            source_number = int(match[1]) if match else labelled['episode']
            if source_number <= 0:
                continue
            if not rule.get('source_min', 1) <= source_number <= rule.get('source_max', 99999):
                continue
            number = source_number - rule.get('episode_offset', 0)
            if number <= 0:
                continue
            data = rule['episodes'].get(str(number), {})
        season = data.get('season', 99)
        title = rule['catalog_folder']
        suffix = Path(path).suffix.lower()
        tail = f"{title}/Season {season:02}/{rule['title']} S{season:02}E{number:04}{suffix}"
        return {'kind': 'Shows', 'series': title, 'season': season, 'episode': number, 'is_extra': bool(extra), 'absolute_episode': None if extra else number, 'relative': 'Shows/' + tail, 'catalog_relative': 'Anime/' + tail, 'episode_title_prefix': data.get('episode_title_prefix', True), 'recognition_rule': 'continuous-series', 'recognition_evidence': '已核对的作品身份与总集数顺序', 'display_title': rule['title'], 'series_tmdb': rule['provider_ids']['Tmdb'], 'series_year': rule['year'], 'series_imdb': rule['provider_ids'].get('Imdb'), 'episode_title': data.get('title') or f'第{number}集（资料待更新）', 'episode_overview': data.get('overview', ''), 'episode_aired': data.get('aired', ''), 'season_title': rule['seasons'].get(str(season), '待核对集数')}

def write_missing(path, root_tag, fields, ids=()):
    if path.exists():
        return
    node = ET.Element(root_tag)
    for tag, value in fields:
        if value is not None and str(value):
            ET.SubElement(node, tag).text = str(value)
    for provider, value in ids:
        if value:
            ET.SubElement(node, 'uniqueid', {'type': provider, 'default': 'true' if provider == 'tmdb' else 'false'}).text = str(value)
    try:
        with path.open('xb') as output:
            ET.ElementTree(node).write(output, encoding='utf-8', xml_declaration=True)
    except FileExistsError:
        pass

def write_absolute_metadata(root, path, entry):
    if not entry.get('series_tmdb'):
        return
    series = cache_path(root, path.parent.parent.relative_to(root).as_posix() + '/tvshow.nfo')
    write_missing(series, 'tvshow', [('title', entry['display_title']), ('year', entry['series_year'])], [('tmdb', entry['series_tmdb']), ('imdb', entry.get('series_imdb'))])
    season = cache_path(root, path.parent.relative_to(root).as_posix() + '/season.nfo')
    write_missing(season, 'season', [('title', entry['season_title']), ('seasonnumber', entry['season']), ('lockdata', 'true')])
    episode = cache_path(root, path.with_suffix('.nfo').relative_to(root).as_posix())
    title = entry['episode_title']
    if entry.get('episode_title_prefix', True) and (not title.startswith('第' + str(entry['episode']) + '集')):
        title = f"第{entry['episode']}集 · {title}"
    write_missing(episode, 'episodedetails', [('title', title), ('showtitle', entry['display_title']), ('season', entry['season']), ('episode', entry['episode']), ('plot', entry.get('episode_overview')), ('aired', entry.get('episode_aired')), ('lockdata', 'true')])
