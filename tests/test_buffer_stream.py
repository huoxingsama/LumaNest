"""Exercise committed playback blocks through the real streaming producer."""
import importlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from personal_cinema.worker import prepare


class RangeResponse:
    """A range response that stalls after one block, like a slow source."""
    def __init__(self, data, start, end, block):
        self.status = 206
        self.headers = {'Content-Range': f'bytes {start}-{end}/{len(data)}'}
        self.body = io.BytesIO(data[start:end + 1])
        self.block = block
        self.blocked = threading.Event()
        self.cancelled = threading.Event()
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self))
        self.bytes_read = 0

    def read(self, count):
        if self.body.tell() >= self.block:
            self.blocked.set()
            if not self.cancelled.wait(5):
                raise TimeoutError('Fixture was not shut down after the read-ahead check')
            raise ConnectionAbortedError('Fixture response closed')
        data = self.body.read(min(count, self.block - self.body.tell()))
        self.bytes_read += len(data)
        return data

    def shutdown(self, _how):
        self.cancelled.set()

    def close(self):
        self.cancelled.set()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class RangeHTTP:
    def __init__(self, payload, block):
        self.payload, self.block = payload, block
        self.requests, self.responses = [], []

    def open(self, request, timeout):
        match = re.fullmatch(r'bytes=(\d+)-(\d+)', request['range'])
        if match is None:
            raise AssertionError('Expected an exact byte-range request')
        start, end = map(int, match.groups())
        if not 0 <= start <= end < len(self.payload):
            raise AssertionError('Requested range is outside this fixture')
        self.requests.append(dict(request))
        response = RangeResponse(self.payload, start, end, self.block)
        self.responses.append(response)
        return response


