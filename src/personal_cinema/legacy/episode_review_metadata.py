"""Retire generated review placeholders after a stable episode is identified."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import shutil
import time
import urllib.parse
import xml.etree.ElementTree as ET
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_policy import cache_path
from library_routing import catalog_relative, meaningful
from library_tasks import write
REVIEW_TITLE = re.compile('^第\\d+集（编号待核对）$')

def confirmed(entry):
    return entry.get('kind') == 'Shows' and (not entry.get('identity_needs_review')) and (not entry.get('source_missing')) and (not entry.get('catalog_hidden')) and isinstance(entry.get('season'), int) and (entry['season'] != 99) and isinstance(entry.get('episode'), int)

def clear_local_review(catalog, entry):
    if not confirmed(entry):
        return []
    catalog = Path(catalog)
    path = cache_path(catalog, Path(catalog_relative(entry)).with_suffix('.nfo').as_posix())
    if not path.is_file():
        return []
    try:
        tree = ET.parse(path)
        node = tree.getroot()
    except (ET.ParseError, OSError):
        return []
    if node.tag != 'episodedetails' or node.findtext('season') != '99' or (not REVIEW_TITLE.fullmatch(node.findtext('title') or '')):
        return []
    backup = catalog.parent / 'data/catalog-review-backups' / entry['id']
    backup.mkdir(parents=True, exist_ok=True)
    target = backup / (path.name + '.' + str(time.time_ns()))
    shutil.copy2(path, target)
    node.find('season').text = str(entry['season'])
    node.find('title').text = entry.get('episode_title') or '第' + str(entry['episode']) + '集'
    episode = node.find('episode')
    if episode is not None:
        episode.text = str(entry['episode'])
    lock = node.find('lockdata')
    if lock is not None:
        lock.text = 'false'
    tree.write(path, encoding='utf-8', xml_declaration=True)
    changed = [str(path)]
    season_file = path.parent / 'season.nfo'
    if season_file.is_file():
        try:
            season_tree = ET.parse(season_file)
            season = season_tree.getroot()
            if season.findtext('title') == '待核对集数' and season.findtext('seasonnumber') == '99':
                shutil.copy2(season_file, backup / ('season.nfo.' + str(time.time_ns())))
                season.find('title').text = '第 ' + str(entry['season']) + ' 季'
                season.find('seasonnumber').text = str(entry['season'])
                locked = season.find('lockdata')
                if locked is not None:
                    locked.text = 'false'
                season_tree.write(season_file, encoding='utf-8', xml_declaration=True)
                changed.append(str(season_file))
        except (ET.ParseError, OSError):
            pass
    return changed

def repair_confirmed_reviews(root, client, entries):
    """Work-scoped metadata updates retain the native episode/user-data IDs."""
    root = Path(root)
    groups = {}
    local = []
    changed = []
    removed = []
    for entry in entries:
        if not confirmed(entry):
            continue
        local.extend(clear_local_review(root / 'catalog', entry))
        if entry.get('series_id'):
            groups.setdefault(entry['series_id'], []).append(entry)

    def api(path, data=None, method=None):
        return client.json(path, data, method) if hasattr(client, 'json') else client.api('jellyfin', path, data, method)
    for series_id, selected in groups.items():
        rows = api('/Items?ParentId=' + series_id + '&Recursive=true&IncludeItemTypes=Season,Episode&Fields=Path,ParentId,SeriesId,ParentIndexNumber,IndexNumber,ChildCount')['Items']
        by_path = {str(Path(row['Path']).resolve()).casefold(): row for row in rows if row.get('Path')}
        for entry in selected:
            path = cache_path(root / 'catalog', Path(catalog_relative(entry)).with_suffix('.strm').as_posix())
            row = by_path.get(str(path.resolve()).casefold())
            if not row or row.get('Type') != 'Episode' or row.get('ParentIndexNumber') != 99:
                continue
            full = api('/Items/' + row['Id'])
            if full.get('SeriesId') != series_id or full.get('ParentIndexNumber') != 99:
                continue
            before = {k: full.get(k) for k in ('Id', 'Name', 'Path', 'SeriesId', 'SeasonId', 'ParentIndexNumber', 'IndexNumber', 'IsLocked', 'LockedFields')}
            write(root / 'data/catalog-review-backups' / entry['id'] / 'native-before.json', before)
            update = {**full, 'ParentIndexNumber': entry['season'], 'IndexNumber': entry['episode']}
            if REVIEW_TITLE.fullmatch(full.get('Name') or ''):
                update.update(Name=entry.get('episode_title') or '第' + str(entry['episode']) + '集', IsLocked=False, LockedFields=[k for k in full.get('LockedFields') or [] if k not in ('Name', 'IndexNumber', 'ParentIndexNumber')])
            api('/Items/' + row['Id'], update, 'POST')
            row.update(ParentIndexNumber=entry['season'], IndexNumber=entry['episode'])
            changed.append(row['Id'])
        if any((row.get('Type') == 'Episode' and row.get('ParentIndexNumber') == 99 for row in rows)):
            continue
        stale = [row for row in rows if row.get('Type') == 'Season' and row.get('IndexNumber') == 99 and (not row.get('Path')) and (not row.get('ChildCount'))]
        for season in stale:
            children = api('/Items?ParentId=' + season['Id'] + '&Recursive=true&Limit=1')
            if children.get('TotalRecordCount') or children.get('Items'):
                continue
            destinations = {e['season'] for e in selected}
            target = next((row for row in rows if row.get('Type') == 'Season' and row.get('IndexNumber') in destinations), None) if len(destinations) == 1 else None
            users = api('/Users')
            records = []
            for user in users:
                old = api('/Users/' + user['Id'] + '/Items/' + season['Id']).get('UserData') or {}
                if not meaningful(old):
                    continue
                if not target:
                    break
                current = api('/Users/' + user['Id'] + '/Items/' + target['Id']).get('UserData') or {}
                payload = {k: current[k] for k in ('Played', 'PlayCount', 'PlaybackPositionTicks', 'LastPlayedDate', 'IsFavorite', 'Likes', 'Rating') if k in current}
                if old.get('IsFavorite'):
                    payload['IsFavorite'] = True
                if old.get('Likes') is not None and current.get('Likes') is None:
                    payload['Likes'] = old['Likes']
                if old.get('Rating') is not None and current.get('Rating') is None:
                    payload['Rating'] = old['Rating']
                records.append({'user_id': user['Id'], 'old_id': season['Id'], 'target_id': target['Id'], 'data': old})
                write(root / 'data/catalog-review-backups' / season['Id'] / 'season-user-data.json', records)
                api('/UserItems/' + target['Id'] + '/UserData?userId=' + urllib.parse.quote(user['Id']), payload, 'POST')
            else:
                api('/Items/' + series_id + '/Refresh?Recursive=false&MetadataRefreshMode=ValidationOnly&ImageRefreshMode=None&ReplaceAllMetadata=false', method='POST')
                removed.append(season['Id'])
    result = {'updated_at': time.time(), 'local_sidecars': local, 'updated_episode_ids': changed, 'empty_season_cleanup_requested': removed}
    if local or changed or removed:
        write(root / 'data/catalog-review-recovery.json', result)
    return result
