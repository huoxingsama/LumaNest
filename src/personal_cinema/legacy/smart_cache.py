"""Cache demand from server-observable playback signals; no cloud writes."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import datetime
import re
import urllib.parse
from personal_cinema.runtime_paths import RuntimePath as Path
from library_routing import catalog_relative

def session_entries(entries, sessions, active_streams):
    """One actual source per playing work, including paused multi-version movies."""
    result = {}
    for s in sessions:
        item = s.get('NowPlayingItem') or {}
        if not item.get('Id'):
            continue
        candidates = [e for e in entries if e.get('jellyfin_item_id') == item['Id']]
        source = (s.get('PlayState') or {}).get('MediaSourceId')
        exact = [e for e in candidates if source and e.get('jellyfin_media_source_id') == source]
        choices = exact or sorted(candidates, key=lambda e: (not bool(active_streams.get(e['id'])), bool(e.get('source_missing')), not bool(e.get('ready')), -e.get('size', 0), e['id']))
        if choices:
            result[choices[0]['id']] = s
    return result

def timestamp(value):
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else 0
    except (TypeError, ValueError):
        return 0

def apply_playback(entries, root, items, sessions, active_streams, now):
    """Apply a complete successful snapshot, including removal of favorites."""
    entries = list(entries)
    media_ids = {}
    by_id = {e['id']: e for e in entries}
    movie_folders = {}
    for entry in entries:
        if entry.get('kind') == 'Movies':
            movie_folders.setdefault(Path(catalog_relative(entry)).parent, []).append(entry)
    expanded = []
    for item in items:
        if item.get('Type') not in ('Movie', 'Episode'):
            expanded.append(item)
            continue
        try:
            parent = Path(item.get('Path') or '').relative_to(root / 'catalog').parent
        except ValueError:
            expanded.append(item)
            continue
        variants = {e['id']: e for e in movie_folders.get(parent, [])} if item['Type'] == 'Movie' else {}
        for source in item.get('MediaSources') or []:
            match = re.fullmatch('/stream/([a-f0-9]{24})', urllib.parse.urlsplit(source.get('Path') or '').path)
            if not match:
                continue
            entry = by_id.get(match[1])
            expected = 'Movies' if item['Type'] == 'Movie' else 'Shows'
            if entry and entry.get('kind') == expected:
                variants[entry['id']] = entry
                media_ids[entry['id']] = source.get('Id')
        if not variants:
            expanded.append(item)
        else:
            for e in variants.values():
                expanded.append({**item, 'Path': str((root / 'catalog' / catalog_relative(e)).with_suffix('.strm'))})
    favored_series = {i['Id'] for i in items if i.get('Type') == 'Series' and ((i.get('UserData') or {}).get('IsFavorite') or (i.get('UserData') or {}).get('Likes') is True)}
    updates = {}
    for item in expanded:
        if item.get('Type') == 'Series':
            continue
        try:
            relative = Path(item.get('Path') or '').relative_to(root / 'catalog').with_suffix('').as_posix()
        except ValueError:
            continue
        d = item.get('UserData') or {}
        u = updates.setdefault(relative, dict(played=False, favorite=False, series_favorite=False, active=False, last_played_at=0, position=0, runtime=0))
        u['played'] |= bool(d.get('Played'))
        u['favorite'] |= bool(d.get('IsFavorite') or d.get('Likes') is True)
        u['series_favorite'] |= item.get('SeriesId') in favored_series
        u['series_id'] = item.get('SeriesId') or u.get('series_id')
        u['jellyfin_item_id'] = item['Id']
        u['display_title'] = ' · '.join((v for v in [item.get('SeriesName'), item.get('Name')] if v))
        last = timestamp(d.get('LastPlayedDate'))
        if last >= u['last_played_at']:
            u.update(last_played_at=last, position=d.get('PlaybackPositionTicks') or 0, runtime=item.get('RunTimeTicks') or 0)
    for e in entries:
        u = updates.get(Path(catalog_relative(e)).with_suffix('').as_posix())
        e['playback_active'] = bool(u and u['active'])
        e['active'] = bool(active_streams.get(e['id'])) or bool(u and u['active'])
        if not u:
            continue
        e.update(favorite=u['favorite'], series_favorite=u['series_favorite'], series_id=u['series_id'], display_title=e.get('source_name_override') or u['display_title'] if e.get('kind') == 'Movies' else u['display_title'], jellyfin_item_id=u['jellyfin_item_id'], playback_position_ticks=u['position'], runtime_ticks=u['runtime'])
        if e['id'] in media_ids:
            e['jellyfin_media_source_id'] = media_ids[e['id']]
        e['last_played_at'] = max(e.get('last_played_at', 0), u['last_played_at'])
        e['last_access'] = max(e.get('last_access', 0), u['last_played_at'])
        e['progress_percent'] = round(min(100, 100 * u['position'] / u['runtime']), 1) if u['runtime'] else None
        resuming = u['position'] >= 30 * 10000000 and (not u['runtime'] or u['position'] < 0.9 * u['runtime'])
        if u['played'] and (not resuming):
            if e.get('watched_at') is None:
                e['watched_at'] = now
        else:
            e.pop('watched_at', None)
    playing = session_entries(entries, sessions, active_streams)
    for e in entries:
        s = playing.get(e['id'])
        e['playback_active'] = bool(s)
        e['playback_paused'] = bool(s and (s.get('PlayState') or {}).get('IsPaused'))
        e['active'] = bool(s) or bool(active_streams.get(e['id']))
        if s:
            e['last_access'] = now
            e['last_played_at'] = now
            position = (s.get('PlayState') or {}).get('PositionTicks')
            if position is not None:
                e['playback_position_ticks'] = position
                if e.get('runtime_ticks'):
                    e['progress_percent'] = round(min(100, 100 * position / e['runtime_ticks']), 1)
                if position >= 30 * 10000000 and (not e.get('runtime_ticks') or position < 0.9 * e['runtime_ticks']):
                    e.pop('watched_at', None)

def cache_plan(entries, next_episodes, now, recent_seconds=14 * 86400):
    """Ordered demand; ready files occupy window slots but need no transfer."""
    entries = [e for e in entries if not e.get('cache_blocked') and (not e.get('catalog_hidden') and (not e.get('source_missing')) or e.get('playback_active'))]
    playing = [e for e in entries if e.get('playback_active')]
    if playing:
        if any((not e.get('playback_paused') for e in playing)):
            return []
        return [{'id': e['id'], 'reason': '暂停播放 · 补齐当前影片'} for e in playing if not e.get('ready') and e.get('retry_download_at', 0) <= now]
    candidates = {}
    movie_groups = {}
    for e in entries:
        if e.get('kind') == 'Movies':
            movie_groups.setdefault(e.get('jellyfin_item_id') or e['id'], []).append(e)
    preferred = {min(group, key=lambda e: (not bool(e.get('ready')), not bool(e.get('active')), -e.get('size', 0), e['id']))['id'] for group in movie_groups.values()}

    def add(e, rank, reason, recency=0, order=0):
        if e.get('ready') or e.get('retry_download_at', 0) > now:
            return
        priority = (rank, -recency, order, e['id'])
        if e['id'] not in candidates or priority < candidates[e['id']][0]:
            candidates[e['id']] = (priority, reason)
    groups = {}
    for e in entries:
        unfinished = e.get('watched_at') is None
        recent = e.get('last_played_at', 0) > max(0, now - recent_seconds)
        if e.get('active'):
            add(e, 0, '正在播放', now)
        if e.get('queued'):
            add(e, 1, '高清替换 · 同步缓存' if e.get('cache_upgrade_from') else '手动指定', e.get('queued_at', 0))
        auto = e.get('kind') != 'Movies' or e['id'] in preferred
        if e.get('download_paused') and e.get('pause_kind') in ('background', 'steam') and (not e.get('manual_cancelled')) and (auto or e.get('queued')):
            add(e, 1.5, '恢复后台续传', -e.get('paused_at', 0))
        if e.get('pinned') and unfinished:
            add(e, 1, '手动保留')
        if recent and unfinished and (e.get('kind') == 'Movies') and auto:
            add(e, 3, '最近续播', e['last_played_at'])
        if e.get('favorite') and auto:
            add(e, 2, '收藏或喜欢')
        if e.get('kind') == 'Shows':
            groups.setdefault(e.get('series_id') or e['series'], []).append(e)
    for group in groups.values():
        history = [e for e in group if e.get('last_played_at', 0) > 0]
        latest = max(history, key=lambda e: e['last_played_at'], default=None)
        followed = any((e.get('series_favorite') for e in group))
        if not followed and (not (latest and latest['last_played_at'] > max(0, now - recent_seconds))):
            continue
        is_bonus = lambda e: bool(e.get('is_extra') or e.get('season') == 0)
        bonus = latest is not None and is_bonus(latest)
        season_order = lambda e: (e.get('season', 0), e.get('episode', 0))
        eligible = [e for e in group if is_bonus(e) == bonus and e.get('watched_at') is None and (not bonus or e.get('season') == latest.get('season'))]
        if latest:
            eligible = [e for e in eligible if season_order(e) >= season_order(latest)]
        variants = {}
        for e in eligible:
            key = season_order(e)
            preference = (not e.get('active', False), -e.get('last_played_at', 0), not e.get('ready', False), e['id'])
            if key not in variants or preference < variants[key][0]:
                variants[key] = (preference, e)
        window = [variants[k][1] for k in sorted(variants)][:max(0, next_episodes)]
        for order, e in enumerate(window):
            if latest and e['id'] == latest['id']:
                add(e, 3, '最近续播', latest['last_played_at'])
            else:
                add(e, 4, '追剧后续' if latest else '整剧收藏', latest['last_played_at'] if latest else 0, order)
    return [{'id': ident, 'reason': reason} for ident, (_, reason) in sorted(candidates.items(), key=lambda pair: pair[1][0])]
