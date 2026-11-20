"""Continuously drain a durable work-scoped portrait backlog; no cloud scans."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import argparse
import contextlib
import os
import re
import secrets
import subprocess
import sys
import time
from personal_cinema.runtime_paths import RuntimePath as Path
from library_tasks import read, write, observe
POLL_SECONDS = 15
RECOVER_SECONDS = 300
WORK_BATCH = 20
PERSON_BATCH = 120
IDENT = re.compile('[a-f0-9]{32}')

@contextlib.contextmanager
def _lock(path, blocking=False):
    handle = path.open('a+b')
    if handle.seek(0, 2) == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except OSError:
        handle.close()
        if blocking:
            raise
        yield False
        return
    try:
        yield True
    finally:
        handle.seek(0)
        try:
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

def _number(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _missing(work):
    if work.get('status') in ('applied', 'complete'):
        return False
    if 'actionable_missing' in work:
        return _number(work['actionable_missing']) > 0
    return _number(work.get('missing'), len(work.get('missing_people') or [])) > 0

def _next_due(work, people, version, now, revisions=None):
    """Use person retry deadlines, including unavailable and ambiguous people."""
    if not work or not _missing(work):
        return now
    ids = work.get('person_ids') or [p.get('id') for p in work.get('missing_people') or []]
    deadlines = []
    for ident in ids:
        person = people.get(ident) or {}
        if person.get('status') in ('applied', 'no_photo'):
            continue
        if str(ident).startswith('missing-id:'):
            deadlines.append(_number(person.get('checked_at')) + 300)
        elif person.get('version') != version or (revisions and revisions.get(ident) and (revisions[ident] != person.get('evidence_revision'))):
            deadlines.append(now)
        else:
            deadlines.append(_number(person.get('next_attempt_at'), now))
    return min(deadlines) if deadlines else now

def _report(root, title, state='running', count=None, retry_at=None):
    try:
        observe(root, 'portraits', 'portraits', title, state, count=count, retry_at=retry_at)
    except OSError:
        pass

def _log(*values):
    try:
        print(*values, flush=True)
    except OSError:
        pass

def enqueue_works(root, ids, launch=True):
    root = Path(root)
    folder = root / 'data/person-portrait-queue'
    folder.mkdir(parents=True, exist_ok=True)
    valid = {ident for ident in ids if isinstance(ident, str) and IDENT.fullmatch(ident)}
    with _lock(folder / '.queue.lock', blocking=True):
        for ident in valid:
            path = folder / (ident + '.json')
            old = read(path)
            write(path, {**old, 'id': ident, 'queued_at': old.get('queued_at', time.time()), 'requested_at': time.time(), 'request_id': secrets.token_hex(8), 'next_attempt_at': 0})
    # The runtime owns one persistent portrait worker; queue writes never spawn
    # an untracked duplicate service or change the selected network policy.


def _save_job(folder, path, snapshot, updates=None, complete=False):
    with _lock(folder / '.queue.lock', blocking=True):
        current = read(path)
        if not path.exists() or current.get('request_id') != snapshot.get('request_id'):
            return
        if complete:
            path.unlink(missing_ok=True)
        else:
            write(path, {**current, **(updates or {}), 'updated_at': time.time()})

def _recover(folder, state, version, now):
    """Restore unfinished works even when an older worker discarded the queue."""
    with _lock(folder / '.queue.lock', blocking=True):
        for ident, work in state.get('works', {}).items():
            if not IDENT.fullmatch(ident) or not _missing(work):
                continue
            path = folder / (ident + '.json')
            if not path.exists():
                write(path, {'id': ident, 'queued_at': now, 'requested_at': now, 'request_id': secrets.token_hex(8), 'portrait_version': version, 'next_attempt_at': _next_due(work, state.get('people', {}), version, now)})

def _jobs(folder, state, version, now):
    from person_portraits import evidence_revisions
    revisions = evidence_revisions(folder.parent.parent, state.get('people', {}))
    result = []
    for path in folder.glob('*.json'):
        if not IDENT.fullmatch(path.stem):
            continue
        job = read(path)
        job['id'] = path.stem
        work = state.get('works', {}).get(path.stem, {})
        due = _next_due(work, state.get('people', {}), version, now, revisions)
        changed_evidence = any((revisions.get(ident) and revisions[ident] != state.get('people', {}).get(ident, {}).get('evidence_revision') for ident in work.get('person_ids', []) if state.get('people', {}).get(ident, {}).get('status') != 'applied'))
        requested = _number(job.get('requested_at'))
        fresh_request = requested > _number(work.get('updated_at')) and requested > _number(job.get('last_attempt_at'))
        if job.get('portrait_version') == version:
            due = max(due, _number(job.get('next_attempt_at')))
        if fresh_request or changed_evidence:
            due = now
        result.append((path, job, due))
    result.sort(key=lambda row: (_number(row[1].get('last_attempt_at') or row[1].get('queued_at')), _number(row[1].get('queued_at')), row[0].stem))
    return result

def drain(root):
    root = Path(root)
    folder = root / 'data/person-portrait-queue'
    folder.mkdir(parents=True, exist_ok=True)
    with _lock(root / 'data/person-portrait-worker.lock') as acquired:
        if not acquired:
            return
        client = None
        recover_at = failure_until = failures = 0
        while True:
            active = []
            now = time.time()
            if (root / 'data/paused').exists():
                _report(root, '影视库已暂停，头像待办保留', 'idle')
                time.sleep(POLL_SECONDS)
                continue
            if now < failure_until:
                time.sleep(min(POLL_SECONDS, failure_until - now))
                continue
            try:
                from metadata_matcher import Jellyfin
                from person_portraits import sync_portraits, VERSION
                state = read(root / 'data/person-portraits-state.json')
                if now >= recover_at:
                    _recover(folder, state, VERSION, now)
                    recover_at = now + RECOVER_SECONDS
                jobs = _jobs(folder, state, VERSION, now)
                active = [(path, job) for path, job, due in jobs if due <= now][:WORK_BATCH]
                if not active:
                    retry_at = min((due for _, _, due in jobs), default=None)
                    _report(root, '头像补配等待下次重试' if jobs else '头像补配已完成，等待新入库', 'idle', count=len(jobs), retry_at=retry_at)
                    time.sleep(min(POLL_SECONDS, max(1, retry_at - now)) if retry_at else POLL_SECONDS)
                    continue
                if client is None:
                    client = Jellyfin(root)
                ids = [path.stem for path, _ in active]
                _report(root, '补齐演职员头像', count=len(jobs))
                items = client.json('/Items?Ids=' + ','.join(ids) + '&Fields=People,ProviderIds,Path').get('Items', [])
                result = sync_portraits(root, client, items, max_people=PERSON_BATCH, max_seconds=90)
                now = time.time()
                if result.get('deferred'):
                    for path, job in active:
                        _save_job(folder, path, job, {'portrait_version': VERSION, 'last_attempt_at': now, 'next_attempt_at': now + 5})
                    time.sleep(2)
                    continue
                latest = read(root / 'data/person-portraits-state.json')
                states = result.get('work_states', {})
                live = {item['Id'] for item in items}
                for path, job in active:
                    work = states.get(path.stem)
                    if work is not None and (not _missing(work)):
                        _save_job(folder, path, job, complete=True)
                        continue
                    if path.stem not in live or work is None:
                        due = now + 3600
                        status = 'awaiting-index'
                    else:
                        due = _next_due(work, latest.get('people', {}), VERSION, now)
                        due = max(due, now + (2 if result.get('attempted') else 60))
                        status = 'retry' if due > now + 60 else 'pending'
                    _save_job(folder, path, job, {'portrait_version': VERSION, 'last_attempt_at': now, 'next_attempt_at': due, 'status': status, 'consecutive_errors': 0})
                failures = 0
                _log('Portrait batch:', result.get('attempted', 0), 'processed,', result.get('applied', 0), 'restored; unfinished requests retained')
            except Exception as error:
                failures += 1
                now = time.time()
                failure_until = now + min(300, 5 * 2 ** min(failures - 1, 6))
                client = None
                for path, job in active:
                    try:
                        _save_job(folder, path, job, {'last_attempt_at': now, 'next_attempt_at': failure_until, 'status': 'retry', 'consecutive_errors': _number(job.get('consecutive_errors')) + 1, 'last_error_type': type(error).__name__})
                    except OSError:
                        pass
                _report(root, '头像补配暂缓，待办已保留并将自动重试', 'error', retry_at=failure_until)
                _log('Portrait batch deferred; retry scheduled:', type(error).__name__)
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=_cinema_path(''))
    args = parser.parse_args()
    drain(Path(args.root))
