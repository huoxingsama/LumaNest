"""Journalled Quark media organization: one inventory, bounded mutations, one audit."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import collections
import concurrent.futures
import json
import re
import shutil
import threading
import time
from pathlib import PurePosixPath
from personal_cinema.runtime_paths import RuntimePath as Path
from cache_worker import Worker
from cache_policy import cache_path, delete_cached
from library_routing import catalog_relative, meaningful
from library_tasks import read, write
from metadata_matcher import Jellyfin
from quark_manage import Quark, QuarkError
from series_view import literal_sidecars
from subtitle_rules import classify as subtitle_info, suffix as subtitle_suffix
from movie_groups import clean_movie_title, collection_names, movie_group
from subtitle_reconcile import subtitle_matches
ROOT = Path(_cinema_path(''))
WORK = Path(__file__).parent
AD = re.compile('广告|廣告|引流|扫码|掃碼|加群|进群|進群|公众号|公眾號|微信|QQ群|领取|領取|更多资源|更多資源|https?[:：]|www\\.|免费资源|免費資源', re.I)
SUB = {'.ass', '.ssa', '.srt', '.vtt', '.sub', '.idx', '.smi'}

def clean(text, limit=110):
    text = re.sub('[<>:"/\\\\|?*#\\x00-\\x1f：]', ' ', str(text))
    return re.sub('\\s+', ' ', text).strip(' .')[:limit]

def progress(root, stage, title, done=0, total=0, state='running', **extra):
    value = {'worker': 'organizer', 'stage': stage, 'title': title, 'state': state, 'updated_at': time.time(), 'count': done, 'total': total, **extra}
    write(root / 'data/task-events/organizer.json', value)
    write(root / 'data/cloud-organizer-status.json', value)

class Tree:

    def __init__(self, q):
        self.q = q
        self.source_root = q.source_root
        self.dirs = {self.source_root: q.movie_root_fid}
        self.cache = {}

    def rows(self, path, fresh=False):
        if fresh or path not in self.cache:
            self.cache[path] = self.q.listing(self.directory(path))
        return self.cache[path]

    def directory(self, path):
        if (path != self.source_root and not path.startswith(self.source_root + '/')) or '..' in path.split('/'):
            raise QuarkError('目录超出所选来源。')
        if path in self.dirs:
            return self.dirs[path]
        parent = str(PurePosixPath(path).parent)
        name = PurePosixPath(path).name
        row = next((r for r in self.rows(parent) if r.get('file_name') == name and r.get('dir')), None)
        if not row:
            raise QuarkError('目录未确认：' + name)
        self.dirs[path] = row['fid']
        return row['fid']

    def ensure(self, path):
        if (path != self.source_root and not path.startswith(self.source_root + '/')) or '..' in path.split('/'):
            raise QuarkError('目录超出所选来源。')
        if path in self.dirs:
            return self.dirs[path]
        parent = str(PurePosixPath(path).parent)
        fid = self.ensure(parent)
        result = self.q.mkdir(fid, PurePosixPath(path).name)
        self.dirs[path] = result
        self.cache.pop(parent, None)
        return result

def metadata(root):
    j = Jellyfin(root)
    rows = j.json('/Items?Recursive=true&IncludeItemTypes=Movie,Series,Episode,BoxSet&Fields=Path,ProviderIds')['Items']
    by_id = {r['Id']: r for r in rows}
    by_path = {str(Path(r['Path']).absolute()).casefold(): r for r in rows if r.get('Path')}
    movies = {str(Path(r['Path']).parent.absolute()).casefold(): r for r in rows if r.get('Type') == 'Movie' and r.get('Path')}
    return (j, by_id, by_path, movies)

def merge_userdata(rows):
    ordered = sorted(rows, key=lambda d: (d.get('LastPlayedDate') or '', d.get('PlaybackPositionTicks') or 0), reverse=True)
    result = {}
    fields = ('PlaybackPositionTicks', 'PlayCount', 'IsFavorite', 'Likes', 'Rating', 'LastPlayedDate', 'Played')
    for d in reversed(ordered):
        result.update({k: d[k] for k in fields if k in d})
    result['IsFavorite'] = any((d.get('IsFavorite') for d in rows))
    result['PlayCount'] = max((d.get('PlayCount') or 0 for d in rows), default=0)
    result['Played'] = any((d.get('Played') for d in rows)) and (not bool(result.get('PlaybackPositionTicks')))
    return result


def make_plan(root, q, w, j, by_id, by_path, movies, entry_ids=None):
    tree = Tree(q)
    source_root = tree.source_root
    entries = [e for e in w.entries.values() if e.get('remote', '').startswith(source_root + '/') and (not e.get('source_missing')) and (not e.get('catalog_unavailable_archived'))]
    if entry_ids is not None:
        entries = [e for e in entries if e['id'] in entry_ids]
    franchise = read(root / 'data/franchise-movies.json').get('海贼王', {})
    names = collection_names(by_id.values())
    movie_numbers = {r['provider_ids']['Tmdb']: int(n) for n, r in franchise.items()}
    naruto = sorted([r for r in movies.values() if str(r.get('ProviderIds', {}).get('TmdbCollection')) == '23616'], key=lambda r: r.get('ProductionYear') or 9999)
    for n, r in enumerate(naruto, 1):
        movie_numbers[str(r['ProviderIds']['Tmdb'])] = n
    operations = []
    models = {}
    review = []
    grouped = collections.defaultdict(list)
    anchors = read(root / 'data/cloud-normalization-rules.json')
    for e in entries:
        remote = e['remote']
        parent = str(PurePosixPath(remote).parent)
        grouped[parent].append(e)
        rel = Path(catalog_relative(e))
        local = (root / 'catalog' / rel).with_suffix('.strm')
        if e['kind'] == 'Movies':
            item = movies.get(str(local.parent.absolute()).casefold())
            tmdb = str((item or {}).get('ProviderIds', {}).get('Tmdb') or '')
            if not item or not tmdb:
                review.append({'entry': e['id'], 'reason': '作品身份尚未确认'})
                continue
            title = clean_movie_title(item['Name'])
            year = item.get('ProductionYear')
            family, branch = movie_group(root, item, franchise, names)
            if not family and e.get('franchise_family'):
                family, branch = (e['franchise_family'], '电影')
            if e.get('media_role') == 'special' and family:
                branch = '电视特别篇'
            number = movie_numbers.get(tmdb)
            tail = re.sub('^.*?剧场版[：:]?', '', title).strip()
            leaf = clean((f'剧场版{number:02d} ' if number else '') + tail + (f' ({year})' if year else ''))
            base = clean((family + ' ' if family and (not leaf.startswith(family)) and (not branch.endswith('系列')) else '') + leaf)
            target = source_root + '/' + (clean(family) + '/' + clean(branch) + '/' if family else '') + leaf
            key = 'Movie:' + tmdb
            model = {'key': key, 'family': clean(family) if family else leaf, 'target_dir': target, 'base': base, 'tmdb': tmdb, 'title': title, 'year': year, 'kind': 'Movies', 'entry': e}
            anchors[target] = {'kind': 'Movies', 'series': rel.parts[1], 'title': title}
        else:
            item = by_id.get(e.get('jellyfin_item_id')) or by_path.get(str(local.absolute()).casefold())
            series = by_id.get((item or {}).get('SeriesId') or e.get('series_id'))
            if not series:
                folder = (root / 'catalog' / rel.parts[0] / rel.parts[1]).absolute()
                series = by_path.get(str(folder).casefold())
            if not series:
                review.append({'entry': e['id'], 'reason': '剧集身份尚未确认'})
                continue
            title = series['Name']
            family = clean(title)
            episode = e.get('absolute_episode') or e.get('episode')
            season = e.get('season')
            episode_title = e.get('episode_title') or (item or {}).get('Name') or ''
            episode_title = re.sub('^第\\s*\\d+\\s*集\\s*[·.、：:]?\\s*', '', episode_title)
            if e.get('is_extra') or season in (0, 90, 91):
                category = '联动特别篇' if season == 90 else '动画短篇' if season == 91 else '电视特别篇'
                name = clean(episode_title or '特别篇' + str(episode))
                target = f'{source_root}/{family}/{category}/{name}'
                base = clean(f'{family} - S{season or 0:02d}E{episode:02d} - {name}')
                anchors[f'{source_root}/{family}/{category}'] = {'kind': 'Shows', 'series': rel.parts[1], 'title': title, 'catalog_kind': rel.parts[0], 'extra_title': e.get('extra_title'), 'is_extra': True}
            else:
                block = f'{(episode - 1) // 100 * 100 + 1:04d}-{((episode - 1) // 100 + 1) * 100:04d}' if e.get('absolute_episode') else f'第{season or 1:02d}季'
                target = f'{source_root}/{family}/正传/{block}'
                code = f'E{episode:04d}' if e.get('absolute_episode') else f'S{season or 1:02d}E{episode:02d}'
                base = clean(f'{family} - {code}' + (' - ' + episode_title if episode_title and (not re.search('资料待|编号待', episode_title)) else ''), 135)
            key = 'Episode:' + str(series['Id']) + ':' + str(season) + ':' + str(episode)
            model = {'key': key, 'family': family, 'target_dir': target, 'base': base, 'kind': 'Shows', 'entry': e}
            anchors[f'{source_root}/{family}/正传'] = {'kind': 'Shows', 'series': rel.parts[1], 'title': title, 'catalog_kind': rel.parts[0]}
        models[e['id']] = model
    variants = collections.defaultdict(list)
    for model in models.values():
        variants[model['key']].append(model)
    for group in variants.values():
        group.sort(key=lambda m: (not bool(m['entry'].get('quality_preferred')), bool(m['entry'].get('catalog_hidden')), -m['entry'].get('created_at', 0), -m['entry']['size']))
        for n, m in enumerate(group):
            if n:
                m['target_dir'] = group[0]['target_dir'] + '/备用版本'
                m['base'] = group[0]['base'] + ' - ' + str(round(m['entry']['size'] / 1000000.0)) + 'MB'
    # Planning only inventories and computes names. Destination directories are
    # created by apply_plan after the exact plan has permission to execute.
    subtitle_count = 0
    for number, (parent, group) in enumerate(grouped.items(), 1):
        progress(root, 'inventory', '读取目录与字幕：' + PurePosixPath(parent).name, number, len(grouped))
        rows = tree.rows(parent)
        by_name = {r['file_name']: r for r in rows}
        side_rows = [{**r, '_parent': parent} for r in rows]
        for directory in rows:
            if directory.get('dir') and re.search('字幕|subtitles?|subs', directory['file_name'], re.I) and (not AD.search(directory['file_name'])):
                nested = parent + '/' + directory['file_name']
                side_rows.extend(({**r, '_parent': nested} for r in tree.rows(nested)))
        eligible = [e for e in group if e['id'] in models]
        subtitle_names = {e['id']: [PurePosixPath(e['remote']).name] for e in eligible}
        for e in eligible:
            row = by_name.get(PurePosixPath(e['remote']).name)
            if not row or row.get('dir') or row.get('size') != e['size']:
                review.append({'entry': e['id'], 'reason': '当前网盘对象已变化'})
                continue
            m = models[e['id']]
            stem = PurePosixPath(e['remote']).stem
            target = m['target_dir'] + '/' + m['base'] + PurePosixPath(e['remote']).suffix.lower()
            occupied = next((other for other in w.entries.values() if other['id'] != e['id'] and other.get('remote') == target and (not other.get('source_missing')) and (not other.get('source_retired_at'))), None)
            if occupied:
                m['target_dir'] += '/备用版本'
                m['base'] += ' - ' + e['id'][:6]
                target = m['target_dir'] + '/' + m['base'] + PurePosixPath(e['remote']).suffix.lower()
            operations.append({'entry_id': e['id'], 'fid': row['fid'], 'size': row['size'], 'source': e['remote'], 'target': target, 'old_name': row['file_name'], 'source_parent': parent, 'kind': 'video', 'state': 'pending'})
            for side in side_rows:
                if side.get('dir') or side['fid'] == row['fid']:
                    continue
                ext = PurePosixPath(side['file_name']).suffix.lower()
                same = PurePosixPath(side['file_name']).stem == stem or side['file_name'].startswith(stem + '.')
                if ext in SUB:
                    owners = subtitle_matches(side['file_name'], subtitle_names)
                    if len(owners) > 1 or (owners and e['id'] not in owners):
                        continue
                    if owners:
                        same = True
                if not same and e['kind'] == 'Shows' and (ext in SUB):
                    from catalog_intelligence import episode_label
                    label = episode_label(side['file_name'], True)
                    same = bool(label and label['episode'] == (e.get('absolute_episode') or e.get('episode')))
                if not same and (not (parent != source_root and len(eligible) == 1 and (ext not in {'.mp4', '.mkv', '.avi', '.mov', '.ts', '.m4v', '.m2ts', '.rmvb', '.rm'}))):
                    continue
                side_parent = side['_parent']
                side_remote = side_parent + '/' + side['file_name']
                info = None
                if ext in SUB and side.get('size', 0) <= 10000000 and (ext not in ('.idx', '.sub')):
                    try:
                        with w.http.open(w.cloud_request(side_remote), timeout=20) as response:
                            raw = response.read(3000000)
                        info = subtitle_info(raw, side['file_name'])
                        subtitle_count += 1
                    except OSError:
                        info = {'recognized': False, 'label': '未识别字幕', 'language': 'und', 'secondary': False, 'hearing_impaired': False, 'traditional': False}
                    name = m['base'] + subtitle_suffix(info, ext, default=info['language'] == 'chi' and (not info['secondary']) and (not info.get('traditional')))
                else:
                    name = m['base'] + side['file_name'][len(stem):] if same else clean(side['file_name'], 150)
                operations.append({'video_entry_id': e['id'], 'fid': side['fid'], 'size': side.get('size', 0), 'source': side_remote, 'target': m['target_dir'] + '/' + name, 'old_name': side['file_name'], 'source_parent': side_parent, 'kind': 'subtitle' if ext in SUB else 'attachment', 'subtitle': info, 'state': 'pending'})
    ads = []
    for parent, rows in list(tree.cache.items()):
        for row in rows:
            if row.get('dir') and AD.search(row['file_name']):
                ads.append({'fid': row['fid'], 'source': parent + '/' + row['file_name'], 'reason': 'advertisement-directory'})
    seen = set()
    unique = []
    targets = set()
    subtitle_groups = collections.defaultdict(list)
    for op in operations:
        if op.get('subtitle'):
            subtitle_groups[op['video_entry_id']].append(op)
    for group in subtitle_groups.values():
        eligible = [op for op in group if op['subtitle']['language'] == 'chi' and (not op['subtitle']['secondary'])]
        default = min(eligible, key=lambda op: (op['subtitle'].get('traditional', False), op['subtitle'].get('bilingual', False), op['target']), default=None)
        for op in group:
            op['target'] = op['target'].replace('.default.', '.')
            if op is default:
                p = PurePosixPath(op['target'])
                op['target'] = str(p.with_name(p.stem + '.default' + p.suffix))
    for op in operations:
        if op['fid'] in seen:
            continue
        seen.add(op['fid'])
        if op['target'] in targets:
            p = PurePosixPath(op['target'])
            op['target'] = str(p.with_name(p.stem + ' - ' + op['fid'][:6] + p.suffix))
        targets.add(op['target'])
        unique.append(op)
    plan = {'source_id': q.entry['id'], 'created_at': time.time(), 'phase': 'planned', 'operations': unique, 'review': review, 'ad_directories': ads, 'directory_fids': tree.dirs, 'anchors': anchors, 'subtitles_detected': subtitle_count}
    write(root / 'data' / ('cloud-organizer-plan' + ('-' + q.slug if hasattr(q, 'slug') else '') + '.json'), plan)
    return (plan, tree)

def synchronize_absolute_rules(root, source_root, ops):
    rules = read(root / 'data/absolute-series.json', [])
    for r in list(rules):
        if r.get('provider_ids', {}).get('Tmdb') not in ('37854', '46260'):
            continue
        if not r['source_root'].startswith(source_root + '/'):
            continue
        new = json.loads(json.dumps(r))
        new['source_root'] = source_root + ('/海贼王' if r['provider_ids']['Tmdb'] == '37854' else '/火影忍者')
        names = {PurePosixPath(op['source']).name: PurePosixPath(op['target']).name for op in ops if op.get('entry_id')}
        new['extra_files'] = {names.get(k, k): v for k, v in new.get('extra_files', {}).items()}

        def key(x):
            return (x.get('source_root'), x.get('source_min', 1), x.get('source_max', 99999))
        existing = next((x for x in rules if key(x) == key(new)), None)
        if existing is None:
            rules.append(new)
        else:
            existing['extra_files'] = {**new.get('extra_files', {}), **existing.get('extra_files', {})}
    write(root / 'data/absolute-series.json', rules)

def apply_plan(root, q, w, plan, tree):
    q.require_write()
    for op in plan.get('operations', []):
        for key in ('source', 'target'):
            path = op.get(key, '')
            if not path.startswith(q.source_root + '/') or '..' in path.split('/') or '\\' in path:
                raise QuarkError('计划包含来源目录外操作。')
    plan['directory_fids'] = tree.dirs
    source_root = tree.source_root
    bindings = read(root / 'data/source-bindings.json')
    ops = plan['operations']
    groups = collections.defaultdict(list)
    for op in ops:
        if op.get('entry_id'):
            prior = bindings.get(op['entry_id'], {})
            current = op['target'] if op['state'] == 'done' else op['source']
            bindings[op['entry_id']] = {**prior, 'fid': op['fid'], 'size': op['size'], 'original_remote': prior.get('original_remote') or op['source'], 'previous_paths': list(dict.fromkeys([*prior.get('previous_paths', []), prior.get('remote') or op['source'], op['source']])), 'remote': current, 'phase': 'done' if op['state'] == 'done' else 'bound', 'normalized': op['state'] == 'done'}
        groups[str(PurePosixPath(op['target']).parent)].append(op)
    plan['phase'] = 'applying'
    journal = root / 'data' / ('cloud-organizer-plan' + ('-' + q.slug if hasattr(q, 'slug') else '') + '.json')
    write(journal, plan)
    write(root / 'data/source-bindings.json', bindings)
    write(root / 'data/cloud-normalization-rules.json', plan['anchors'])
    if any((op.get('entry_id') and op['state'] == 'done' for op in ops)):
        with w.lock:
            w.source_bindings.apply(w.entries)
            w.save()
    synchronize_absolute_rules(root, source_root, ops)
    done = sum((op['state'] == 'done' for op in ops))
    errors = []
    for target, group in groups.items():
        dest = tree.ensure(target)
        pending = [op for op in group if op['state'] != 'done']
        for offset in range(0, len(pending), 35):
            batch = pending[offset:offset + 35]
            progress(root, 'organize', '正在归位：' + target[len(source_root + '/'):], done, len(ops))
            for op in batch:
                op['state'] = 'applying'
            write(root / 'data' / ('cloud-organizer-plan' + ('-' + q.slug if hasattr(q, 'slug') else '') + '.json'), plan)
            if hasattr(q, 'relocate'):
                q.relocate(batch, dest)
            else:

                def rename(op):
                    name = PurePosixPath(op['target']).name
                    if name != op['old_name']:
                        q.rename(op['fid'], name)
                    op['renamed'] = True
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    futures = [pool.submit(rename, op) for op in batch]
                    for f in futures:
                        f.result()
                moving = [op['fid'] for op in batch if tree.directory(op['source_parent']) != dest]
                if moving:
                    q.move(moving, dest)
            for op in batch:
                op['state'] = 'done'
                done += 1
                if op.get('entry_id'):
                    bindings[op['entry_id']].update(remote=op['target'], phase='done', normalized=True)
            write(root / 'data/source-bindings.json', bindings)
            write(root / 'data' / ('cloud-organizer-plan' + ('-' + q.slug if hasattr(q, 'slug') else '') + '.json'), plan)
            with w.lock:
                w.source_bindings.apply(w.entries)
                w.save()
    plan['phase'] = 'verifying'
    write(journal, plan)
    progress(root, 'verify', '统一核对目标文件', done, len(ops))
    for target, group in groups.items():
        for attempt in range(3):
            missing = []
            try:
                listing = {str(row['fid']): row for row in q.listing(tree.dirs[target])}
                for op in group:
                    actual = listing.get(str(op['fid']))
                    if not actual or actual.get('file_name') != PurePosixPath(op['target']).name or actual.get('size', 0) != op['size']:
                        missing.append(op['fid'])
                if not missing:
                    break
            except (OSError, ValueError, RuntimeError) as error:
                if attempt == 2:
                    plan['verification_pending'] = {'directory': target, 'error_type': type(error).__name__, 'updated_at': time.time()}
                    write(journal, plan)
                    raise
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
        errors.extend(missing)
    if errors:
        raise QuarkError('最终核对有 ' + str(len(errors)) + ' 个对象待处理；任务日志已保留')
    plan.pop('verification_pending', None)
    from cloud_cleanup import queue_plan_cleanup, cleanup_old_directories
    queue_plan_cleanup(root, plan, source_root)
    write(journal, plan)
    progress(root, 'cleanup', '清理已搬空目录并更新网盘目录显示', done, len(ops))
    removed = cleanup_old_directories(w, seed=False)
    deleted = list(plan.get('removed_directories') or [])
    deleted.extend(({'source': path, 'reason': 'empty-after-organization'} for path in removed if not any((r.get('source') == path for r in deleted))))
    plan.update(phase='complete', completed_at=time.time(), removed_directories=deleted, verified_files=len(ops))
    write(root / 'data/cloud-directory-map.json', {**read(root / 'data/cloud-directory-map.json'), **tree.dirs})
    write(root / 'data' / ('cloud-organizer-plan' + ('-' + q.slug if hasattr(q, 'slug') else '') + '.json'), plan)
    progress(root, 'complete', '网盘分类、规范命名和字幕整理完成', done, len(ops), 'complete', removed_directories=len(deleted), subtitles=plan['subtitles_detected'])

def resume_plan(worker, source=None, manager=None):
    """Reconcile stable object IDs before resuming this selected source only."""
    root = worker.root
    if manager is None:
        from source_policy import manager_for
        manager = manager_for(worker, source)
    q = manager
    journal = root / 'data' / ('cloud-organizer-plan' + ('-' + q.slug if hasattr(q, 'slug') else '') + '.json')
    plan = read(journal)
    if not plan.get('operations') or plan.get('phase') == 'complete':
        return {'state': 'idle', 'resumed': 0}
    tree = Tree(q)
    tree.dirs.update(plan.get('directory_fids') or {})
    if hasattr(q, 'paths'):
        for path, fid in tree.dirs.items():
            if path == q.source_root or path.startswith(q.source_root + '/'):
                q.paths[str(fid)] = q.native_root + path[len(q.source_root):]
    for path, fid in tree.dirs.items():
        if path == q.source_root or path.startswith(q.source_root + '/'):
            q._remember(fid, path, True)
    listings = {}

    def rows(path):
        if path not in listings:
            fid = tree.dirs.get(path)
            if fid is None:
                try:
                    fid = tree.directory(path)
                except QuarkError:
                    return {}
            listings[path] = {str(row['fid']): row for row in q.listing(fid)}
        return listings[path]
    for op in plan['operations']:
        target_parent = str(PurePosixPath(op['target']).parent)
        actual = rows(target_parent).get(str(op['fid']))
        if actual and actual.get('file_name') == PurePosixPath(op['target']).name and (actual.get('size', 0) == op['size']):
            op['state'] = 'done'
            continue
        actual = rows(op['source_parent']).get(str(op['fid']))
        if not actual or actual.get('size', 0) != op['size']:
            raise QuarkError('恢复时文件身份或位置尚未确认，保留操作日志')
        op['old_name'] = actual['file_name']
        op['state'] = 'pending'
    write(journal, plan)
    apply_plan(root, q, worker, plan, tree)
    with worker.lock:
        worker.source_bindings.apply(worker.entries)
        worker.save()
    return {'state': 'complete', 'resumed': len(plan['operations']), 'verified_files': plan.get('verified_files', 0)}

