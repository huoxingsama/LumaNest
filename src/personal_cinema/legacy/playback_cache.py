"""Reuse verified ranges and save only bytes actually delivered for playback."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import errno, hashlib, http.client, json, logging, os, re, shutil, socket, ssl, threading, time, urllib.error
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor
from cache_policy import cache_path
from range_cache import manifest_write
from source_fingerprint import partial_identity
_WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix='playback-cache-write')
_SLOT = threading.BoundedSemaphore(1)

def sparse_file(output):
    """A tail probe must not force Windows to zero-fill an entire film."""
    if os.name != 'nt':
        return True
    try:
        import ctypes, msvcrt
        api = ctypes.WinDLL('kernel32', use_last_error=True).DeviceIoControl
        api.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
        api.restype = ctypes.c_int
        returned = ctypes.c_ulong()
        return bool(api(msvcrt.get_osfhandle(output.fileno()), 590020, None, 0, None, 0, ctypes.byref(returned), None))
    except (OSError, ValueError, AttributeError):
        return False

def paths(worker, entry):
    block = worker.config.get('download_block_bytes', 16 * 1024 * 1024)
    if not isinstance(block, int) or not 1024 * 1024 <= block <= 64 * 1024 * 1024:
        raise ValueError('Invalid cache block size')
    staging = cache_path(_cinema_cache_path('partial'), entry['id'] + '.part')
    manifest = cache_path(worker.root / 'data/range-resume', entry['id'] + '.json')
    expected = partial_identity(entry, block)
    return (staging, manifest, expected)

def state(worker, entry):
    staging, manifest, expected = paths(worker, entry)
    try:
        value = json.loads(manifest.read_text('utf-8'))
        if any((value.get(k) != v for k, v in expected.items())):
            return (staging, manifest, expected, {})
        if not staging.is_file() or staging.stat().st_size != entry['size'] or staging.stat().st_nlink != 1:
            return (staging, manifest, expected, {})
        if not isinstance(value.get('completed'), dict):
            return (staging, manifest, expected, {})
        return (staging, manifest, expected, value['completed'])
    except (OSError, ValueError, TypeError):
        return (staging, manifest, expected, {})

def cached_block(worker, entry, index):
    staging, _, expected, completed = state(worker, entry)
    digest = completed.get(str(index))
    if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
        return None
    start = index * expected['block_size']
    length = min(expected['block_size'], entry['size'] - start)
    if length <= 0:
        return None
    try:
        with staging.open('rb') as source:
            source.seek(start)
            data = source.read(length)
        return data if len(data) == length and hashlib.sha256(data).hexdigest() == digest else None
    except OSError:
        return None

def commit(worker, entry, index, data):
    owner = worker.transfer_lock
    if not owner.acquire(blocking=False):
        return
    try:
        if entry.get('cache_blocked') or entry.get('ready'):
            return
        staging, manifest, expected, completed = state(worker, entry)
        block = expected['block_size']
        start = index * block
        if start < 0 or len(data) != min(block, entry['size'] - start):
            return
        if str(index) in completed:
            return
        if not completed:
            if staging.exists() or manifest.exists():
                return
            ready = sum((e['size'] for e in worker.entries.values() if e.get('ready')))
            partial = sum((p.stat().st_size for p in _cinema_cache_path('partial').glob('*.part') if p.is_file()))
            if ready + partial + entry['size'] > worker.config['max_cache_bytes']:
                return
            if shutil.disk_usage(worker.root).free - len(data) < worker.config['reserve_free_bytes']:
                return
            staging.parent.mkdir(parents=True, exist_ok=True)
            with staging.open('xb') as output:
                sparse = sparse_file(output)
                if sparse:
                    output.truncate(entry['size'])
            if not sparse:
                staging.unlink(missing_ok=True)
                return
        if shutil.disk_usage(worker.root).free - len(data) < worker.config['reserve_free_bytes']:
            return
        with staging.open('r+b') as output:
            if not sparse_file(output):
                return
            output.seek(start)
            output.write(data)
            output.flush()
        completed[str(index)] = hashlib.sha256(data).hexdigest()
        manifest_write(manifest, {**expected, 'completed': completed})
        with worker.lock:
            entry['partial_bytes_completed'] = sum((min(block, entry['size'] - int(i) * block) for i in completed if str(i).isdecimal() and 0 <= int(i) * block < entry['size']))
            entry['playback_cached_at'] = time.time()
            worker.save()
    except (OSError, ValueError, RuntimeError):
        pass
    finally:
        owner.release()

def queue_block(worker, entry, index, data):
    if not _SLOT.acquire(blocking=False):
        return

    def write():
        try:
            commit(worker, entry, index, data)
        finally:
            _SLOT.release()
    try:
        _WRITER.submit(write)
    except RuntimeError:
        _SLOT.release()

def activity(worker, entry):
    with worker.lock:
        worker.playback_stream_activity[entry['id']] = time.time()
    interrupt = getattr(worker, 'transfer_interrupt', None)
    if interrupt:
        interrupt()

def retryable_upstream(error):
    """Only temporary source failures qualify; never retry an invalid range."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 429) or 500 <= error.code <= 599
    if isinstance(error, urllib.error.URLError):
        return isinstance(error.reason, BaseException) and retryable_upstream(error.reason)
    if isinstance(error, ssl.SSLCertVerificationError):
        return False
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    if isinstance(error, (TimeoutError, ConnectionError, http.client.IncompleteRead, http.client.RemoteDisconnected, ssl.SSLEOFError)):
        return True
    return isinstance(error, OSError) and (error.errno in (errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.ECONNREFUSED, errno.ENETRESET, errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPIPE) or getattr(error, 'winerror', None) in (10050, 10051, 10052, 10053, 10054, 10060, 10061, 10065))

