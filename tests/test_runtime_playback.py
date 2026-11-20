import importlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from personal_cinema.configuration import ConfigStore
from personal_cinema.credentials import SecretStore
from personal_cinema.runtime import Runtime, write
from personal_cinema.runtime_paths import instance_context


class PlaybackTests(unittest.TestCase):
    def test_local_only_library_defers_apply_for_paused_session(self):
        responses = [[{'NowPlayingItem': {'Id': 'fixture'}, 'PlayState': {'IsPaused': True}}], []]
        seen = []

        class FakeJellyfin(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get('X-Emby-Token')))
                raw = json.dumps(responses.pop(0)).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(('127.0.0.1', 0), FakeJellyfin)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config, secrets = ConfigStore(root), SecretStore(root)
                runtime = Runtime(root, config, secrets, None)
                config.update({'foundation.jellyfin_url': f'http://127.0.0.1:{server.server_port}/jf'}, 0)
                secrets.set('jellyfin.token', 'fixture-token')
                runtime.apply()
                self.assertFalse((root / 'runtime/data/cache-status.json').exists())
                with patch.object(runtime, '_alive', side_effect=lambda name: name == 'gateway'):
                    self.assertEqual(runtime.apply()['state'], 'waiting_for_playback')
                    self.assertFalse(runtime.playing())
                self.assertEqual(seen, [('/jf/Sessions', 'fixture-token')] * 2)
        finally:
            server.shutdown()
            server.server_close()

    def test_failed_playback_status_does_not_stop_running_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = Runtime(root, ConfigStore(root), SecretStore(root), None)
            runtime.apply()
            with patch.object(runtime, '_alive', return_value=True):
                self.assertTrue(runtime.playing())
                with self.assertRaises(ValueError):
                    runtime.stop()

    def test_buffer_uses_configured_directory_and_more_than_ten_gib(self):
        from personal_cinema.worker import prepare
        from types import SimpleNamespace
        prepare()
        buffer = importlib.import_module('playback_buffer')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, secrets = ConfigStore(root), SecretStore(root)
            config.update({'cache.directory': str(root / 'selected-cache'), 'cache.playback_gib': 12}, 0)
            Runtime(root, config, secrets, None).apply()
            worker = SimpleNamespace(root=root / 'runtime', config={'playback_buffer_bytes': 12 * 1024 ** 3})
            with instance_context(root):
                manager = buffer.Manager(worker)
                self.assertEqual(manager.path.resolve(), (root / 'selected-cache/playback-buffer').resolve())
                self.assertEqual(buffer.limit(worker), 12 * 1024 ** 3)
                self.assertFalse((root / 'runtime/cache/playback-buffer').exists())


if __name__ == '__main__':
    unittest.main()
