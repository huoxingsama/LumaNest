import importlib
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


class WorkerBridgeTests(unittest.TestCase):
    def test_formal_gateway_roundtrip_with_remote_basepath(self):
        from personal_cinema.configuration import ConfigStore
        from personal_cinema.credentials import SecretStore
        from personal_cinema.runtime import Runtime
        from personal_cinema.worker import prepare
        seen = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get('Host')))
                raw = json.dumps({'Name': 'Isolated fixture', 'Items': []}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config, secrets = ConfigStore(root), SecretStore(root)
                config.update({'foundation.jellyfin_url': f'http://127.0.0.1:{upstream.server_port}/jf'}, 0)
                Runtime(root, config, secrets, None).apply()
                with patch.dict(os.environ, {'PERSONAL_CINEMA_HOME': str(root), 'MEDIA_LIBRARY_ROOT': str(root / 'runtime')}):
                    prepare()
                    from personal_cinema.runtime_paths import RuntimePath
                    module = importlib.import_module('metadata_proxy')
                    proxy = ThreadingHTTPServer(('127.0.0.1', 0), module.handler_for(module.Catalog(RuntimePath(root / 'runtime'))))
                    threading.Thread(target=proxy.serve_forever, daemon=True).start()
                    try:
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        with opener.open(f'http://127.0.0.1:{proxy.server_port}/System/Info/Public', timeout=5) as response:
                            self.assertEqual(json.load(response)['Name'], 'Isolated fixture')
                        with opener.open(f'http://127.0.0.1:{proxy.server_port}/MediaCache/', timeout=5) as response:
                            self.assertIn(b'html', response.read(2048).lower())
                        self.assertEqual(seen[0], ('/jf/System/Info/Public', f'127.0.0.1:{upstream.server_port}'))
                    finally:
                        proxy.shutdown()
                        proxy.server_close()
        finally:
            upstream.shutdown()
            upstream.server_close()

    def test_unapproved_worker_name_is_rejected(self):
        from personal_cinema.worker import main
        with self.assertRaises(SystemExit):
            main(['deploy_cache_and_one_piece'])


if __name__ == '__main__':
    unittest.main()