def close_upstream(response):
    if response is not None:
        with suppress(OSError):
            response.close()

def stream(worker, entry, start, end, output):
    """Use cached blocks when present; request only the client's missing span."""
    _, _, expected, _ = state(worker, entry)
    block = expected['block_size']
    position = start
    while position <= end:
        activity(worker, entry)
        index = position // block
        data = cached_block(worker, entry, index)
        if data is not None:
            take = min(end - position + 1, len(data) - (position - index * block))
            output.write(data[position - index * block:position - index * block + take])
            position += take
            continue
        _, _, _, completed = state(worker, entry)
        next_cached = min((int(i) * block for i in completed if str(i).isdecimal() and int(i) * block > position), default=entry['size'])
        stop = min(end, next_cached - 1, (index + 8) * block - 1)
        response = None
        retries = 0
        buffer = bytearray()
        buffer_index = position // block
        aligned = position % block == 0
        try:
            while position <= stop:
                failure = None
                if response is None:
                    request = worker.cloud_request(entry['remote'], range_header=f'bytes={position}-{stop}', entry_id=entry['id'])
                    try:
                        response = worker.http.open(request, timeout=120)
                    except (OSError, http.client.HTTPException) as error:
                        failure = error
                    if failure is None:
                        if response.status != 206 or response.headers.get('Content-Range') != f"bytes {position}-{stop}/{entry['size']}":
                            logging.warning('Playback upstream range rejected id=%s status=%s position=%d', entry['id'], response.status, position)
                            raise OSError('Playback range did not match the original file')
                if failure is None:
                    boundary = min(stop + 1, (position // block + 1) * block)
                    try:
                        data = response.read(min(256 * 1024, boundary - position))
                        if not data:
                            raise ConnectionResetError('Playback source ended before the requested range')
                    except (OSError, http.client.HTTPException) as error:
                        failure = error
                if failure is not None:
                    close_upstream(response)
                    response = None
                    if isinstance(failure, urllib.error.HTTPError):
                        close_upstream(failure)
                    status = failure.code if isinstance(failure, urllib.error.HTTPError) else '-'
                    if not retryable_upstream(failure) or retries >= 3:
                        logging.warning('Playback upstream stopped id=%s status=%s position=%d retries=%d error=%s', entry['id'], status, position, retries, type(failure).__name__)
                        raise failure
                    delay = (0.5, 1.0, 2.0)[retries]
                    retries += 1
                    logging.warning('Playback upstream reconnect id=%s status=%s position=%d retry=%d/3 delay=%.1fs error=%s', entry['id'], status, position, retries, delay, type(failure).__name__)
                    activity(worker, entry)
                    time.sleep(delay)
                    continue
                written = output.write(data)
                if written is not None and written != len(data):
                    raise ConnectionAbortedError('Playback client write was incomplete')
                if aligned:
                    buffer.extend(data)
                position += len(data)
                activity(worker, entry)
                if position % block == 0 or position == entry['size']:
                    if aligned and buffer:
                        queue_block(worker, entry, buffer_index, bytes(buffer))
                    buffer = bytearray()
                    buffer_index = position // block
                    aligned = True
        finally:
            close_upstream(response)
