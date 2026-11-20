"""Verified resumable ranges with cooperative playback preemption."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import json
import os
import secrets
import shutil
import socket
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from cache_policy import cache_path, delete_cached
from cache_subtitles import sync_ready as sync_cached_subtitles
from source_fingerprint import partial_identity

class DownloadPaused(Exception):
    pass

def manifest_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            json.dump(value, output, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def load_completed(staging, manifest, expected):
    try:
        saved = json.loads(manifest.read_text('utf-8'))
        if any((saved.get(k) != v for k, v in expected.items())):
            return {}
        if not staging.is_file() or staging.stat().st_size != expected['size'] or staging.stat().st_nlink != 1:
            return {}
        result = {}
        with staging.open('rb') as source:
            for key, digest in saved.get('completed', {}).items():
                index = int(key)
                start = index * expected['block_size']
                if index < 0 or start >= expected['size']:
                    continue
                source.seek(start)
                data = source.read(min(expected['block_size'], expected['size'] - start))
                if hashlib.sha256(data).hexdigest() == digest:
                    result[str(index)] = digest
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {}

def allocated_bytes(path):
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        query = kernel.GetCompressedFileSizeW
        query.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
        query.restype = ctypes.c_ulong
        high = ctypes.c_ulong(0)
        ctypes.set_last_error(0)
        low = query(str(path), ctypes.byref(high))
        if low == 4294967295 and ctypes.get_last_error():
            return 0
        return min(path.stat().st_size, high.value << 32 | low)
    info = path.stat()
    return min(info.st_size, getattr(info, 'st_blocks', 0) * 512)

def download_ranges(worker, entry):
    root, size = (worker.root, entry['size'])
    target = cache_path(_cinema_cache_path('ready'), entry['relative'])
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = cache_path(_cinema_cache_path('partial'), entry['id'] + '.part')
    manifest = cache_path(root / 'data/range-resume', entry['id'] + '.json')
    block_size = worker.config.get('download_block_bytes', 16 * 1024 * 1024)
    if not isinstance(block_size, int) or block_size <= 0:
        raise ValueError('Invalid range block size')
    identity = partial_identity(entry, block_size)
    if target.exists():
        if entry.get('cached_at') is not None and target.stat().st_size == size:
            entry['ready'] = True
            sync_cached_subtitles(root, [entry])
            return True
        raise IOError('Existing local file has no verified cache provenance; will not overwrite')
    completed = load_completed(staging, manifest, identity)
    if not completed:
        delete_cached(_cinema_cache_path('partial'), entry['id'] + '.part')
        manifest.unlink(missing_ok=True)
    resumed = bool(completed)
    completed_bytes = sum((min(block_size, size - int(i) * block_size) for i in completed))
    incoming = 0 if resumed else size
    disk_incoming = max(0, size - allocated_bytes(staging)) if resumed else size
    worker.cleanup(incoming=incoming, disk_incoming=disk_incoming)
    ready_bytes = sum((e['size'] for e in worker.entries.values() if e.get('ready')))
    partial_bytes = sum((p.stat().st_size for p in _cinema_cache_path('partial').glob('*') if p.is_file()))
    if ready_bytes + partial_bytes + incoming > worker.config['max_cache_bytes'] or shutil.disk_usage(root).free - disk_incoming < worker.config['reserve_free_bytes']:
        return False
    if not resumed:
        staging.parent.mkdir(parents=True, exist_ok=True)
        with staging.open('xb') as output:
            output.truncate(size)
    state = dict(identity, completed=completed)
    manifest_write(manifest, state)
    worker.downloading = entry['id']
    worker.download_progress = completed_bytes
    entry.pop('download_paused', None)
    entry.pop('pause_kind', None)
    entry.pop('paused_at', None)
    entry['partial_bytes_completed'] = completed_bytes
    worker.save()
    abort = threading.Event()
    response_lock = threading.Lock()
    responses = set()
    paused = False
    started_for_playback = bool(entry.get('playback_active'))

    def stop():
        abort.set()
        with response_lock:
            for response in list(responses):
                try:
                    response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                except (AttributeError, OSError):
                    pass

    def interrupt():
        nonlocal paused
        if worker.should_preempt(entry, started_for_playback):
            paused = True
            stop()

    def tick():
        interrupt()
        if paused:
            raise DownloadPaused('Playback priority changed')
    worker.transfer_interrupt = interrupt

    def block(index):
        start, end = (index * block_size, min((index + 1) * block_size, size) - 1)
        for attempt in range(3):
            if abort.is_set():
                raise DownloadPaused('Transfer interrupted') if paused else OSError('Another download range failed')
            try:
                extra = {'entry_id': entry['id']} if getattr(worker, 'source_bindings', None) else {}
                request = worker.cloud_request(entry['remote'], range_header=f'bytes={start}-{end}', **extra)
                with worker.http.open(request, timeout=15) as response:
                    with response_lock:
                        responses.add(response)
                    try:
                        if response.status != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end}/{size}':
                            raise IOError('Source did not honor the requested original-file range')
                        checksum = hashlib.sha256()
                        with staging.open('r+b') as output:
                            output.seek(start)
                            remaining = end - start + 1
                            while remaining:
                                if abort.is_set():
                                    raise DownloadPaused('Transfer interrupted') if paused else OSError('Another download range failed')
                                data = response.read(min(256 * 1024, remaining))
                                if not data:
                                    raise IOError('Download range was truncated')
                                output.write(data)
                                checksum.update(data)
                                remaining -= len(data)
                            output.flush()
                            os.fsync(output.fileno())
                        with worker.lock:
                            completed[str(index)] = checksum.hexdigest()
                            manifest_write(manifest, state)
                            worker.download_progress += end - start + 1
                            entry['partial_bytes_completed'] = worker.download_progress
                            worker.save()
                        return
                    finally:
                        with response_lock:
                            responses.discard(response)
            except DownloadPaused:
                raise
            except (OSError, urllib.error.URLError):
                if abort.is_set() or attempt == 2:
                    abort.set()
                    raise
                abort.wait(2 ** attempt)
    try:
        with ThreadPoolExecutor(max_workers=worker.config.get('download_connections', 2)) as pool:
            futures = [pool.submit(block, index) for index in range((size + block_size - 1) // block_size) if str(index) not in completed]
            try:
                worker.wait_downloads(futures, on_tick=tick)
            except Exception:
                stop()
                for future in futures:
                    future.cancel()
                raise
        if len(completed) != (size + block_size - 1) // block_size or staging.stat().st_size != size:
            raise IOError('Incomplete cached file')
        os.rename(staging, target)
        with worker.lock:
            entry.update(ready=True, cached_at=time.time(), last_access=time.time())
            entry['queued'] = False
            entry.pop('partial_bytes_completed', None)
        manifest.unlink(missing_ok=True)
        sync_cached_subtitles(root, [entry])
        return True
    except Exception:
        if paused:
            kind = 'steam' if getattr(worker, 'steam_downloading', False) else 'playback' if started_for_playback else 'background'
            entry.update(download_paused=True, pause_kind=kind, paused_at=time.time())
            raise DownloadPaused('Playback priority changed') from None
        raise
    finally:
        worker.transfer_interrupt = None
        worker.downloading = None
        if not completed:
            delete_cached(_cinema_cache_path('partial'), entry['id'] + '.part')
            manifest.unlink(missing_ok=True)
        worker.save()
