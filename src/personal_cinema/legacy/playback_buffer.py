"""Bounded disk read-ahead shared by all HTTP readers of a media source."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import http.client
import logging
import os
from personal_cinema.runtime_paths import RuntimePath as Path
import re
import secrets
import shutil
import socket
import threading
import time
import urllib.error
from playback_cache import activity, cached_block, close_upstream, retryable_upstream
from source_fingerprint import identity
BLOCK = 16 * 1024 * 1024
MAX_BYTES = 10 * 1024 ** 3
IDLE_SECONDS = 15
_MANAGERS = {}
_MANAGER_LOCK = threading.Lock()

class BufferUnavailable(OSError):
    pass

class SourceMismatch(OSError):
    pass

class Interrupted(Exception):
    pass

def limit(worker):
    try:
        return max(0, min(1024 * 1024 ** 3, int(worker.config.get('playback_buffer_bytes', MAX_BYTES))))
    except (TypeError, ValueError):
        return MAX_BYTES

def linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())

class RangeReader:
    """One source connection, resumed at bytes already written to staging."""

    def __init__(self, producer, start, end):
        self.producer = producer
        self.position, self.end = (start, end)
        self.response = None
        self.retries = 0
        self.cancelled = False

    def interrupt(self):
        self.cancelled = True
        response = self.response
        try:
            sock = response.fp.raw._sock
            sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass

    def close(self):
        close_upstream(self.response)
        self.response = None

    def read(self, count):
        while True:
            if self.cancelled or self.producer.should_stop():
                raise Interrupted()
            self.producer.ensure_identity()
            failure = None
            if self.response is None:
                entry = self.producer.entry
                request = self.producer.worker.cloud_request(entry['remote'], range_header=f'bytes={self.position}-{self.end}', entry_id=entry['id'])
                try:
                    self.response = self.producer.worker.http.open(request, timeout=15)
                except (OSError, http.client.HTTPException) as error:
                    failure = error
                if self.cancelled or self.producer.should_stop():
                    raise Interrupted()
                if failure is None:
                    expected = f"bytes {self.position}-{self.end}/{entry['size']}"
                    if self.response.status != 206 or self.response.headers.get('Content-Range') != expected:
                        logging.warning('Playback buffer range rejected id=%s status=%s position=%d', entry['id'], self.response.status, self.position)
                        raise SourceMismatch('Playback source range or length changed')
            if failure is None:
                try:
                    data = self.response.read(min(count, self.end - self.position + 1))
                    if not data:
                        raise ConnectionResetError('Playback source range ended early')
                except (OSError, http.client.HTTPException) as error:
                    failure = error
            if failure is None:
                self.position += len(data)
                return data
            self.close()
            if isinstance(failure, urllib.error.HTTPError):
                close_upstream(failure)
            if self.cancelled or self.producer.should_stop():
                raise Interrupted()
            code = failure.code if isinstance(failure, urllib.error.HTTPError) else '-'
            if not retryable_upstream(failure) or self.retries >= 3:
                logging.warning('Playback buffer stopped id=%s status=%s position=%d retries=%d error=%s', self.producer.entry['id'], code, self.position, self.retries, type(failure).__name__)
                raise failure
            delay = (0.5, 1.0, 2.0)[self.retries]
            self.retries += 1
            logging.warning('Playback buffer reconnect id=%s status=%s position=%d retry=%d/3', self.producer.entry['id'], code, self.position, self.retries)
            with self.producer.manager.condition:
                self.producer.manager.condition.wait(delay)

class Manager:

    def __init__(self, worker):
        self.worker = worker
        self.path = _cinema_cache_path('playback-buffer')
        if linked(self.path) or linked(self.path.parent):
            raise BufferUnavailable('Playback buffer directory is redirected')
        self.path.mkdir(parents=True, exist_ok=True)
        self.condition = threading.Condition(threading.RLock())
        self.blocks = {}
        self.producers = {}
        self.used = 0
        self.reserved = 0
        self.focus = None
        self.order = 0
        self.failure = None
        self.retry_after = 0
        for directory, subdirs, files in os.walk(self.path, followlinks=False):
            base = Path(directory)
            subdirs[:] = [d for d in subdirs if not linked(base / d)]
            for name in files:
                path = base / name
                if linked(path):
                    raise BufferUnavailable('Playback buffer contains a redirected file')
                own_dir = base.parent == self.path and re.fullmatch('[a-f0-9]{64}', base.name)
                if own_dir and re.fullmatch('\\d+\\.[a-f0-9]{12}\\.part', name):
                    try:
                        path.unlink()
                        continue
                    except OSError:
                        pass
                stat = path.stat()
                self.used += stat.st_size
                if own_dir and re.fullmatch('\\d+\\.blk', name):
                    self.blocks[base.name, int(path.stem)] = {'path': path, 'size': stat.st_size, 'pins': 0, 'at': stat.st_mtime}
        with self.condition:
            if not self.trim(None, 0):
                raise BufferUnavailable('Playback buffer contains files exceeding its limit')

    def remove(self, key):
        record = self.blocks.get(key)
        if not record or record['pins']:
            return False
        try:
            if linked(record['path']) or linked(record['path'].parent):
                return False
            record['path'].unlink(missing_ok=True)
        except OSError:
            return False
        self.used -= record['size']
        del self.blocks[key]
        try:
            record['path'].parent.rmdir()
        except OSError:
            pass
        return True

    def trim(self, producer, incoming):
        cap = limit(self.worker)
        cursor = producer.cursor() // BLOCK if producer else 0
        window = max(1, cap // BLOCK)
        candidates = []
        for key, record in self.blocks.items():
            source, index = key
            owner = self.producers.get(source)
            needed = owner and any((r['cursor'] // BLOCK == index for r in owner.requests.values()))
            if record['pins'] or needed:
                continue
            eligible = producer is None or source != producer.key or index < cursor or (index >= cursor + window)
            if eligible:
                candidates.append((source == (producer.key if producer else None), record['at'], key))
        for _, _, key in sorted(candidates):
            if self.used + self.reserved + incoming <= cap:
                return True
            self.remove(key)
        return self.used + self.reserved + incoming <= cap

    def reserve(self, producer, index):
        amount = min(BLOCK, producer.entry['size'] - index * BLOCK)
        with self.condition:
            if self.failure:
                raise self.failure
            if amount <= 0 or limit(self.worker) < amount:
                raise BufferUnavailable('Playback disk buffer is disabled or too small')
            if not self.trim(producer, amount):
                return None
            try:
                floor = int(self.worker.config.get('reserve_free_bytes', 0))
                if shutil.disk_usage(self.path).free - self.reserved - amount < floor:
                    raise BufferUnavailable('Playback buffer disk reserve reached')
                folder = self.path / producer.key
                if linked(folder):
                    raise BufferUnavailable('Playback source directory is redirected')
                folder.mkdir(exist_ok=True)
            except OSError as error:
                raise BufferUnavailable('Playback buffer disk unavailable') from error
            self.reserved += amount
            path = folder / f'{index}.{secrets.token_hex(6)}.part'
            return (path, amount)

    def abandon(self, reservation):
        path, amount = reservation
        with self.condition:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                try:
                    self.used += path.stat().st_size
                except OSError:
                    self.used += amount
            self.reserved -= amount
            self.condition.notify_all()

    def publish(self, producer, index, reservation):
        path, amount = reservation
        destination = path.parent / f'{index}.blk'
        with self.condition:
            try:
                if path.stat().st_size != amount or linked(destination):
                    raise BufferUnavailable('Playback buffer block incomplete')
                os.replace(path, destination)
            except OSError as error:
                raise BufferUnavailable('Playback buffer block could not be committed') from error
            previous = self.blocks.get((producer.key, index))
            self.used += amount - (previous['size'] if previous else 0)
            self.reserved -= amount
            self.blocks[producer.key, index] = {'path': destination, 'size': amount, 'pins': 0, 'at': time.time()}
            self.condition.notify_all()

    def fail_disk(self, error):
        with self.condition:
            self.failure = error
            self.retry_after = time.monotonic() + 30
            for producer in self.producers.values():
                producer.error = error
                producer.stop_requested = True
                if producer.reader:
                    producer.reader.interrupt()
            self.condition.notify_all()

    def add(self, entry, start, end):
        key = identity(entry)
        with self.condition:
            if self.failure:
                raise self.failure
            producer = self.producers.get(key)
            if producer is None:
                producer = self.producers[key] = Producer(self, entry, key)
            self.order += 1
            token = self.order
            producer.requests[token] = {'cursor': start, 'end': end, 'order': token}
            producer.last_cursor = start
            self.focus = key
            if producer.timer:
                producer.timer.cancel()
                producer.timer = None
            if not producer.running:
                producer.start()
            elif producer.reader and producer.target != start // BLOCK and ((key, start // BLOCK) not in self.blocks):
                producer.reader.interrupt()
            self.condition.notify_all()
            return (producer, token)

class Producer:

    def __init__(self, manager, entry, key):
        self.manager, self.worker = (manager, manager.worker)
        self.entry, self.key = (dict(entry), key)
        self.requests = {}
        self.last_cursor = 0
        self.running = False
        self.stop_requested = False
        self.reader = None
        self.target = None
        self.timer = None
        self.error = None

    def cursor(self):
        if self.requests:
            return max(self.requests.values(), key=lambda r: r['order'])['cursor']
        return self.last_cursor

    def start(self):
        self.running = True
        self.stop_requested = False
        self.error = None
        threading.Thread(target=self.run, daemon=True, name='playback-buffer-' + self.entry['id'][:8]).start()

    def should_stop(self):
        return self.stop_requested or self.manager.failure is not None

    def ensure_identity(self):
        current = self.worker.entries.get(self.entry['id'])
        if current is None or identity(current) != self.key:
            raise SourceMismatch('Playback source identity changed')

    def pick(self):
        manager = self.manager
        requests = sorted(self.requests.values(), key=lambda r: r['order'], reverse=True)
        for request in requests:
            index = request['cursor'] // BLOCK
            if request['cursor'] <= request['end'] and (self.key, index) not in manager.blocks:
                return index
        if manager.focus != self.key:
            return None
        cursor = self.cursor() // BLOCK
        count = max(1, limit(self.worker) // BLOCK)
        total = (self.entry['size'] + BLOCK - 1) // BLOCK
        for index in range(cursor, min(total, cursor + count)):
            if (self.key, index) not in manager.blocks:
                return index
        return None

    def expire(self):
        with self.manager.condition:
            if self.requests:
                return
            self.stop_requested = True
            if self.reader:
                self.reader.interrupt()
            self.manager.condition.notify_all()

    def remove(self, token):
        with self.manager.condition:
            request = self.requests.pop(token, None)
            if request:
                self.last_cursor = request['cursor']
            if not self.requests:
                self.timer = threading.Timer(IDLE_SECONDS, self.expire)
                self.timer.daemon = True
                self.timer.start()
            self.manager.condition.notify_all()

    def fill(self, index, reservation, reader):
        path, amount = reservation
        try:
            output = path.open('xb')
        except OSError as error:
            raise BufferUnavailable('Playback buffer staging unavailable') from error
        try:
            legacy = None
            if self.worker.config.get('download_block_bytes', BLOCK) == BLOCK:
                legacy = cached_block(self.worker, self.entry, index)
            if legacy is not None and len(legacy) == amount:
                try:
                    output.write(legacy)
                    output.flush()
                except OSError as error:
                    raise BufferUnavailable('Playback buffer disk write failed') from error
                return False
            written = 0
            while written < amount:
                if self.should_stop() or reader.cancelled:
                    raise Interrupted()
                data = reader.read(min(256 * 1024, amount - written))
                try:
                    count = output.write(data)
                    if count != len(data):
                        raise OSError('Short disk write')
                except OSError as error:
                    raise BufferUnavailable('Playback buffer disk write failed') from error
                written += len(data)
                activity(self.worker, self.entry)
            try:
                output.flush()
            except OSError as error:
                raise BufferUnavailable('Playback buffer disk flush failed') from error
            return True
        finally:
            try:
                output.close()
            except OSError as error:
                raise BufferUnavailable('Playback buffer disk close failed') from error

    def run(self):
        manager = self.manager
        reservation = None
        try:
            while not self.should_stop():
                with manager.condition:
                    index = self.pick()
                    if index is None:
                        manager.condition.wait(0.5)
                        continue
                    reservation = manager.reserve(self, index)
                    if reservation is None:
                        manager.condition.wait(0.5)
                        continue
                    self.target = index
                    next_cached = min((i for key, i in manager.blocks if key == self.key and i > index), default=index + 8)
                    end = min(self.entry['size'], min(index + 8, next_cached) * BLOCK) - 1
                    self.reader = RangeReader(self, index * BLOCK, end)
                try:
                    while True:
                        fetched = self.fill(index, reservation, self.reader)
                        manager.publish(self, index, reservation)
                        reservation = None
                        with manager.condition:
                            following = self.pick()
                            if not fetched or self.should_stop() or self.reader.cancelled or (following != index + 1) or (self.reader.position > end):
                                break
                            index = following
                            reservation = manager.reserve(self, index)
                            if reservation is None:
                                break
                            self.target = index
                except Interrupted:
                    pass
                finally:
                    self.reader.close()
                    with manager.condition:
                        self.reader = None
                        self.target = None
                        if reservation is not None:
                            manager.abandon(reservation)
                            reservation = None
        except BufferUnavailable as error:
            manager.fail_disk(error)
            logging.warning('Playback disk buffer unavailable id=%s; readers will use demand streaming', self.entry['id'])
        except Exception as error:
            with manager.condition:
                self.error = error
                manager.condition.notify_all()
            logging.warning('Playback buffer producer stopped id=%s error=%s', self.entry['id'], type(error).__name__)
        finally:
            if self.reader:
                self.reader.close()
            with manager.condition:
                if reservation is not None:
                    manager.abandon(reservation)
                self.reader = None
                self.target = None
                self.running = False
                if self.requests and self.stop_requested and (not self.error) and (not manager.failure):
                    self.start()
                manager.condition.notify_all()

def manager_for(worker):
    key = str(Path(worker.root).resolve())
    with _MANAGER_LOCK:
        manager = _MANAGERS.get(key)
        if manager is not None:
            with manager.condition:
                if manager.failure and time.monotonic() >= manager.retry_after and (not any((p.running or p.requests for p in manager.producers.values()))):
                    manager = None
        if manager is None:
            try:
                manager = Manager(worker)
            except OSError as error:
                raise BufferUnavailable('Playback disk buffer could not be opened') from error
            _MANAGERS[key] = manager
        return manager

def stream(worker, entry, start, end, output):
    """Serve committed blocks; fall back from the first unsent byte on disk errors."""
    from playback_cache import stream as demand_stream
    if limit(worker) < min(BLOCK, entry['size']):
        return demand_stream(worker, entry, start, end, output)
    position = start
    producer = None
    token = None
    try:
        manager = manager_for(worker)
        producer, token = manager.add(entry, start, end)
        while position <= end:
            index = position // BLOCK
            key = (producer.key, index)
            with manager.condition:
                while key not in manager.blocks:
                    if producer.error:
                        raise producer.error
                    if manager.failure:
                        raise manager.failure
                    manager.condition.wait(0.5)
                record = manager.blocks[key]
                expected = min(BLOCK, entry['size'] - index * BLOCK)
                if record['size'] != expected or linked(record['path']):
                    raise BufferUnavailable('Playback buffer block size changed')
                try:
                    source = record['path'].open('rb')
                    source.seek(position - index * BLOCK)
                except OSError as error:
                    raise BufferUnavailable('Playback buffer block could not be read') from error
                record['pins'] += 1
                record['at'] = time.time()
            try:
                boundary = min(end + 1, (index + 1) * BLOCK, entry['size'])
                while position < boundary:
                    try:
                        data = source.read(min(256 * 1024, boundary - position))
                        if not data:
                            raise OSError('Short disk read')
                    except OSError as error:
                        raise BufferUnavailable('Playback buffer disk read failed') from error
                    sent = output.write(data)
                    if sent is not None and sent != len(data):
                        raise ConnectionAbortedError('Playback client write was incomplete')
                    position += len(data)
                    activity(worker, entry)
                    with manager.condition:
                        producer.requests[token]['cursor'] = position
                        producer.last_cursor = position
                        manager.condition.notify_all()
            finally:
                try:
                    source.close()
                except OSError:
                    pass
                finally:
                    with manager.condition:
                        record['pins'] -= 1
                        manager.condition.notify_all()
    except BufferUnavailable as error:
        if producer:
            producer.manager.fail_disk(error)
            producer.remove(token)
            producer = None
        if position <= end:
            return demand_stream(worker, entry, position, end, output)
    finally:
        if producer:
            producer.remove(token)

def status(worker):
    key = str(Path(worker.root).resolve())
    with _MANAGER_LOCK:
        manager = _MANAGERS.get(key)
    result = {'enabled': limit(worker) >= BLOCK, 'available': True, 'pool_bytes': 0, 'limit_bytes': limit(worker), 'reserved_bytes': 0, 'current': None, 'active': [], 'error': None}
    if manager is None:
        return result
    with manager.condition:
        result.update(pool_bytes=manager.used, reserved_bytes=manager.reserved, available=manager.failure is None, error='磁盘缓冲暂不可用' if manager.failure else None)
        for producer in manager.producers.values():
            if not producer.running and producer.key != manager.focus:
                continue
            cursor = producer.cursor()
            ahead = 0
            index = cursor // BLOCK
            while (producer.key, index) in manager.blocks:
                size = manager.blocks[producer.key, index]['size']
                amount = size - (cursor % BLOCK if ahead == 0 else 0)
                if amount <= 0:
                    break
                ahead += amount
                if size < BLOCK:
                    break
                index += 1
            entry = worker.entries.get(producer.entry['id'], producer.entry)
            phase = 'error' if producer.error else 'paused' if producer.requests and entry.get('playback_paused') else 'watching' if producer.requests else 'grace' if producer.running else 'stopped'
            current = {'id': producer.entry['id'], 'title': entry.get('display_title') or entry.get('series') or '', 'source': str(producer.entry.get('remote', '')).strip('/').split('/')[0], 'phase': phase, 'ahead_bytes': ahead, 'cursor_bytes': cursor, 'reading': producer.target is not None, 'readers': len(producer.requests)}
            if producer.running:
                result['active'].append(current)
            if producer.key == manager.focus:
                result['current'] = current
        return result
