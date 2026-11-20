"""Durable user-started ingestion; bounded retries, no idle rescans."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import logging
import threading
import time
from personal_cinema.runtime_paths import RuntimePath as Path
from library_tasks import read, write
REVISION = 2026110903

def prepare_runtime():
    """Load changed metadata helpers between batches without restarting streams."""
    import importlib, sys
    for name in ('artwork_quality', 'chinese_credits', 'chinese_credits_worker', 'verified_artwork', 'media_attachments', 'subtitle_reconcile', 'episode_metadata_sync', 'movie_groups', 'cloud_sync', 'confirmed_series_episodes'):
        module = sys.modules.get(name)
        if module is None:
            continue
        path = Path(module.__file__)
        stamp = path.stat().st_mtime_ns
        if getattr(module, '_ingestion_loaded_mtime', None) != stamp:
            module = importlib.reload(module)
            module._ingestion_loaded_mtime = stamp

def stage(worker, key, **extra):
    path = worker.root / 'data/manual-ingestion.json'
    from library_activity import LABELS
    write(path, {**read(path), 'state': 'running', 'stage': key, 'note': LABELS.get(key, '处理本批资源'), 'updated_at': time.time(), **extra})

def wait_for_index(worker, entries, allow_stable_alias=False):
    from jellyfin_ingestion import wait_for_index as wait, notify_changed
    result = wait(worker, entries=entries, allow_stable_alias=allow_stable_alias)
    if not result['complete']:
        notify_changed(worker, paths=result['pending_paths'])
        result = wait(worker, entries=entries, allow_stable_alias=allow_stable_alias)
    if not result['complete']:
        worker.library_errors['index'] = '部分新增条目尚未建立索引'
    else:
        worker.library_errors.pop('index', None)
    return result

def execute_batch(worker, requests):
    prepare_runtime()
    root = worker.root
    payloads = [read(p) for p in requests]
    full = any((p.get('action') == 'scan' for p in payloads))
    worker.full_maintenance = full
    worker.ingestion_running = True
    worker.library_errors = {}
    write(root / 'data/manual-ingestion.json', {'state': 'running', 'stage': 'discovery', 'scope': 'full' if full else 'new', 'started_at': time.time(), 'updated_at': time.time(), 'request_ids': [p.get('request_id') for p in payloads]})
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(15):
            path = root / 'data/manual-ingestion.json'
            current = read(path)
            if current.get('state') == 'running':
                write(path, {**current, 'updated_at': time.time()})
    monitor = threading.Thread(target=heartbeat, daemon=True)
    monitor.start()
    try:
        worker.scan()
        run_batch(worker)
        if worker.source_errors:
            for source in list(worker.source_errors):
                try:
                    stage(worker, 'discovery')
                    worker.scan_source(source)
                    worker.source_errors.pop(source, None)
                except (OSError, ValueError, RuntimeError, KeyError):
                    pass
            if not worker.source_errors:
                worker.save()
                worker.library_errors.pop('metadata', None)
                run_batch(worker)
        if worker.source_errors or worker.library_errors:
            current = read(root / 'data/manual-ingestion.json')
            write(root / 'data/manual-ingestion.json', {**current, 'state': 'partial', 'updated_at': time.time(), 'note': '本批仍有任务未完成，已完成结果保留；再次扫描会从日志继续', 'source_errors': list(worker.source_errors), 'library_errors': worker.library_errors})
        result = read(root / 'data/manual-ingestion.json')
    except Exception as error:
        logging.exception('Manual ingestion batch failed (%s)', type(error).__name__)
        result = {**read(root / 'data/manual-ingestion.json'), 'state': 'error', 'updated_at': time.time(), 'note': '本批未完成，可重新点击扫描；已完成的分类与资料会保留', 'error_type': type(error).__name__}
        write(root / 'data/manual-ingestion.json', result)
    finally:
        stop.set()
        monitor.join(timeout=1)
        worker.full_maintenance = False
        worker.ingestion_running = False
    for path, payload in zip(requests, payloads):
        rid = payload.get('request_id')
        if rid:
            p = root / 'data/task-receipts' / (rid + '.json')
            prior = read(p)
            write(p, {**prior, 'request_id': rid, 'state': 'applied' if result['state'] == 'complete' else 'partial' if result['state'] == 'partial' else 'error', 'message': result.get('note', ''), 'updated_at': time.time()})
        path.unlink(missing_ok=True)

def repair_selected(worker, entry_ids, target_ids):
    """Finish a user-confirmed work without discovering or repairing other works."""
    ids = {str(i) for i in entry_ids}
    targets = {str(i) for i in target_ids}
    entries = [worker.entries[i] for i in ids if i in worker.entries and (not worker.entries[i].get('source_missing')) and (not worker.entries[i].get('catalog_hidden'))]
    if not ids or len(entries) != len(ids) or (not targets):
        return {'state': 'partial', 'message': '所选作品的源文件或索引已变化，请刷新工作台后重试'}
    names = ('full_maintenance', 'ingestion_running', 'library_errors', 'source_errors', 'scan_scope')
    missing = object()
    prior = {key: getattr(worker, key, missing) for key in names}
    root = worker.root
    stop = threading.Event()
    worker.full_maintenance = False
    worker.ingestion_running = True
    worker.library_errors = {}
    worker.source_errors = {}
    worker.scan_scope = {'mode': 'selected', 'entry_ids': sorted(ids), 'target_ids': sorted(targets)}
    write(root / 'data/manual-ingestion.json', {'state': 'running', 'stage': 'identity', 'scope': 'selected', 'started_at': time.time(), 'updated_at': time.time(), 'affected_entry_ids': sorted(ids), 'affected_work_ids': sorted(targets), 'note': '正在整理所选作品的海报、演职人员、评分和目录'})

    def heartbeat():
        while not stop.wait(15):
            path = root / 'data/manual-ingestion.json'
            current = read(path)
            if current.get('state') == 'running' and current.get('scope') == 'selected':
                write(path, {**current, 'updated_at': time.time()})
    monitor = threading.Thread(target=heartbeat, daemon=True)
    monitor.start()
    try:
        from confirmed_series_episodes import confirm_review_seasons
        if confirm_review_seasons(worker, entries, targets, explicit=True):
            wait_for_index(worker, entries)
            from library_routing import restore_progress
            restore_progress(worker)
        result = run_batch(worker, entry_ids=ids, target_ids=targets)
        return {**result, 'message': result.get('note', '所选作品整理已结束')}
    except Exception as error:
        logging.exception('Selected work repair deferred (%s)', type(error).__name__)
        result = {**read(root / 'data/manual-ingestion.json'), 'state': 'partial', 'updated_at': time.time(), 'note': '已保留手动身份和已完成资料；本作品仍有步骤待继续', 'error_type': type(error).__name__}
        write(root / 'data/manual-ingestion.json', result)
        return {**result, 'message': result['note']}
    finally:
        stop.set()
        monitor.join(timeout=1)
        for key, value in prior.items():
            if value is missing:
                if hasattr(worker, key):
                    delattr(worker, key)
            else:
                setattr(worker, key, value)

def run_batch(worker, entry_ids=None, target_ids=None):
    selected = entry_ids is not None
    selected_ids = set(entry_ids or [])
    root = worker.root
    config = read(root / 'data/automation-config.json')
    since = config.get('last_ingestion_completed_at', 0)
    stage(worker, 'identity')
    from media_attachments import reconcile_bonus
    from catalog_reclassification import reclassify_context
    stage(worker, 'classify')
    if not selected:
        reconcile_bonus(worker)
        reclassify_context(worker)
    bindings = read(root / 'data/source-bindings.json')
    changed = [e for e in worker.entries.values() if not e.get('source_missing') and (not e.get('catalog_hidden')) and (e['id'] in selected_ids if selected else e.get('created_at', 0) > since or not bindings.get(e['id'], {}).get('normalized'))]
    if not selected:
        from season_release_groups import merge_release_seasons
        merge_release_seasons(worker, changed)
    if changed:
        wait_for_index(worker, changed, allow_stable_alias=True)
    stage(worker, 'identity')
    from metadata_matcher import Matcher
    matcher = Matcher(root)
    matcher.target_ids = set(target_ids or [])
    rows = worker.api('jellyfin', '/Items?Recursive=true&IncludeItemTypes=Movie,Series&Fields=Path,ProviderIds,Overview')['Items']
    credits_state = read(root / 'data/chinese-credits-state.json')
    art = read(root / 'data/verified-artwork-state.json')
    from movie_groups import clean_movie_title
    for item in rows:
        if selected:
            continue
        ident = item['Id']
        title_changed = item.get('Type') == 'Movie' and clean_movie_title(item.get('Name', '')) != item.get('Name', '')
        if title_changed or getattr(worker, 'full_maintenance', False) or (not (item.get('ProviderIds') or {}).get('Tmdb')) or (credits_state.get(ident, {}).get('status') != 'applied') or (art.get(ident, {}).get('status') != 'applied') or (not item.get('ImageTags', {}).get('Primary')):
            matcher.target_ids.add(ident)
    by_folder = {str((Path(i['Path']).parent if i['Type'] == 'Movie' else Path(i['Path'])).absolute()).casefold(): i['Id'] for i in rows if i.get('Path')}
    for entry in changed:
        rel = Path(entry.get('catalog_relative') or entry['relative'])
        parent = (root / 'catalog' / rel.parts[0] / rel.parts[1]).absolute()
        ident = by_folder.get(str(parent).casefold())
        if ident:
            matcher.target_ids.add(ident)
    if matcher.target_ids:
        matcher.config['max_items_per_cycle'] = len(matcher.target_ids)
        for ident in matcher.target_ids:
            if ident in matcher.state:
                matcher.state[ident]['next_attempt_at'] = 0
        matcher.cycle()
    for entry in changed:
        rel = Path(entry.get('catalog_relative') or entry['relative'])
        parent = (root / 'catalog' / rel.parts[0] / rel.parts[1]).absolute()
        native = by_folder.get(str(parent).casefold())
        record = matcher.state.get(native, {})
        if record.get('status') in ('needs_review', 'retry', 'pending'):
            entry['identity_needs_review'] = True
        elif record.get('status') in ('matched', 'already_matched', 'locked') and entry.get('season') != 99:
            entry['identity_needs_review'] = False
    worker.save()
    if not selected:
        from confirmed_series_episodes import confirm_review_seasons
        if confirm_review_seasons(worker, changed, matcher.target_ids):
            wait_for_index(worker, changed)
            from library_routing import restore_progress
            restore_progress(worker)
    from episode_review_metadata import repair_confirmed_reviews
    repair_confirmed_reviews(root, worker, changed)
    refreshed = worker.api('jellyfin', '/Items?Recursive=true&IncludeItemTypes=Movie,Series&Fields=Path,ProviderIds')['Items']
    from title_art_assets import confirmed_batch_work_keys, sync_missing_batch_logos
    logo_work_keys = confirmed_batch_work_keys(root, [worker.entries.get(e['id'], e) for e in changed], refreshed, matcher.state)
    work_keys = {(i['Type'], str((i.get('ProviderIds') or {}).get('Tmdb'))) for i in refreshed if i['Id'] in matcher.target_ids}
    from external_ratings import confirmed_items
    rating_work_keys = {(i['Type'], str((i.get('ProviderIds') or {}).get('Tmdb'))) for i in confirmed_items(root, refreshed) if i['Id'] in matcher.target_ids}
    move_ids = set()
    from catalog_reconcile import reconcile
    for request in (root / 'data/requests').glob('*.json'):
        payload = read(request)
        if payload.get('action') != 'canonical-movie' or (selected and payload.get('id') not in selected_ids):
            continue
        if reconcile(worker, payload):
            move_ids.add(payload['id'])
            request.unlink(missing_ok=True)
    if move_ids:
        wait_for_index(worker, [worker.entries[i] for i in move_ids if i in worker.entries])
    refreshed = worker.api('jellyfin', '/Items?Recursive=true&IncludeItemTypes=Movie,Series&Fields=Path,ProviderIds')['Items']
    matcher.target_ids = {i['Id'] for i in refreshed if i['Id'] in matcher.target_ids or (i['Type'], str((i.get('ProviderIds') or {}).get('Tmdb'))) in work_keys}
    from verified_artwork import sync_artwork
    stage(worker, 'poster', note='正在核对本批海报并优选中文高清原图')
    sync_artwork(root, matcher.client, target_ids=matcher.target_ids)
    logo_results = []
    if logo_work_keys:
        stage(worker, 'poster', note='正在补齐本批作品的艺术片名')
        logo_results = sync_missing_batch_logos(root, matcher.client, refreshed, logo_work_keys)
    if work_keys:
        from external_ratings import confirmed_items, sync_ratings
        stage(worker, 'ratings', note='正在同步本批作品的真实评分')
        rating_items = [i for i in refreshed if (i['Type'], str((i.get('ProviderIds') or {}).get('Tmdb'))) in rating_work_keys]
        sync_ratings(root, rating_items, batch_keys=rating_work_keys)
    worker.last_cloud_normalize = 0
    from cloud_sync import normalize_pending
    stage(worker, 'organize')
    normalize_pending(worker, entry_ids=selected_ids if selected else None)
    from media_attachments import organize_bonus
    organize_bonus(worker, entry_ids=selected_ids if selected else None)
    from subtitle_reconcile import repair_known_subtitles
    repair_known_subtitles(worker, entry_ids=selected_ids if selected else None)
    from episode_metadata_sync import sync_episode_names
    named = sync_episode_names(worker, entry_ids=selected_ids if selected else None)
    from chinese_credits_worker import Worker as Credits
    credits = Credits(root)
    credits.target_ids = matcher.target_ids
    credits.force_retry = True
    if matcher.target_ids:
        stage(worker, 'credits')
        credits.cycle(fetch=True, max_items=len(matcher.target_ids))
    changed_series_folders = {str((root / 'catalog' / Path(e.get('catalog_relative') or e['relative']).parts[0] / Path(e.get('catalog_relative') or e['relative']).parts[1]).absolute()).casefold() for e in changed if e.get('kind') == 'Shows'}
    series_ids = {i['Id'] for i in refreshed if i.get('Type') == 'Series' and (i['Id'] in matcher.target_ids or (i.get('Path') and str(Path(i['Path']).absolute()).casefold() in changed_series_folders))}
    matcher.target_ids.update(series_ids)
    if series_ids:
        from season_artwork import ArtworkWorker
        from season_ratings import RatingWorker
        stage(worker, 'poster')
        ArtworkWorker(root, credits.client).cycle(limit=100, target_ids=series_ids)
        stage(worker, 'ratings')
        RatingWorker(root, credits.client).cycle(limit=100, target_ids=series_ids)
    if matcher.target_ids:
        from person_portraits import sync_portraits
        stage(worker, 'portraits', note='正在补齐本批演职人员头像')
        people_items = worker.api('jellyfin', '/Items?Ids=' + ','.join(matcher.target_ids) + '&Fields=People,ProviderIds,Path').get('Items', [])
        for series_id in series_ids:
            people_items.extend(worker.api('jellyfin', '/Shows/' + series_id + '/Seasons?Fields=People,ProviderIds,Path').get('Items', []))
        sync_portraits(root, credits.client, people_items, max_people=120, max_seconds=90)
        from person_portrait_worker import enqueue_works
        enqueue_works(root, [i['Id'] for i in people_items])
    from media_probe import queue_entries
    stage(worker, 'probe', note='正在安排新增、换源及缺失参数的视频信息读取')
    queue_entries(root, worker.entries, entry_ids=selected_ids if selected else None)
    stage(worker, 'poster')
    from episode_artwork import refresh_new_episode_artwork
    episode_targets = {row['id'] for row in named}
    episode_entries = [e for e in changed if e.get('kind') == 'Shows']
    if episode_entries:
        shorts = {e['id'][:12] for e in episode_entries}
        for item in worker.api('jellyfin', '/Items?Recursive=true&IncludeItemTypes=Episode&Fields=Path')['Items']:
            if any((short in item.get('Path', '') for short in shorts)):
                episode_targets.add(item['Id'])
    refresh_new_episode_artwork(root, credits.client, limit=100, target_ids=None if getattr(worker, 'full_maintenance', False) else episode_targets)
    from movie_groups import sync_collections
    sync_collections(worker, entry_ids=selected_ids if selected else None)
    collection_routes = getattr(worker, 'collection_route_pending', set())
    if selected:
        collection_routes = set(collection_routes) & selected_ids
    if collection_routes:
        stage(worker, 'organize', note='正在按已确认的系列信息归并电影目录')
        normalize_pending(worker, entry_ids=collection_routes)
    from cloud_cleanup import cleanup_old_directories
    if not selected:
        cleanup_old_directories(worker)
    stage(worker, 'verify')
    pending = audit_pending(worker, matcher.target_ids, changed)
    retry_credits = {g['id'] for g in pending if not g['id'].startswith('pending-') and any((s['key'] == 'credits' and s['state'] != 'done' for s in g['steps']))}
    retry_art = {g['id'] for g in pending if not g['id'].startswith('pending-') and any((s['key'] == 'poster' and s['state'] != 'done' for s in g['steps']))}
    if retry_credits:
        stage(worker, 'credits')
        credits.target_ids = retry_credits
        credits.cycle(fetch=True, max_items=len(retry_credits))
    if retry_art:
        stage(worker, 'poster')
        sync_artwork(root, matcher.client, target_ids=retry_art)
    stage(worker, 'verify')
    pending = audit_pending(worker, matcher.target_ids, changed)
    if pending:
        worker.library_errors['metadata'] = '仍有' + str(len(pending)) + '部作品资料待完成'
    else:
        worker.library_errors.pop('metadata', None)
    complete = not worker.source_errors and (not getattr(worker, 'library_errors', {}))
    if complete and (not selected):
        config['last_ingestion_completed_at'] = time.time()
        write(root / 'data/automation-config.json', config)
    previous = read(root / 'data/manual-ingestion.json')
    result = {**previous, 'state': 'complete' if complete else 'partial', 'stage': 'complete' if complete else 'verify', 'updated_at': time.time(), 'changed_files': len(changed), 'affected_works': len(matcher.target_ids), 'scan_scope': getattr(worker, 'scan_scope', {}), 'affected_work_ids': sorted(matcher.target_ids), 'affected_entry_ids': [e['id'] for e in changed], 'logo_results': logo_results, 'pending_works': [{'title': g['title'], 'state': g['state']} for g in pending], 'source_errors': list(worker.source_errors), 'library_errors': dict(worker.library_errors), 'note': ('所选作品的资料与目录整理已完成' if selected else '本批检查已结束；定时扫描关闭') if complete else '已完成结果已保留；下方列出的资料仍待处理'}
    write(root / 'data/manual-ingestion.json', result)
    return result

def audit_pending(worker, target_ids=None, entries=None):
    from task_center_api import Center
    rows = worker.api('jellyfin', '/Items?Recursive=true&IncludeItemTypes=Movie,Series,Episode&Fields=Path,ProviderIds,Overview')['Items']
    snapshot = Center(worker.root).snapshot(worker.entries, rows)
    affected = None if target_ids is None else set(target_ids)
    if affected is not None:
        for e in entries or []:
            folder = Path(e.get('catalog_relative') or e.get('relative', '')).parent.as_posix()
            if e.get('kind') == 'Shows':
                folder = e.get('series', folder)
            affected.add('pending-' + hashlib.md5(folder.encode()).hexdigest())
    return [g for g in snapshot['ingestion'] if g['state'] != 'complete' and (affected is None or g['id'] in affected)]