class BufferStreamTests(unittest.TestCase):
    BLOCK = 4096

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / 'runtime'
        self.root.mkdir()
        environment = patch.dict(os.environ, {'PERSONAL_CINEMA_HOME': str(self.home),
                                             'MEDIA_LIBRARY_ROOT': str(self.root)})
        environment.start(); self.addCleanup(environment.stop)
        prepare()
        self.buffer = importlib.import_module('playback_buffer')
        block = patch.object(self.buffer, 'BLOCK', self.BLOCK)
        block.start(); self.addCleanup(block.stop)
        self.entry = {'id': 'a' * 24, 'remote': '/Fixture/movie.mkv',
                      'size': self.BLOCK * 8, 'source_fid': 'original-object',
                      'source_checksum': 'original-checksum', 'ready': False}
        self.http = RangeHTTP(b'A' * self.entry['size'], self.BLOCK)
        self.worker = SimpleNamespace(root=self.root, entries={self.entry['id']: self.entry},
            config={'playback_buffer_bytes': self.BLOCK * 4, 'reserve_free_bytes': 0,
                    # This test isolates playback blocks from the offline pool.
                    'download_block_bytes': 1024 * 1024},
            lock=threading.RLock(), playback_stream_activity={}, transfer_interrupt=None,
            http=self.http,
            cloud_request=lambda remote, range_header, entry_id=None: {
                'remote': remote, 'range': range_header, 'entry_id': entry_id})
        self.addCleanup(self.stop_producers)

    def stop_producers(self):
        key = str(self.root.resolve())
        with self.buffer._MANAGER_LOCK:
            manager = self.buffer._MANAGERS.get(key)
        if manager is None:
            return
        timers = []
        with manager.condition:
            for producer in manager.producers.values():
                producer.requests.clear()
                producer.stop_requested = True
                if producer.timer:
                    producer.timer.cancel()
                    timers.append(producer.timer)
                if producer.reader:
                    producer.reader.interrupt()
            manager.condition.notify_all()
            deadline = time.monotonic() + 3
            while any(p.running for p in manager.producers.values()) and time.monotonic() < deadline:
                manager.condition.wait(.05)
            self.assertFalse(any(p.running for p in manager.producers.values()), 'Fixture producer leaked')
        for timer in timers:
            timer.join(1)
        with self.buffer._MANAGER_LOCK:
            self.buffer._MANAGERS.pop(key, None)

    def read_prefix(self, entry):
        output = io.BytesIO()
        self.buffer.stream(self.worker, entry, 0, self.BLOCK // 2 - 1, output)
        return output.getvalue()

    def enable_offline_pool(self):
        # The offline manifest accepts real 1 MiB blocks; exercise its actual
        # reader instead of mocking cached_block or bypassing that integration.
        self.BLOCK = 1024 * 1024
        # The outer setUp patch restores this only after producers stop.
        self.buffer.BLOCK = self.BLOCK
        self.entry.update(size=self.BLOCK * 8, relative='Fixture/movie.mkv')
        self.worker.config.update(playback_buffer_bytes=self.BLOCK * 4,
            download_block_bytes=self.BLOCK, max_cache_bytes=self.BLOCK * 32,
            download_connections=2)
        self.http.block = self.BLOCK
        self.http.payload = b'A' * self.entry['size']
        self.offline = importlib.import_module('playback_cache')
        self.ranges = importlib.import_module('range_cache')

    def seed_partial(self, *, legacy=False):
        staging, manifest, expected = self.offline.paths(self.worker, self.entry)
        if legacy:
            expected.pop('source_fingerprint', None)
        data = b'A' * self.BLOCK
        staging.parent.mkdir(parents=True, exist_ok=True)
        with staging.open('wb') as stream:
            stream.write(data)
            stream.truncate(self.entry['size'])
        self.ranges.manifest_write(manifest, {**expected,
            'completed': {'0': hashlib.sha256(data).hexdigest()}})
        return staging, manifest

    def test_committed_block_is_reused_before_full_download_without_another_upstream_request(self):
        first = self.read_prefix(self.entry)
        self.assertTrue(self.http.responses[0].blocked.wait(2), 'Producer did not reach read-ahead')
        manager = self.buffer.manager_for(self.worker)
        key = self.buffer.identity(self.entry)
        self.assertIn((key, 0), manager.blocks)
        self.assertNotIn((key, 1), manager.blocks)
        self.assertLess(manager.used, self.entry['size'])
        self.assertFalse(self.entry['ready'])
        self.assertFalse((self.root / 'cache/ready').exists())

        second = self.read_prefix(self.entry)
        self.assertEqual(first, b'A' * (self.BLOCK // 2))
        self.assertEqual(second, first)
        self.assertEqual(len(self.http.requests), 1)
        self.assertEqual(self.http.responses[0].bytes_read, self.BLOCK)
        self.assertIn(self.entry['id'], self.worker.playback_stream_activity)

    def test_same_path_and_size_with_new_source_fingerprint_does_not_reuse_old_bytes(self):
        self.assertEqual(self.read_prefix(self.entry), b'A' * (self.BLOCK // 2))
        self.assertTrue(self.http.responses[0].blocked.wait(2))
        manager = self.buffer.manager_for(self.worker)
        old_key = self.buffer.identity(self.entry)
        self.assertIn((old_key, 0), manager.blocks)

        replacement = {**self.entry, 'source_fid': 'replacement-object',
                       'source_checksum': 'replacement-checksum'}
        self.worker.entries[self.entry['id']] = replacement
        self.http.payload = b'B' * replacement['size']
        new_key = self.buffer.identity(replacement)
        self.assertNotEqual(old_key, new_key)
        self.assertEqual(self.read_prefix(replacement), b'B' * (self.BLOCK // 2))
        self.assertEqual(len(self.http.requests), 2)
        self.assertIn((old_key, 0), manager.blocks)
        self.assertIn((new_key, 0), manager.blocks)
        self.assertNotEqual(manager.blocks[(old_key, 0)]['path'], manager.blocks[(new_key, 0)]['path'])

    def test_playback_does_not_adopt_offline_block_from_replaced_source(self):
        self.enable_offline_pool()
        self.seed_partial()
        replacement = {**self.entry, 'source_fid': 'new-object', 'source_checksum': 'new-checksum'}
        self.worker.entries[self.entry['id']] = replacement
        self.http.payload = b'B' * replacement['size']
        self.assertEqual(self.read_prefix(replacement), b'B' * (self.BLOCK // 2))
        self.assertEqual(len(self.http.requests), 1)

    def test_playback_does_not_adopt_legacy_partial_without_source_fingerprint(self):
        self.enable_offline_pool()
        self.seed_partial(legacy=True)
        self.http.payload = b'B' * self.entry['size']
        self.assertEqual(self.read_prefix(self.entry), b'B' * (self.BLOCK // 2))
        self.assertEqual(len(self.http.requests), 1)

    def test_downloader_rebuilds_legacy_partial_and_publishes_fingerprint_accepted_by_playback(self):
        self.enable_offline_pool()
        _staging, manifest = self.seed_partial(legacy=True)
        self.entry.update(source_fid='new-object', source_checksum='new-checksum')
        self.http.payload = b'B' * self.entry['size']
        observations, playable = [], []

        def save():
            if manifest.exists():
                observations.append(json.loads(manifest.read_text('utf-8')))
                block = self.offline.cached_block(self.worker, self.entry, 0)
                if block is not None:
                    playable.append(block)

        def wait_downloads(futures, on_tick):
            for future in futures:
                on_tick()
                future.result(timeout=3)

        self.worker.cleanup = lambda **_kwargs: None
        self.worker.save = save
        self.worker.should_preempt = lambda *_args: False
        self.worker.wait_downloads = wait_downloads
        with patch.object(self.ranges, 'sync_cached_subtitles'):
            self.assertTrue(self.ranges.download_ranges(self.worker, self.entry))
        ready = self.root / 'cache/ready' / self.entry['relative']
        self.assertEqual(ready.read_bytes(), b'B' * self.entry['size'])
        fingerprint = self.buffer.identity(self.entry)
        self.assertTrue(observations)
        self.assertTrue(all(row.get('source_fingerprint') == fingerprint for row in observations))
        self.assertTrue(playable)
        self.assertTrue(all(block == b'B' * self.BLOCK for block in playable))

    def test_partial_invalidation_does_not_overwrite_an_existing_complete_file(self):
        self.enable_offline_pool()
        staging, manifest = self.seed_partial(legacy=True)
        target = self.root / 'cache/ready' / self.entry['relative']
        target.parent.mkdir(parents=True)
        target.write_bytes(b'complete-movie-original')
        with self.assertRaisesRegex(OSError, 'will not overwrite'):
            self.ranges.download_ranges(self.worker, self.entry)
        self.assertEqual(target.read_bytes(), b'complete-movie-original')
        self.assertTrue(staging.is_file())
        self.assertTrue(manifest.is_file())
        self.assertEqual(self.http.requests, [])


if __name__ == '__main__':
    unittest.main()
