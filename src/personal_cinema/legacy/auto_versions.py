"""Persistent, evidence-based source upgrades, executed by the cache state owner."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
from pathlib import PurePosixPath
from personal_cinema.runtime_paths import RuntimePath as Path
from library_tasks import read, write
from cache_policy import cache_path, delete_cached
from library_routing import catalog_relative, meaningful
PROFILE_VERSION = 2
LANGUAGES = {'ja': 'jpn', 'jpn': 'jpn', 'japanese': 'jpn', '日语': 'jpn', '日語': 'jpn', '日本語': 'jpn', 'en': 'eng', 'eng': 'eng', 'english': 'eng', '英语': 'eng', '英語': 'eng', 'zh': 'zho', 'chi': 'zho', 'zho': 'zho', 'cmn': 'zho', 'chinese': 'zho', 'mandarin': 'zho', '中文': 'zho', '汉语': 'zho', '漢語': 'zho', '普通话': 'zho', '普通話': 'zho', '国语': 'zho', '國語': 'zho', 'yue': 'yue', 'cantonese': 'yue', '粤语': 'yue', '粵語': 'yue', 'ko': 'kor', 'kor': 'kor', 'korean': 'kor', '韩语': 'kor', '韓語': 'kor', 'fr': 'fra', 'fre': 'fra', 'fra': 'fra', 'french': 'fra', '法语': 'fra', '法語': 'fra', 'de': 'deu', 'ger': 'deu', 'deu': 'deu', 'german': 'deu', '德语': 'deu', '德語': 'deu', 'es': 'spa', 'spa': 'spa', 'spanish': 'spa', '西班牙语': 'spa', '西班牙語': 'spa'}

def language(value):
    text = str(value or '').strip().casefold()
    return LANGUAGES.get(text) or LANGUAGES.get(text.split('-')[0])

def original_language(worker, entry):
    """Use explicit metadata or the exact work page's main language field."""
    for key in ('original_language', 'original_audio_language', 'metadata_original_language'):
        if language(entry.get(key)):
            return language(entry[key])
    ident = work_id(entry)
    if not ident:
        return None
    page = worker.root / 'data/artwork-source-pages' / ('movie-' + ident + '.html')
    if not page.exists():
        return None
    from chinese_credits import Document
    doc = Document(page.read_text('utf-8'))
    canonical = next((node.attrs.get('href', '') for node in doc.root.find('link') if node.attrs.get('rel') == 'canonical'), '')
    if not re.fullmatch('https://www\\.themoviedb\\.org/movie/' + re.escape(ident) + '(?:-[^/?#]*)?/?', canonical):
        return None
    for node in doc.root.find('p'):
        text = node.text().strip()
        match = re.fullmatch('(?:默认语言|默認語言|原始语言|原始語言|Original\\s+Language)\\s*[:：]?\\s*(.+)', text, re.I)
        if match and language(match[1]):
            return language(match[1])
    return None

def work_id(entry):
    if entry.get('kind') != 'Movies':
        return None
    match = re.search('\\[tmdbid-(\\d+)\\]', catalog_relative(entry))
    return str(entry.get('canonical_movie_tmdb') or entry.get('movie_tmdb') or (match[1] if match else '')) or None

def same_cut(new, old):
    if not new or not old or (not new.get('duration')) or (not old.get('duration')):
        return False
    return abs(new['duration'] - old['duration']) <= max(60, 0.035 * old['duration'])

def clearer(new, old):
    if not same_cut(new, old):
        return False
    if old.get('hdr') and (not new.get('hdr')):
        return False
    return new.get('width', 0) * new.get('height', 0) >= 1.4 * old.get('width', 0) * old.get('height', 0) and new.get('height', 0) >= old.get('height', 0)

def has_original(value, required=None):
    if not value:
        return None
    required = required or value.get('original_language')
    tracks = [track for track in value.get('audio') or [] if not track.get('commentary')]
    if not required or not tracks:
        return None
    if any((track.get('language') == required for track in tracks)):
        return True
    return None if any((not track.get('language') for track in tracks)) else False

