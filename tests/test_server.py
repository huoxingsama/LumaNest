import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('personal_cinema.server'), 'Authenticated settings API has not been implemented')
        from personal_cinema.server import create_server
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server(Path(self.temp.name), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.headers = {}

    def tearDown(self):
        if hasattr(self, 'server'):
            self.server.shutdown()
            self.server.server_close()
            self.temp.cleanup()

    def request(self, path, body=None, headers=None, method=None):
        req = urllib.request.Request(self.url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={**self.headers, 'Content-Type': 'application/json', **(headers or {})}, method=method)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        data = response.read()
        return response, json.loads(data) if data else {}

    def login(self):
        response, value = self.request('/api/session', {'token': self.server.application.launch_token}, headers={'Origin': self.url})
        self.assertEqual(response.status, 200)
        self.headers['Cookie'] = response.headers['Set-Cookie'].split(';')[0]
        self.headers['X-Cinema-CSRF'] = value['csrf']
        self.headers['Origin'] = self.url

    def test_requires_session_and_rejects_cross_site_mutation(self):
        response, _ = self.request('/api/config')
        self.assertEqual(response.status, 401)
        self.login()
        response, config = self.request('/api/config')
        self.assertEqual(response.status, 200)
        response, _ = self.request('/api/config', {'revision': config['revision'], 'changes': {}}, headers={'Origin': 'https://attacker.invalid'})
        self.assertEqual(response.status, 403)
        response, _ = self.request('/api/config', {'revision': config['revision'], 'changes': {}}, headers={'X-Cinema-CSRF': 'wrong'})
        self.assertEqual(response.status, 403)

    def test_persists_config_and_rejects_stale_revision(self):
        self.login()
        _, config = self.request('/api/config')
        payload = {'revision': config['revision'], 'changes': {'cache.playback_gib': 8}}
        response, saved = self.request('/api/config', payload)
        self.assertEqual(response.status, 200)
        self.assertEqual(saved['values']['cache.playback_gib'], 8)
        response, _ = self.request('/api/config', payload)
        self.assertEqual(response.status, 409)

    def test_host_validation_and_no_credentials_in_export(self):
        self.login()
        response, _ = self.request('/api/config', headers={'Host': 'attacker.invalid'})
        self.assertEqual(response.status, 403)
        response, _ = self.request('/api/credentials', {'name': 'mdblist.api_key', 'value': 'private-test-value'})
        self.assertEqual(response.status, 200)
        _, value = self.request('/api/config/export')
        self.assertNotIn('private-test-value', json.dumps(value))
        _, value = self.request('/api/diagnostics')
        self.assertNotIn('private-test-value', json.dumps(value))

    def test_internal_profiles_and_session_keys_cannot_be_edited_as_provider_credentials(self):
        self.login()
        for name in ('settings.launcher', 'player.fixture.current', 'jellyfin.user_id', 'stream.key'):
            self.server.application.secrets.set(name, 'internal-test-value')
            response, _ = self.request('/api/credentials', {'name': name, 'value': 'replacement'})
            self.assertEqual(response.status, 400)
            response, _ = self.request('/api/credentials/' + name, method='DELETE')
            self.assertEqual(response.status, 400)
        _, data = self.request('/api/credentials')
        self.assertEqual(data['configured'], {})
        _, value = self.request('/api/diagnostics')
        self.assertNotIn('internal-test-value', json.dumps(value))


if __name__ == '__main__':
    unittest.main()
