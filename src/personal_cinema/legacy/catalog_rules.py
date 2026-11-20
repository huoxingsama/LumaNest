"""Confirmed work boundaries, separate from stable cloud and cache identities."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
from personal_cinema.runtime_paths import RuntimePath as Path

def classify_override(remote, rules):
    for rule in rules:
        if rule.get('remote'):
            if remote != rule['remote']:
                continue
        elif not remote.startswith(rule['remote_prefix'].rstrip('/') + '/'):
            continue
        suffix = Path(remote).suffix.lower()
        folder = rule['catalog_folder']
        title = rule['title']
        result = {'kind': rule['kind'], 'series': folder, 'display_title': title, 'catalog_rule_applied': True, 'series_tmdb': None, 'absolute_episode': None, 'season': None, 'episode': None}
        if rule['kind'] == 'Movies':
            result['relative'] = f'Movies/{folder}/{folder}{suffix}'
            result['catalog_relative'] = result['relative']
        else:
            match = re.search('S(\\d{1,2})E(\\d{1,5})(?!\\d)', Path(remote).stem, re.I)
            if not match or int(match[1]) != rule.get('source_season', int(match[1])):
                continue
            season = rule.get('season', int(match[1]))
            episode = int(match[2])
            result.update(season=season, episode=episode, relative=f'Shows/{folder}/Season {season:02}/{title} S{season:02}E{episode:02}{suffix}')
            result['catalog_relative'] = 'Anime/' + result['relative'][len('Shows/'):]
        return result

def retain_cache_path(entry, classified, ident, unique):
    result = dict(classified)
    result['relative'] = entry.get('cache_relative') or unique(classified['relative'], ident)
    if classified.get('catalog_relative'):
        result['catalog_relative'] = unique(classified['catalog_relative'], ident)
    return result
