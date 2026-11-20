import importlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_integrations import Secrets


class FixtureService(BaseHTTPRequestHandler):
    seen = []
    redirect_login = False
    def log_message(self, *args): pass
    def do_POST(self): self.respond()
    def do_GET(self): self.respond()
    def respond(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.seen.append((self.path, dict(self.headers)))
        if self.path == '/redirect' or self.path.startswith('/dav/') or (self.redirect_login and self.path in ('/api/auth/login', '/Users/AuthenticateByName')):
            self.send_response(302)
            self.send_header('Location', self.server.destination)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        value = {'AccessToken': 'fake-refreshed', 'code': 200, 'data': {'token': 'fake-refreshed'}}
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class SizedResponse(io.BytesIO):
    def __init__(self, size):
        super().__init__(b' ' * size)
        self.requested = []
    def read(self, size=-1):
        self.requested.append(size)
        return super().read(size)


class WorkerNetworkTests(unittest.TestCase):
    def setUp(self):
        from personal_cinema.configuration import ConfigStore
        from personal_cinema.runtime_paths import RuntimePath, instance_context
        from personal_cinema.worker import prepare
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.enterContext(instance_context(self.home))
        self.secrets = Secrets()
        for name in ('jellyfin.token', 'jellyfin.password', 'openlist.token', 'openlist.password', 'stream.key'):
            self.secrets.set(name, 'test-' + name)
        self.enterContext(patch('personal_cinema.runtime_paths.vault', return_value=self.secrets))
        config = ConfigStore(self.home)
        config.update({'foundation.jellyfin_username': 'fixture', 'sources.openlist_username': 'fixture'}, 0)
        self.root = RuntimePath(self.home / 'runtime')
        (self.root / 'data').mkdir(parents=True)
        (self.root / 'data/cache-config.json').write_text('{}', 'utf-8')
        prepare()
        self.cache = importlib.import_module('cache_worker')
        self.metadata = importlib.import_module('metadata_matcher')
        self.probe = importlib.import_module('media_probe')
        FixtureService.seen = []
        FixtureService.redirect_login = False
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), FixtureService)
        self.other = ThreadingHTTPServer(('127.0.0.1', 0), FixtureService)
        for server in (self.upstream, self.other):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.upstream.destination = f'http://127.0.0.1:{self.other.server_port}/target'
        base = f'http://127.0.0.1:{self.upstream.server_port}'
        (self.home / 'runtime-manifest.json').write_text(json.dumps({'endpoints': {'jellyfin': base, 'openlist': base, 'stream': base}}), 'utf-8')

    def test_worker_api_rejects_redirect_but_media_strips_auth_and_follows(self):
        from personal_cinema.network_security import CredentialRedirectError
        worker = self.cache.Worker(self.root)
        with self.assertRaises(CredentialRedirectError): worker.api('openlist', '/redirect')
        self.assertEqual(1, len(FixtureService.seen))
        with worker.http.open(worker.cloud_request('/Movies/test.mkv', range_header='bytes=0-4'), timeout=3) as response:
            response.read()
        headers = {k.lower(): v for k, v in FixtureService.seen[-1][1].items()}
        self.assertNotIn('authorization', headers)
        self.assertEqual('bytes=0-4', headers.get('range'))

    def test_worker_login_cannot_redirect_credentials(self):
        from personal_cinema.network_security import CredentialRedirectError
        FixtureService.redirect_login = True
        worker = self.cache.Worker(self.root)
        with self.assertRaises(CredentialRedirectError): worker.renew_openlist_token()
        worker.token = None
        with self.assertRaises(CredentialRedirectError): worker.api('jellyfin', '/Items')
        self.assertEqual(2, len(FixtureService.seen))

    def test_worker_json_and_login_responses_are_bounded(self):
        worker = self.cache.Worker(self.root)
        for call in (lambda: worker.api('openlist', '/read'), worker.renew_openlist_token):
            response = SizedResponse(16 * 1024 * 1024 + 1)
            with patch.object(worker.api_http, 'open', return_value=response), self.assertRaises(ValueError): call()
            self.assertEqual([16 * 1024 * 1024 + 1], response.requested)

    def test_metadata_api_and_login_cannot_redirect(self):
        from personal_cinema.network_security import CredentialRedirectError
        with patch.object(self.metadata, '_cinema_vault', return_value=self.secrets):
            client = self.metadata.Jellyfin(self.root)
        with self.assertRaises(CredentialRedirectError): client.json('/redirect')
        FixtureService.redirect_login = True
        client.token = None
        with self.assertRaises(CredentialRedirectError): client.json('/Items')
        self.assertEqual(2, len(FixtureService.seen))

    def test_metadata_json_response_limit_is_separate_from_image_budget(self):
        with patch.object(self.metadata, '_cinema_vault', return_value=self.secrets):
            client = self.metadata.Jellyfin(self.root)
        for image, limit in ((False, 16 * 1024 * 1024), (True, 12 * 1024 * 1024)):
            response = SizedResponse(limit + 1)
            with patch.object(client.http, 'open', return_value=response), self.assertRaises(ValueError):
                client.request('/large', image=image)
            self.assertEqual([limit + 1], response.requested)

    def test_probe_internal_stream_does_not_follow_redirect(self):
        from personal_cinema.network_security import CredentialRedirectError, NoCredentialRedirect
        with patch.object(self.probe._BudgetBridge, 'read_range', return_value=b''):
            bridge = self.probe._BudgetBridge(self.root, {'id': 'fixture', 'size': 100})
        self.addCleanup(bridge.close)
        self.assertTrue(any(isinstance(h, NoCredentialRedirect) for h in bridge.http.handlers))
        bridge.url = f'http://127.0.0.1:{self.upstream.server_port}/redirect'
        with self.assertRaises(CredentialRedirectError): bridge.read_range(0, 4)
        self.assertEqual(1, len(FixtureService.seen))


if __name__ == '__main__': unittest.main()