def preferable(new, old):
    """Original audio may break equal-quality ties; never sacrifice known HDR."""
    if not same_cut(new, old):
        return None
    required = new.get('original_language') or old.get('original_language')
    if new.get('original_language') and old.get('original_language') and (new['original_language'] != old['original_language']):
        return None
    old_original = has_original(old, required)
    new_original = has_original(new, required)
    if old_original is True and new_original is not True:
        return None
    if clearer(new, old):
        return 'clearer-video'
    same_or_better = new.get('width', 0) * new.get('height', 0) >= old.get('width', 0) * old.get('height', 0) and new.get('height', 0) >= old.get('height', 0) and (not old.get('hdr') or new.get('hdr'))
    if new_original is True and old_original is False and same_or_better:
        return 'original-audio'
    return None

def parsed_profile(data, preferred=None):
    video = next((stream for stream in data['streams'] if stream.get('codec_type') == 'video'))
    audio = []
    for stream in data['streams']:
        if stream.get('codec_type') != 'audio':
            continue
        tags = stream.get('tags') or {}
        title = tags.get('title') or ''
        known = language(tags.get('language'))
        if not known:
            for marker in ('日语', '日語', '日本語', 'Japanese', '普通话', '普通話', '国语', '國語', 'Mandarin', '粤语', '粵語', 'Cantonese', '英语', '英語', 'English'):
                if re.search(re.escape(marker), title, re.I):
                    known = language(marker)
                    break
        audio.append({'index': stream.get('index'), 'codec': stream.get('codec_name'), 'language': known, 'tag_language': tags.get('language'), 'title': title, 'default': bool((stream.get('disposition') or {}).get('default')), 'commentary': bool((stream.get('disposition') or {}).get('comment') or re.search('commentary|解说|解說|评论|評論|评注|評註', title, re.I))})
    return {'duration': float(data['format']['duration']), 'width': video['width'], 'height': video['height'], 'codec': video.get('codec_name'), 'hdr': video.get('color_transfer') in ('smpte2084', 'arib-std-b67'), 'original_language': preferred, 'audio': audio}

