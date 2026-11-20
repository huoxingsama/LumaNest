import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request


class RedirectService(BaseHTTPRequestHandler):
    observed = []
    def log_message(self, *args): pass
    def do_GET(self):
        self.observed.append((self.path, dict(self.headers)))
        if self.path.startswith('/redirect'):
            self.send_response(302)
            self.send_header('Location', self.server.target)
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')


class NetworkSecurityTests(unittest.TestCase):
    def setUp(self):
        RedirectService.observed = []
        self.first = ThreadingHTTPServer(('127.0.0.1', 0), RedirectService)
        self.second = ThreadingHTTPServer(('127.0.0.1', 0), RedirectService)
        for server in (self.first, self.second):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.url = f'http://127.0.0.1:{self.first.server_port}'
        self.first.target = f'http://127.0.0.1:{self.second.server_port}/media'

    def request(self):
        return urllib.request.Request(self.url + '/redirect', headers={
            'Authorization': 'Bearer test-secret', 'Cookie': 'private=test-cookie', 'X-Emby-Token': 'test-token',
            'X-Api-Key': 'test-api-key', 'Range': 'bytes=0-8', 'User-Agent': 'PersonalCinemaTest'})

    def test_credential_api_does_not_forward_any_redirect(self):
        from personal_cinema.network_security import credential_opener, CredentialRedirectError
        with self.assertRaises(CredentialRedirectError):
            credential_opener(urllib.request.ProxyHandler({})).open(self.request(), timeout=3)
        self.assertEqual(1, len(RedirectService.observed))

    def test_stream_cross_origin_redirect_strips_secrets_but_keeps_range(self):
        from personal_cinema.network_security import media_opener
        with media_opener(urllib.request.ProxyHandler({})).open(self.request(), timeout=3) as response:
            self.assertEqual(b'ok', response.read())
        headers = {key.lower(): value for key, value in RedirectService.observed[-1][1].items()}
        for name in ('authorization', 'cookie', 'x-emby-token', 'x-api-key'):
            self.assertNotIn(name, headers)
        self.assertEqual('bytes=0-8', headers.get('range'))

    def test_stream_same_origin_keeps_service_authorization(self):
        from personal_cinema.network_security import media_opener
        self.first.target = self.url + '/media'
        with media_opener(urllib.request.ProxyHandler({})).open(self.request(), timeout=3) as response:
            response.read()
        headers = {key.lower(): value for key, value in RedirectService.observed[-1][1].items()}
        self.assertEqual('Bearer test-secret', headers.get('authorization'))

    def test_stream_redirect_downgrade_and_userinfo_are_rejected(self):
        from personal_cinema.network_security import MediaRedirect, CredentialRedirectError
        import io
        request = urllib.request.Request('https://source.example/media')
        fixture_userinfo_url = 'https://' + 'username:password' + '@cdn.example/media'
        for target in ('http://cdn.example/media', fixture_userinfo_url):
            with self.subTest(target=target), self.assertRaises(CredentialRedirectError):
                MediaRedirect().redirect_request(request, io.BytesIO(), 302, '', {}, target)


if __name__ == '__main__': unittest.main()
