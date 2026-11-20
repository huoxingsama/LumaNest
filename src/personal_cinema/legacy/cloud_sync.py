"""Incremental cloud organization after library identification has completed."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import time
import logging
from library_tasks import read, write
from source_policy import manager_for, journal_path, source_entries, plan_id, _permission, _config

def normalize_pending(worker, entry_ids=None):
    if not _config(worker.root).get_effective().get('organizing.enabled', False):
        return
    if not read(worker.root / 'data/automation-config.json').get('enabled', True):
        return
    from cloud_cleanup import retry_due
    if entry_ids is None:
        retry_due(worker)
    recovery_only = bool(read(worker.root / 'data/automation-config.json').get('manual_only') and read(worker.root / 'data/manual-ingestion.json').get('state') != 'running')
    if recovery_only and (getattr(worker, 'ingestion_running', False) or getattr(worker, 'playback_present', False) or getattr(worker, 'playback_status_unknown', False) or getattr(worker, 'steam_downloading', False)):
        return
    if entry_ids is None and time.time() - getattr(worker, 'last_cloud_normalize', 0) < 120:
        return
    worker.last_cloud_normalize = time.time()
    if entry_ids is None and (not recovery_only) and (time.time() - getattr(worker, 'last_empty_cleanup', 0) > 300):
        from cloud_cleanup import cleanup_old_directories
        cleanup_old_directories(worker)
        worker.last_empty_cleanup = time.time()
    bindings = read(worker.root / 'data/source-bindings.json')
    pending = [e for e in worker.entries.values() if not recovery_only and (entry_ids is None or e['id'] in entry_ids) and (not e.get('source_missing')) and (not e.get('catalog_hidden')) and (not e.get('source_rename_pending')) and (not e.get('identity_needs_review')) and (e.get('season') != 99) and (not e.get('playback_active')) and (not getattr(worker, 'active', {}).get(e['id'])) and (not bindings.get(e['id'], {}).get('normalized'))]
    from cloud_organizer import make_plan, apply_plan, metadata, progress, resume_plan
    meta = None
    retry_file = worker.root / 'data/cloud-organizer-retry.json'
    retries = read(retry_file)
    for entry in source_entries(worker.root):
        if not entry.get('enabled', True) or entry.get('kind') != 'openlist' or not entry.get('allow_write') or entry.get('driver') not in ('Quark', 'BaiduNetdisk'):
            continue
        source = entry['path']
        ids = {e['id'] for e in pending if e.get('remote', '').startswith(source + '/')}
        journal = read(journal_path(worker.root, source))
        unfinished = journal.get('phase') != 'complete' and any((op.get('state') in ('applying', 'done') for op in journal.get('operations', [])))
        if entry_ids is not None and unfinished:
            journal_ids = {op.get('entry_id') or op.get('video_entry_id') for op in journal.get('operations', [])}
            if not journal_ids or not journal_ids.issubset(set(entry_ids)):
                if ids:
                    worker.library_errors[source] = '当前云盘还有其他作品的整理日志，完成后可继续本作品'
                continue
        if not ids and (not unfinished):
            continue
        retry = retries.get(source, {})
        if retry.get('plan_created_at') != journal.get('created_at'):
            retry = {}
        if recovery_only and (retry.get('attempts', 0) >= 6 or retry.get('next_attempt_at', 0) > time.time()):
            continue
        try:
            q = manager_for(worker, source)
            approved = read(worker.root / 'data/cloud-organizer-approvals.json').get(entry['id'], {})
            if approved.get('plan_id') == plan_id(journal):
                q.approval = approved['plan_id']
                unfinished = bool(journal.get('operations') and journal.get('phase') != 'complete')
            if unfinished:
                active = any((worker.entries.get(op.get('entry_id'), {}).get('playback_active') or getattr(worker, 'active', {}).get(op.get('entry_id')) for op in journal['operations']))
                if active:
                    progress(worker.root, 'organize', '等待当前播放结束后继续整理', state='waiting')
                    continue
                resume_plan(worker, source, manager=q)
                getattr(worker, 'library_errors', {}).pop(source, None)
                if source in retries:
                    retries.pop(source)
                    write(retry_file, retries)
            if not ids:
                continue
            if meta is None:
                meta = metadata(worker.root)
            j, by_id, by_path, movies = meta
            plan, tree = make_plan(worker.root, q, worker, j, by_id, by_path, movies, entry_ids=ids)
            if plan['operations']:
                try:
                    _permission(worker.root, entry, approval=q.approval)
                except RuntimeError:
                    plan['phase'] = 'preview'
                    write(journal_path(worker.root, source), plan)
                    progress(worker.root, 'preview', '整理计划已生成，等待预览确认', state='waiting', source_id=entry['id'])
                    continue
                apply_plan(worker.root, q, worker, plan, tree)
                with worker.lock:
                    worker.source_bindings.apply(worker.entries)
                    worker.save()
                getattr(worker, 'library_errors', {}).pop(source, None)
                if source in retries:
                    retries.pop(source)
                    write(retry_file, retries)
            elif plan['review']:
                progress(worker.root, 'identify', '新资源等待作品身份确认', 0, len(plan['review']), state='waiting')
        except (OSError, ValueError, RuntimeError, KeyError, StopIteration) as error:
            if not hasattr(worker, 'library_errors'):
                worker.library_errors = {}
            worker.library_errors[source] = type(error).__name__
            current = read(journal_path(worker.root, source))
            count = (retry.get('attempts', 0) if retry.get('plan_created_at') == current.get('created_at') else 0) + 1
            retries[source] = {'plan_created_at': current.get('created_at'), 'attempts': count, 'next_attempt_at': time.time() + min(1800, 30 * 2 ** min(count - 1, 6)), 'phase': current.get('phase'), 'error_type': type(error).__name__, 'updated_at': time.time()}
            write(retry_file, retries)
            from quark_manage import QuarkError
            detail = str(error)[:180] if isinstance(error, QuarkError) else type(error).__name__
            progress(worker.root, 'error', '云盘整理待继续：' + detail, state='error', retry_at=retries[source]['next_attempt_at'], phase=current.get('phase'))
            logging.warning('Cloud incremental normalization deferred (%s): %s', current.get('phase'), detail)
