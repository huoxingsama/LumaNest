import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from personal_cinema.configuration import ConfigStore
from personal_cinema.credentials import SecretStore
from personal_cinema.integrations import IntegrationManager


class JellyfinAuthTests(unittest.TestCase):
    def test_authenticated_requests_use_combined_authorization(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                auth = self.headers.get('Authorization', '')
                if 'Token="fixture-token"' not in auth:
                    self.send_error(401); return
                raw = b'{"Id":"fixture-user"}'
                self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config, vault = ConfigStore(directory), SecretStore(directory)
                config.update({'foundation.jellyfin_url': 'http://127.0.0.1:' + str(server.server_port)}, 0)
                vault.set('jellyfin.token', 'fixture-token')
                self.assertEqual(IntegrationManager(directory, config, vault)._http('jellyfin', '/Users/Me')['Id'], 'fixture-user')
        finally: server.shutdown(); server.server_close()

    def test_bad_token_is_rejected_without_revealing_or_saving_response(self):
        from personal_cinema.network_security import jellyfin_authorization
        for token in ('private"injection', 'private\r\nInjected:x', 'private\\escape', 'private,part'):
            with self.subTest(token=repr(token)):
                with self.assertRaises(ValueError) as error: jellyfin_authorization(token)
                self.assertNotIn('private', str(error.exception))
        with tempfile.TemporaryDirectory() as directory:
            config, vault = ConfigStore(directory), SecretStore(directory)
            manager = IntegrationManager(directory, config, vault)
            with patch.object(manager, '_http', return_value={'AccessToken': 'private"response'}):
                with self.assertRaises(ValueError): manager._login('jellyfin', {'username': 'fixture', 'password': 'fixture-password'})
            self.assertIsNone(vault.get('jellyfin.token'))

    def test_workbench_fallback_combines_token_and_preserves_existing_auth(self):
        from personal_cinema.worker import prepare
        prepare()
        from task_center_api import auth_headers
        full = 'MediaBrowser Client="Client", Token="fixture-token"'
        self.assertEqual(auth_headers({'Authorization': full})['Authorization'], full)
        self.assertIn('Token="fixture-token"', auth_headers({'X-Emby-Token': 'fixture-token'})['Authorization'])
        self.assertIn('Token="fixture-token"', auth_headers({}, 'api_key=fixture-token')['Authorization'])


if __name__ == '__main__': unittest.main()