def profile(worker, entry):
    path = worker.root / 'data/video-quality' / (entry['id'] + '.json')
    saved = read(path)
    preferred = original_language(worker, entry)
    current = saved.get('profile_version') == PROFILE_VERSION and saved.get('size') == entry['size'] and (saved.get('source_fid') == entry.get('source_fid'))
    if current and saved.get('profile') and ('audio' in saved['profile']):
        return {**saved['profile'], 'original_language': preferred or saved['profile'].get('original_language')}
    if current and saved.get('retry_at', 0) > time.time():
        return None
    url = _cinema_endpoint('stream') + '/stream/' + entry['id'] + '?' + urllib.parse.urlencode({'key': worker.stream_key})
    local = cache_path(_cinema_cache_path('ready'), entry.get('cache_relative') or entry['relative'])
    if entry.get('ready') and local.is_file() and (local.stat().st_size == entry['size']):
        url = str(local)
    binary = worker.root / 'apps/jellyfin/jellyfin/ffprobe.exe'
    env = {k: v for k, v in os.environ.items() if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
    env['NO_PROXY'] = '*'
    try:
        p = subprocess.run([str(binary), '-v', 'error', '-rw_timeout', '15000000', '-analyzeduration', '3000000', '-probesize', '3000000', '-show_entries', 'format=duration:stream=index,codec_type,codec_name,width,height,pix_fmt,color_transfer:stream_tags=language,title:stream_disposition=default,comment', '-of', 'json', url], capture_output=True, timeout=30, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if p.returncode:
            raise ValueError('probe failed')
        value = parsed_profile(json.loads(p.stdout), preferred)
        write(path, {'profile_version': PROFILE_VERSION, 'source_fid': entry.get('source_fid'), 'size': entry['size'], 'profile': value, 'checked_at': time.time()})
        return value
    except (OSError, ValueError, KeyError, StopIteration, subprocess.TimeoutExpired):
        write(path, {'profile_version': PROFILE_VERSION, 'source_fid': entry.get('source_fid'), 'size': entry['size'], 'retry_at': time.time() + 300, 'error': '视频信息暂时无法读取'})
        return None

def event(root, stage, title, state='running'):
    write(root / 'data/task-events/versions.json', {'worker': 'versions', 'stage': stage, 'title': title, 'state': state, 'updated_at': time.time()})

def retire(worker, old, new, tmdb, tree):
    """Journal user data before recycling; never recycle a playing/downloading source."""
    if old.get('playback_active') or worker.active.get(old['id']) or worker.downloading == old['id']:
        return False
    root = worker.root
    path = root / 'data/auto-upgrades' / (old['id'] + '-' + new['id'] + '.json')
    journal = read(path)
    if journal.get('phase') == 'complete':
        return True
    from cloud_organizer import merge_userdata
    from series_view import literal_sidecars
    a = (root / 'catalog' / catalog_relative(old)).with_suffix('.strm')
    b = (root / 'catalog' / catalog_relative(new)).with_suffix('.strm')
    if not journal:
        items = worker.api('jellyfin', '/Items?Recursive=true&IncludeItemTypes=Movie&Fields=Path')['Items']
        matched = [i for i in items if i.get('Path') and Path(i['Path']).parent in (a.parent, b.parent)]
        records = []
        for user in worker.api('jellyfin', '/Users'):
            ids = ','.join((i['Id'] for i in matched))
            if not ids:
                continue
            values = worker.api('jellyfin', '/Items?Ids=' + ids + '&EnableUserData=true&UserId=' + user['Id'])['Items']
            data = merge_userdata([i.get('UserData') or {} for i in values])
            if meaningful(data):
                records.append({'user_id': user['Id'], 'old_id': matched[0]['Id'], 'new_path': str(b), 'data': data, 'restored': False, 'replacement_entry_id': new['id']})
        journal = {'phase': 'prepared', 'old': old['id'], 'new': new['id'], 'tmdb': tmdb, 'records': records, 'old_entry': dict(old), 'created_at': time.time()}
        write(path, journal)
        write(root / 'data/library-migrations' / ('auto-upgrade-' + old['id'] + '-' + new['id'] + '.json'), {'phase': 'moved', 'records': records})
    parent = str(PurePosixPath(old['remote']).parent)
    fid = old.get('source_fid')
    rows = tree.rows(parent, fresh=True)
    actual = next((r for r in rows if str(r['fid']) == str(fid)), None)
    if actual:
        if actual.get('size') != old['size']:
            raise RuntimeError('旧版对象已改变，停止该项回收')
        tree.q.recycle([actual['fid']])
        tree.cache.pop(parent, None)
    journal['phase'] = 'source-recycled'
    write(path, journal)
    archive = root / 'data/retired-versions' / old['id']
    archive.mkdir(parents=True, exist_ok=True)
    if a.parent.exists():
        for file in literal_sidecars(a):
            if file.suffix.lower() in ('.srt', '.ass', '.ssa', '.vtt'):
                dest = b.with_name(b.stem + file.name[len(a.stem):])
                if not dest.exists():
                    dest.write_bytes(file.read_bytes())
            dest = archive / file.name
            if not dest.exists():
                file.rename(dest)
    cached = bool(old.get('ready') or old.get('partial_bytes_completed') or old.get('queued'))
    delete_cached(_cinema_cache_path('ready'), old['relative'])
    for ext in ('.part', '.invalid'):
        delete_cached(_cinema_cache_path('partial'), old['id'] + ext)
    (root / 'data/range-resume' / (old['id'] + '.json')).unlink(missing_ok=True)
    old.update(ready=False, queued=False, source_missing=True, source_missing_scans=2, catalog_hidden=True, catalog_unavailable_archived=True, catalog_archive=archive.relative_to(root).as_posix(), source_retired_at=time.time(), quality_superseded_by=new['id'])
    hidden = read(root / 'data/catalog-hidden.json')
    hidden[old['remote']] = {'reason': 'automatic-quality-upgrade', 'fid': fid, 'replacement': new['id']}
    write(root / 'data/catalog-hidden.json', hidden)
    worker.catalog_hidden = hidden
    if old.get('cache_blocked') and (not new.get('queued')):
        new['cache_blocked'] = True
    if cached and (not new.get('ready')) and (not new.get('cache_blocked')):
        new.update(queued=True, queued_at=time.time(), cache_upgrade_from=old['id'])
    bindings = read(root / 'data/source-bindings.json')
    if new['id'] in bindings:
        bindings[new['id']]['normalized'] = False
        write(root / 'data/source-bindings.json', bindings)
    worker.save()
    journal.update(phase='complete', completed_at=time.time())
    write(path, journal)
    return True

def upgrade_versions(worker):
    config = read(worker.root / 'data/automation-config.json')
    if not config.get('automatic_versions', False):
        return
    groups = {}
    state = read(worker.root / 'data/auto-version-state.json')
    trees = {}

    def tree_for(entry):
        source = '/百度/movie' if entry['remote'].startswith('/百度/') else '/夸克/movie'
        if source not in trees:
            from cloud_organizer import Tree
            if source.startswith('/百度'):
                from baidu_manage import Baidu
                q = Baidu(worker.root)
            else:
                from quark_manage import Quark
                q = Quark(worker.root)
            trees[source] = Tree(q)
        return trees[source]

    def validate_new(entry):
        tree = tree_for(entry)
        parent = str(PurePosixPath(entry['remote']).parent)
        actual = next((r for r in tree.rows(parent, fresh=True) if str(r['fid']) == str(entry.get('source_fid'))), None)
        if not actual or actual.get('size') != entry['size']:
            raise RuntimeError('新版源文件尚未确认可用，保留旧版')
    for path in (worker.root / 'data/auto-upgrades').glob('*.json'):
        journal = read(path)
        if journal.get('phase') == 'complete':
            continue
        old = worker.entries.get(journal.get('old'))
        new = worker.entries.get(journal.get('new'))
        if not old or not new:
            continue
        try:
            validate_new(new)
            retire(worker, old, new, journal['tmdb'], tree_for(old))
        except (OSError, ValueError, RuntimeError, KeyError, StopIteration):
            pass
    for e in worker.entries.values():
        key = work_id(e)
        if key and (not e.get('source_missing')) and (not e.get('catalog_hidden')) and (not e.get('catalog_unavailable_archived')):
            groups.setdefault(key, []).append(e)
    for tmdb, group in groups.items():
        if len(group) < 2:
            continue
        signature = hashlib.sha256((str(PROFILE_VERSION) + '|' + '|'.join(sorted((e['id'] + ':' + str(e['size']) + ':' + str(e.get('quality_override_at', 0)) for e in group)))).encode()).hexdigest()
        prior = state.get(tmdb, {})
        if prior.get('signature') == signature and prior.get('retry_at', 0) > time.time():
            continue
        title = group[0].get('display_title') or group[0].get('series') or '电影版本'
        event(worker.root, 'quality', title)
        explicit = max((e for e in group if e.get('quality_override_at')), key=lambda e: e['quality_override_at'], default=None)
        try:
            if explicit:
                winner = explicit
                inferior = [e for e in group if e is not winner]
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    profiles = dict(zip((e['id'] for e in group), pool.map(lambda e: profile(worker, e), group)))
                winner = max(group, key=lambda e: (has_original(profiles[e['id']]) is True, (profiles[e['id']] or {}).get('width', 0) * (profiles[e['id']] or {}).get('height', 0), -e.get('created_at', 0)))
                inferior = [e for e in group if e is not winner and preferable(profiles[winner['id']], profiles[e['id']])]
                if len(inferior) != len(group) - 1:
                    state[tmdb] = {'signature': signature, 'status': 'review', 'title': title, 'note': '原声、画质或片长不能确认全面升级；保留源文件，可在版本选项中指定优选。', 'retry_at': time.time() + 300, 'updated_at': time.time()}
                    write(worker.root / 'data/auto-version-state.json', state)
                    continue
            validate_new(winner)
            for e in group:
                e['quality_preferred'] = e is winner
            event(worker.root, 'upgrade', title)
            waiting = False
            for old in inferior:
                if not old.get('source_fid'):
                    waiting = True
                    continue
                if not retire(worker, old, winner, tmdb, tree_for(old)):
                    waiting = True
            state[tmdb] = {'signature': signature, 'status': 'waiting' if waiting else 'complete', 'title': title, 'preferred': winner['id'], 'note': '等待旧版退出播放后替换' if waiting else '已升级优选版本，旧源已进入网盘回收站，记录已迁移', 'retry_at': time.time() + 30, 'updated_at': time.time()}
            worker.save()
            write(worker.root / 'data/auto-version-state.json', state)
        except (OSError, ValueError, RuntimeError, KeyError, StopIteration) as error:
            state[tmdb] = {'signature': signature, 'status': 'error', 'title': title, 'note': '版本处理暂缓：' + type(error).__name__, 'retry_at': time.time() + 120, 'updated_at': time.time()}
            write(worker.root / 'data/auto-version-state.json', state)
    event(worker.root, 'quality', '本轮版本处理完成', 'idle')
